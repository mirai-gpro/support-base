"""
Live API WebSocket 中継 (LiveRelay)

stt_stream.py の GeminiLiveApp を Web 向けに再構成。
ブラウザ ↔ サーバー ↔ Gemini Live API の WebSocket 中継を行う。

主要な責務:
1. ブラウザからの PCM 16kHz 音声を Gemini に中継
2. Gemini からの PCM 24kHz 音声をブラウザに中継
3. 累積文字数制限の回避（自動再接続 + コンテキスト引き継ぎ）
4. AI音声を A2E サービスに送信し、Expression をブラウザに中継（アバター連携）
5. 割り込み（barge-in）処理
6. transcription のブラウザ中継

プロトコル (クライアント ↔ サーバー WebSocket):
  クライアント → サーバー:
    { "type": "audio", "data": "<base64 PCM 16kHz>" }
    { "type": "text",  "data": "テキスト入力" }
    { "type": "stop" }

  サーバー → クライアント:
    { "type": "audio",         "data": "<base64 PCM 24kHz>" }
    { "type": "transcription", "role": "user"|"ai", "text": "..." }
    { "type": "expression",    "data": { names, frames, frame_rate } }
    { "type": "shop_cards",    "shops": [...], "response": "..." }
    { "type": "rest_audio",    "data": "<base64 MP3>", "text": "..." }
    { "type": "interrupted" }
    { "type": "reconnecting",  "reason": "..." }
    { "type": "reconnected",   "session_count": N }
    { "type": "error",         "message": "..." }
"""

import asyncio
import base64
import logging
from dataclasses import dataclass, field

from google import genai
from google.genai import types

from fastapi import WebSocket, WebSocketDisconnect

from support_base.config.settings import (
    GEMINI_API_KEY,
    LIVE_API_MODEL,
    RECONNECT_DELAY_SECONDS,
)
from support_base.i18n.language_config import get_language_profile
from support_base.live.reconnect import ReconnectManager
from support_base.modes.base_mode import BaseModePlugin
from support_base.services.a2e_client import A2EClient
from support_base.session.manager import Session

logger = logging.getLogger(__name__)


@dataclass
class LiveRelayState:
    """LiveRelay の内部状態"""
    user_transcript_buffer: str = ""
    ai_transcript_buffer: str = ""
    is_running: bool = True
    # Expression ストリーミング用
    a2e_chunk_buffer: bytearray = field(default_factory=bytearray)
    a2e_total_bytes: int = 0  # ターン内の累計音声バイト数
    a2e_chunk_index: int = 0  # ターン内のチャンク連番
    a2e_turn_complete: bool = False  # ターン完了フラグ（ゼロチャンク送信防止）


class LiveRelay:
    """
    Gemini Live API WebSocket 中継

    stt_stream.py の GeminiLiveApp.run() + _session_loop() +
    receive_audio() を Web 向けに再構成。
    """

    def __init__(
        self,
        session: Session,
        mode_plugin: BaseModePlugin,
        a2e_client: A2EClient | None = None,
    ):
        self.session = session
        self.mode_plugin = mode_plugin
        self.a2e_client = a2e_client
        self.reconnect_mgr = ReconnectManager()
        self.state = LiveRelayState()
        self._gemini_client = genai.Client(api_key=GEMINI_API_KEY)

    # Gemini 接続エラー時の最大リトライ回数
    MAX_GEMINI_RETRIES = 3

    async def handle_client_ws(self, websocket: WebSocket) -> None:
        """
        クライアント WebSocket ハンドラ — メインエントリーポイント

        stt_stream.py L714-796 の run() に相当。
        再接続ループを管理する。
        """
        await websocket.accept()
        logger.info(f"[LiveRelay] Client connected: session={self.session.session_id}")

        error_retries = 0  # エラー起因の連続リトライ回数

        try:
            while self.state.is_running:
                try:
                    await self._run_gemini_session(websocket)
                    # 正常な再接続（累積文字数制限等）はカウンターをリセット
                    error_retries = 0
                    if not self.reconnect_mgr.needs_reconnect:
                        break
                except WebSocketDisconnect:
                    logger.info("[LiveRelay] Client disconnected")
                    break
                except Exception as e:
                    if ReconnectManager.is_retriable_error(e):
                        error_retries += 1
                        logger.warning(
                            f"[LiveRelay] Retriable error ({error_retries}/"
                            f"{self.MAX_GEMINI_RETRIES}): {e}",
                            exc_info=True,
                        )
                        if error_retries >= self.MAX_GEMINI_RETRIES:
                            logger.error(
                                f"[LiveRelay] Max retries exceeded "
                                f"({self.MAX_GEMINI_RETRIES}), giving up: "
                                f"session={self.session.session_id}"
                            )
                            await self._send_json(websocket, {
                                "type": "error",
                                "message": f"Gemini connection failed after "
                                           f"{self.MAX_GEMINI_RETRIES} retries: "
                                           f"{type(e).__name__}: {str(e)[:200]}",
                            })
                            break
                        await self._send_json(websocket, {
                            "type": "reconnecting",
                            "reason": "error",
                        })
                        await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                        self.reconnect_mgr.needs_reconnect = True
                        continue
                    logger.error(f"[LiveRelay] Fatal error: {e}", exc_info=True)
                    await self._send_json(websocket, {
                        "type": "error",
                        "message": f"{type(e).__name__}: {str(e)[:200]}",
                    })
                    break
        finally:
            # 正常な close frame を送信（1006 防止）
            try:
                await websocket.close(code=1000, reason="Session ended")
            except Exception:
                pass  # 既に切断済みの場合は無視
            logger.info(f"[LiveRelay] Session ended: {self.session.session_id}")

    async def _run_gemini_session(self, client_ws: WebSocket) -> None:
        """
        1つの Gemini セッションを実行

        stt_stream.py L741-783 の1ループ反復に相当。
        """
        # コンテキスト引き継ぎ (stt_stream.py L747-752)
        context = None
        if self.reconnect_mgr.session_count > 0:
            context = self.session.memory.get_context_summary()
            logger.info(
                f"[LiveRelay] Reconnecting with context: "
                f"{context[:80] if context else 'none'}..."
            )

        config = self._build_live_config(context)
        self.reconnect_mgr.reset_for_new_session()
        self.session.live_session_count = self.reconnect_mgr.session_count

        # 状態リセット
        self.state.user_transcript_buffer = ""
        self.state.ai_transcript_buffer = ""
        self.state.a2e_chunk_buffer = bytearray()
        self.state.a2e_total_bytes = 0
        self.state.a2e_chunk_index = 0
        self.state.a2e_turn_complete = False

        logger.info(
            f"[LiveRelay] Connecting to Gemini: model={LIVE_API_MODEL}, "
            f"session_count={self.reconnect_mgr.session_count + 1}, "
            f"session={self.session.session_id}"
        )
        async with self._gemini_client.aio.live.connect(
            model=LIVE_API_MODEL,
            config=config,
        ) as gemini_session:

            if self.reconnect_mgr.session_count > 1:
                # 再接続通知 (stt_stream.py L766-776)
                try:
                    await gemini_session.send_client_content(
                        turns=types.Content(
                            role="user",
                            parts=[types.Part(text="続きをお願いします")],
                        ),
                        turn_complete=True,
                    )
                    logger.info("[LiveRelay] Reconnection prompt sent")
                except Exception as e:
                    logger.warning(f"[LiveRelay] Reconnection prompt failed: {e}")

                await self._send_json(client_ws, {
                    "type": "reconnected",
                    "session_count": self.reconnect_mgr.session_count,
                })

            # 3つの非同期タスクを並行実行 (stt_stream.py L926-930)
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(
                        self._relay_client_to_gemini(client_ws, gemini_session)
                    )
                    tg.create_task(
                        self._relay_gemini_to_client(gemini_session, client_ws)
                    )
            except* WebSocketDisconnect:
                raise
            except* Exception as eg:
                for e in eg.exceptions:
                    if isinstance(e, WebSocketDisconnect):
                        raise e
                    if ReconnectManager.is_retriable_error(e):
                        self.reconnect_mgr.needs_reconnect = True
                        logger.warning(
                            f"[LiveRelay] Task error (retriable): "
                            f"{type(e).__name__}: {e}",
                            exc_info=e,
                        )
                    else:
                        logger.error(
                            f"[LiveRelay] Task error (fatal): "
                            f"{type(e).__name__}: {e}",
                            exc_info=e,
                        )
                        raise e

    async def _relay_client_to_gemini(
        self, client_ws: WebSocket, gemini_session
    ) -> None:
        """
        ブラウザ → Gemini 中継

        stt_stream.py の listen_audio() + send_audio() に相当。
        ブラウザからは JSON メッセージで音声/テキストを受け取り、Gemini に転送。
        """
        while not self.reconnect_mgr.needs_reconnect:
            try:
                raw = await asyncio.wait_for(
                    client_ws.receive_text(), timeout=0.5
                )
            except asyncio.TimeoutError:
                continue
            except WebSocketDisconnect:
                self.state.is_running = False
                self.reconnect_mgr.needs_reconnect = True
                raise

            try:
                import json
                msg = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue

            msg_type = msg.get("type")

            if msg_type == "audio":
                # base64 PCM 16kHz → Gemini
                audio_bytes = base64.b64decode(msg["data"])
                await gemini_session.send_realtime_input(
                    audio={"data": audio_bytes, "mime_type": "audio/pcm"}
                )

            elif msg_type == "text":
                # テキスト入力 → Gemini
                text = msg.get("data", "")
                if text:
                    await gemini_session.send_client_content(
                        turns=types.Content(
                            role="user",
                            parts=[types.Part(text=text)],
                        ),
                        turn_complete=True,
                    )
                    self.session.memory.add("ユーザー", text)

            elif msg_type == "stop":
                self.state.is_running = False
                self.reconnect_mgr.needs_reconnect = True
                return

    async def _relay_gemini_to_client(
        self, gemini_session, client_ws: WebSocket
    ) -> None:
        """
        Gemini → ブラウザ 中継

        stt_stream.py の receive_audio() (L579-683) を Web向けに再構成。
        音声データ、transcription、割り込みをブラウザに中継し、
        A2Eサービスに並行リクエストして Expression もブラウザに送信する。
        """
        while not self.reconnect_mgr.needs_reconnect:
            turn = gemini_session.receive()
            async for response in turn:
                if self.reconnect_mgr.needs_reconnect:
                    return

                sc = response.server_content
                if not sc:
                    # --- Function Calling ハンドリング ---
                    if hasattr(response, 'tool_call') and response.tool_call:
                        await self._handle_tool_call(
                            response.tool_call, gemini_session, client_ws
                        )
                    continue

                # --- 割り込み検知 (stt_stream.py L650-662) ---
                if hasattr(sc, "interrupted") and sc.interrupted:
                    logger.info("[LiveRelay] Barge-in detected")
                    # AI音声バッファをフラッシュ
                    if self.state.ai_transcript_buffer.strip():
                        self.session.memory.add(
                            "AI", self.state.ai_transcript_buffer.strip()
                        )
                    self.state.ai_transcript_buffer = ""
                    self.state.a2e_chunk_buffer = bytearray()
                    self.state.a2e_total_bytes = 0
                    self.state.a2e_chunk_index = 0
                    self.state.a2e_turn_complete = False
                    await self._send_json(client_ws, {"type": "interrupted"})
                    continue

                # --- 入力 transcription (stt_stream.py L665-669) ---
                if hasattr(sc, "input_transcription") and sc.input_transcription:
                    user_text = sc.input_transcription.text
                    if user_text:
                        self.state.user_transcript_buffer += user_text
                        await self._send_json(client_ws, {
                            "type": "transcription",
                            "role": "user",
                            "text": user_text,
                            "is_partial": True,
                        })

                # --- 出力 transcription (stt_stream.py L672-676) ---
                if hasattr(sc, "output_transcription") and sc.output_transcription:
                    ai_text = sc.output_transcription.text
                    if ai_text:
                        self.state.ai_transcript_buffer += ai_text
                        await self._send_json(client_ws, {
                            "type": "transcription",
                            "role": "ai",
                            "text": ai_text,
                            "is_partial": True,
                        })

                # --- 音声データ (stt_stream.py L679-683) ---
                if sc.model_turn:
                    for part in sc.model_turn.parts:
                        if hasattr(part, "inline_data") and part.inline_data:
                            audio_data = part.inline_data.data
                            if isinstance(audio_data, bytes):
                                # ブラウザに音声を即時送信
                                await self._send_json(client_ws, {
                                    "type": "audio",
                                    "data": base64.b64encode(audio_data).decode(),
                                })
                                # A2E用にバッファに蓄積（ストリーミング）
                                self.state.a2e_chunk_buffer.extend(audio_data)
                                self.state.a2e_total_bytes += len(audio_data)

                                # 段階的チャンクサイズ（フロントエンド逆提案を採用）
                                # chunk#1: 0.25秒（初動を最速に）
                                # chunk#2: 2.0秒（冒頭品質向上）
                                # chunk#3〜: 5.0秒（503対策）
                                A2E_FIRST_CHUNK = 24000 * 2 // 4       # 12,000 bytes = 0.25s
                                A2E_SECOND_CHUNK = 24000 * 2 * 2       # 96,000 bytes = 2.0s
                                A2E_NORMAL_CHUNK = 24000 * 2 * 5       # 240,000 bytes = 5.0s

                                idx = self.state.a2e_chunk_index
                                if idx == 0:
                                    threshold = A2E_FIRST_CHUNK
                                elif idx == 1:
                                    threshold = A2E_SECOND_CHUNK
                                else:
                                    threshold = A2E_NORMAL_CHUNK

                                if len(self.state.a2e_chunk_buffer) >= threshold:
                                    chunk = bytes(self.state.a2e_chunk_buffer)
                                    self.state.a2e_chunk_buffer = bytearray()
                                    chunk_index = self.state.a2e_chunk_index
                                    self.state.a2e_chunk_index += 1
                                    asyncio.create_task(
                                        self._send_expression_chunk(
                                            client_ws, chunk,
                                            is_first=(chunk_index == 0),
                                            chunk_index=chunk_index,
                                        )
                                    )

                # --- ターン完了 (stt_stream.py L600-645) ---
                if hasattr(sc, "turn_complete") and sc.turn_complete:
                    await self._on_turn_complete(client_ws)

    async def _on_turn_complete(self, client_ws: WebSocket) -> None:
        """
        ターン完了時の処理

        stt_stream.py L600-645 に相当:
        1. ユーザー transcription を確定
        2. AI transcription を確定
        3. 累積文字数チェック → 再接続判定
        4. A2E → Expression をブラウザに送信（アバター連携）
        """
        # ユーザー発言の確定
        user_text = self.state.user_transcript_buffer.strip()
        if user_text:
            self.session.memory.add("ユーザー", user_text)
            await self._send_json(client_ws, {
                "type": "transcription",
                "role": "user",
                "text": user_text,
                "is_partial": False,
            })
        self.state.user_transcript_buffer = ""

        # AI発言の確定
        ai_text = self.state.ai_transcript_buffer.strip()
        if ai_text:
            self.session.memory.add("AI", ai_text)
            await self._send_json(client_ws, {
                "type": "transcription",
                "role": "ai",
                "text": ai_text,
                "is_partial": False,
            })

            # 累積文字数チェック → 再接続判定 (stt_stream.py L624-643)
            self.reconnect_mgr.on_ai_speech_complete(
                ai_text, self.session.language
            )
            if self.reconnect_mgr.needs_reconnect:
                await self._send_json(client_ws, {
                    "type": "reconnecting",
                    "reason": self.reconnect_mgr.reconnect_reason,
                })
        self.state.ai_transcript_buffer = ""

        # --- A2E → Expression（アバター連携） ---
        # ターン完了フラグを立て、以降のゼロチャンク送信を防止（P1対応）
        self.state.a2e_turn_complete = True

        # 残りのバッファをフラッシュ（最後のチャンク）
        if self.a2e_client and len(self.state.a2e_chunk_buffer) > 0:
            chunk = bytes(self.state.a2e_chunk_buffer)
            self.state.a2e_chunk_buffer = bytearray()

            # 短すぎるチャンクは無音でパディング（最低0.25秒 = 12000bytes）
            MIN_CHUNK_BYTES = 24000 * 2 // 4  # 12000 bytes = 0.25s
            if len(chunk) < MIN_CHUNK_BYTES:
                chunk = chunk + b'\x00' * (MIN_CHUNK_BYTES - len(chunk))

            chunk_index = self.state.a2e_chunk_index
            self.state.a2e_chunk_index += 1
            await self._send_expression_chunk(
                client_ws, chunk,
                is_final=True,
                chunk_index=chunk_index,
            )
        self.state.a2e_total_bytes = 0

    async def _send_expression_chunk(
        self,
        client_ws: WebSocket,
        audio_chunk: bytes,
        is_first: bool = False,
        is_final: bool = False,
        chunk_index: int = 0,
    ) -> None:
        """
        音声チャンクから expression を生成してクライアントへ送信（ストリーミング対応）

        段階的チャンクサイズ (0.25s → 2.0s → 5.0s) で audio2exp に送信し、
        結果を即座にクライアントへ転送する。
        フロントエンドの queueLiveExpressionFrames() がバッファに追加して再生する。
        """
        # P1対応: turn_complete 後はexpressionを送信しない（ゼロチャンク防止）
        if self.state.a2e_turn_complete and not is_final:
            logger.debug(
                f"[LiveRelay] Skipping A2E chunk after turn_complete: "
                f"chunk_index={chunk_index}, session={self.session.session_id}"
            )
            return

        try:
            audio_b64 = base64.b64encode(audio_chunk).decode()
            pcm_duration_ms = len(audio_chunk) // (24000 * 2) * 1000
            logger.info(
                f"[LiveRelay] A2E chunk: "
                f"{len(audio_chunk)} bytes (~{pcm_duration_ms}ms), "
                f"chunk_index={chunk_index}, "
                f"is_first={is_first}, is_final={is_final}, "
                f"session={self.session.session_id}"
            )
            result = await self.a2e_client.process_audio(
                audio_base64=audio_b64,
                session_id=self.session.session_id,
                audio_format="pcm",
                sample_rate=24000,
                is_start=is_first,
                is_final=is_final,
            )
            if result and result.frames:
                # P3対応: jawOpen スケーリング（平均0.03→0.08〜0.12を目指す）
                JAW_OPEN_SCALE = 1.8
                try:
                    jaw_idx = result.names.index("jawOpen")
                    for frame in result.frames:
                        frame[jaw_idx] = min(frame[jaw_idx] * JAW_OPEN_SCALE, 1.0)
                except ValueError:
                    pass  # jawOpen が names に存在しない場合はスキップ

                non_zero = sum(
                    1 for f in result.frames if any(v > 0.001 for v in f)
                )
                # P2対応: chunk_index と is_final をメッセージに追加
                await self._send_json(client_ws, {
                    "type": "expression",
                    "data": {
                        "names": result.names,
                        "frames": result.frames,
                        "frame_rate": result.frame_rate,
                        "chunk_index": chunk_index,
                        "is_final": is_final,
                    },
                })
                logger.info(
                    f"[LiveRelay] Expression chunk sent: "
                    f"{len(result.frames)} frames "
                    f"({non_zero} non-zero), "
                    f"chunk_index={chunk_index}, is_final={is_final}"
                )
            else:
                logger.warning(
                    f"[LiveRelay] A2E chunk returned no frames: "
                    f"chunk_index={chunk_index}, "
                    f"session={self.session.session_id}"
                )
        except Exception as e:
            logger.warning(f"[LiveRelay] A2E chunk error (non-fatal): {e}")

    async def _handle_tool_call(
        self, tool_call, gemini_session, client_ws: WebSocket
    ) -> None:
        """
        Gemini からの Function Call を処理

        search_restaurants の場合:
        1. REST API ロジックでレストラン検索 + enrich
        2. ショップカードを WebSocket でクライアントに送信
        3. 1軒目の解説を TTS で音声化してクライアントに送信（隙間埋め）
        4. tool_response を Gemini に返して会話を継続
        """
        function_responses = []

        for fc in tool_call.function_calls:
            logger.info(
                f"[LiveRelay] Tool call: name={fc.name}, "
                f"args={fc.args}, session={self.session.session_id}"
            )

            if fc.name == "search_restaurants":
                result = await self._execute_restaurant_search(
                    fc.args, client_ws
                )
                function_responses.append(
                    types.FunctionResponse(
                        id=fc.id,
                        name=fc.name,
                        response=result,
                    )
                )
            else:
                logger.warning(f"[LiveRelay] Unknown tool: {fc.name}")
                function_responses.append(
                    types.FunctionResponse(
                        id=fc.id,
                        name=fc.name,
                        response={"error": f"Unknown tool: {fc.name}"},
                    )
                )

        # tool_response を Gemini に返す → Gemini が続きの音声を生成
        if function_responses:
            await gemini_session.send_tool_response(
                function_responses=function_responses
            )
            logger.info(
                f"[LiveRelay] Tool response sent: "
                f"{len(function_responses)} responses"
            )

    async def _execute_restaurant_search(
        self, args: dict, client_ws: WebSocket
    ) -> dict:
        """
        search_restaurants ツールの実行

        REST API ロジック (SupportAssistant + enrich_shops_with_photos) を呼び出し、
        ショップカードを WebSocket で送信。1軒目の解説を TTS で読み上げ。
        """
        from support_base.core.support_core import (
            SYSTEM_PROMPTS,
            SupportSession,
            SupportAssistant,
        )
        from support_base.core.api_integrations import (
            enrich_shops_with_photos,
            extract_area_from_text,
        )

        query = args.get("query", "")
        language = self.session.language or "ja"
        session_id = self.session.session_id

        logger.info(
            f"[LiveRelay] Restaurant search: query={query!r}, "
            f"lang={language}, session={session_id}"
        )

        try:
            # REST 用の SupportSession を作成/取得
            rest_session = SupportSession(session_id)
            rest_data = rest_session.get_data()
            if not rest_data:
                rest_session.initialize({}, language=language, mode="chat")

            rest_session.update_language(language)
            rest_session.update_mode("chat")
            rest_session.add_message("user", query, "chat")

            # Gemini REST で推論（同期 → 別スレッドで実行しイベントループをブロックしない）
            assistant = SupportAssistant(rest_session, SYSTEM_PROMPTS)
            result = await asyncio.to_thread(
                assistant.process_user_message, query, "conversation"
            )

            shops = result.get("shops") or []
            response_text = result.get("response", "")

            # enrich with Google Places / HotPepper / TripAdvisor
            # （同期API呼び出し → 別スレッドで実行）
            if shops:
                area = extract_area_from_text(query, language)
                shops = await asyncio.to_thread(
                    enrich_shops_with_photos, shops, area, language
                ) or []

            # ショップカードをクライアントに即送信
            if shops:
                shop_messages = {
                    "ja": lambda c: f"ご希望に合うお店を{c}件ご紹介します。\n\n",
                    "en": lambda c: f"Here are {c} restaurant recommendations.\n\n",
                    "zh": lambda c: f"为您推荐{c}家餐厅。\n\n",
                    "ko": lambda c: f"고객님께 {c}개의 식당을 추천합니다.\n\n",
                }
                intro = shop_messages.get(language, shop_messages["ja"])

                shop_list = []
                for i, shop in enumerate(shops, 1):
                    name = shop.get("name", "")
                    shop_area = shop.get("area", "")
                    description = shop.get("description", "")
                    if shop_area:
                        shop_list.append(
                            f"{i}. **{name}**({shop_area}): {description}"
                        )
                    else:
                        shop_list.append(f"{i}. **{name}**: {description}")

                display_text = intro(len(shops)) + "\n\n".join(shop_list)

                await self._send_json(client_ws, {
                    "type": "shop_cards",
                    "shops": shops,
                    "response": display_text,
                })
                logger.info(
                    f"[LiveRelay] Shop cards sent: {len(shops)} shops, "
                    f"session={session_id}"
                )

                # 1軒目の解説を TTS で非同期に読み上げ（ショップカード送信をブロックしない）
                asyncio.create_task(
                    self._tts_first_shop(shops[0], language, client_ws)
                )
            else:
                await self._send_json(client_ws, {
                    "type": "shop_cards",
                    "shops": [],
                    "response": response_text,
                })

            # Gemini に返す tool_result（短い要約）
            shop_names = [s.get("name", "") for s in shops[:5]]
            return {
                "status": "success",
                "shop_count": len(shops),
                "shop_names": shop_names,
                "message": (
                    f"{len(shops)}件のお店を見つけてショップカードを表示しました。"
                    "1軒目の解説は音声で読み上げ済みです。"
                    "ユーザーにはカードが見えています。"
                    "この後は短く「気になるお店はありますか？」等と聞いてください。"
                    if shops
                    else "条件に合うお店が見つかりませんでした。別の条件を提案してください。"
                ),
            }

        except Exception as e:
            logger.error(
                f"[LiveRelay] Restaurant search error: {e}",
                exc_info=True,
            )
            await self._send_json(client_ws, {
                "type": "shop_cards",
                "shops": [],
                "response": "お店の検索中にエラーが発生しました。",
            })
            return {
                "status": "error",
                "message": f"検索エラー: {type(e).__name__}: {str(e)[:200]}",
            }

    async def _tts_first_shop(
        self, shop: dict, language: str, client_ws: WebSocket
    ) -> None:
        """
        1軒目のお店の解説を TTS で音声化してクライアントに送信（隙間埋め）

        Live API の音声とは別に REST API の TTS を使用。
        フロントエンドは rest_audio タイプで受信し、Live 音声の後に再生する。
        """
        try:
            from google.cloud import texttospeech
        except ImportError:
            logger.warning("[LiveRelay] TTS not available for first shop readout")
            return

        name = shop.get("name", "")
        area = shop.get("area", "")
        description = shop.get("description", "")
        specialty = shop.get("specialty", "")
        rating = shop.get("rating")

        # 読み上げテキストを構築
        tts_parts = {
            "ja": self._build_shop_narration_ja,
            "en": self._build_shop_narration_en,
        }
        builder = tts_parts.get(language, self._build_shop_narration_ja)
        narration = builder(name, area, description, specialty, rating)

        if not narration:
            return

        try:
            tts_client = texttospeech.TextToSpeechClient()
            voice_map = {
                "ja": ("ja-JP", "ja-JP-Chirp3-HD-Leda"),
                "en": ("en-US", "en-US-Chirp3-HD-Leda"),
                "zh": ("cmn-CN", "cmn-CN-Chirp3-HD-Leda"),
                "ko": ("ko-KR", "ko-KR-Chirp3-HD-Leda"),
            }
            lang_code, voice_name = voice_map.get(
                language, voice_map["ja"]
            )

            synthesis_input = texttospeech.SynthesisInput(text=narration)
            voice = texttospeech.VoiceSelectionParams(
                language_code=lang_code, name=voice_name
            )
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                speaking_rate=1.0,
            )

            response = tts_client.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config
            )

            import base64 as b64
            audio_base64 = b64.b64encode(response.audio_content).decode()

            await self._send_json(client_ws, {
                "type": "rest_audio",
                "data": audio_base64,
                "text": narration,
            })
            logger.info(
                f"[LiveRelay] First shop TTS sent: {name}, "
                f"audio_size={len(audio_base64) // 1024}KB"
            )

        except Exception as e:
            logger.warning(f"[LiveRelay] First shop TTS error: {e}")

    @staticmethod
    def _build_shop_narration_ja(
        name: str, area: str, description: str,
        specialty: str, rating: float | None,
    ) -> str:
        """1軒目の日本語ナレーション構築"""
        parts = []
        if name:
            parts.append(f"まず1軒目、{name}です。")
        if area:
            parts.append(f"{area}にあります。")
        if description:
            # 長すぎる場合は切り詰め
            desc = description[:150] if len(description) > 150 else description
            parts.append(desc)
        if specialty:
            parts.append(f"看板メニューは{specialty}です。")
        if rating and rating >= 4.0:
            parts.append(f"評価は{rating}と高評価です。")
        return "".join(parts)

    @staticmethod
    def _build_shop_narration_en(
        name: str, area: str, description: str,
        specialty: str, rating: float | None,
    ) -> str:
        """1軒目の英語ナレーション構築"""
        parts = []
        if name:
            parts.append(f"First up, {name}.")
        if area:
            parts.append(f"Located in {area}.")
        if description:
            desc = description[:150] if len(description) > 150 else description
            parts.append(desc)
        if specialty:
            parts.append(f"Their specialty is {specialty}.")
        if rating and rating >= 4.0:
            parts.append(f"Rated {rating} stars.")
        return " ".join(parts)

    def _build_live_config(self, context: str | None = None) -> dict:
        """
        Live API 設定を構築

        stt_stream.py L410-468 の _build_config() を移植。
        モードプラグインからシステムプロンプトを取得し、
        言語設定と再接続コンテキストを適用する。
        """
        lang_profile = get_language_profile(self.session.language)

        # モードプラグインからシステムプロンプト取得
        system_instruction = self.mode_plugin.get_system_prompt(
            language=self.session.language,
            context=context,
        )

        logger.info(
            f"[LiveRelay] system_instruction: len={len(system_instruction)}, "
            f"preview={system_instruction[:150]!r}"
        )

        config = {
            "response_modalities": ["AUDIO"],
            "system_instruction": types.Content(
                parts=[types.Part(text=system_instruction)]
            ),
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "speech_config": {
                "language_code": lang_profile.live_api_language_code,
            },
            "realtime_input_config": {
                "automatic_activity_detection": {
                    "disabled": False,
                    "start_of_speech_sensitivity": "START_SENSITIVITY_HIGH",
                    "end_of_speech_sensitivity": "END_SENSITIVITY_HIGH",
                    "prefix_padding_ms": 100,
                    "silence_duration_ms": 500,
                }
            },
            "context_window_compression": {
                "sliding_window": {
                    "target_tokens": 32000,
                }
            },
        }

        # モード固有のツール定義
        tools = self.mode_plugin.get_live_api_tools()
        if tools:
            config["tools"] = tools

        return config

    @staticmethod
    async def _send_json(ws: WebSocket, data: dict) -> None:
        """WebSocket に JSON を送信（接続切れを安全に処理）"""
        try:
            import json
            await ws.send_text(json.dumps(data, ensure_ascii=False))
        except Exception:
            pass  # クライアント切断時は無視
