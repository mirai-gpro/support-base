"""
グルメコンシェルジュ モードプラグイン

GCS/ローカルから読み込んだプロンプトを使用。
Live API / REST 両方の経路で使用。

プロンプトソース:
  - GCS: gs://{PROMPTS_BUCKET_NAME}/prompts/concierge_{lang}.txt
  - ローカル: prompts/concierge_{lang}.txt
  - フォールバック: ハードコードされた最小プロンプト
"""

import logging

from google.genai import types

from support_base.modes.base_mode import BaseModePlugin

logger = logging.getLogger(__name__)

# GCS/ローカルから読み込んだプロンプトを取得
try:
    from support_base.core.support_core import SYSTEM_PROMPTS as LOADED_PROMPTS
    from support_base.core.support_core import INITIAL_GREETINGS as LOADED_GREETINGS
    _PROMPTS_LOADED = True
    logger.info("[GourmetPlugin] GCS/ローカルプロンプト読み込み成功")
except Exception as e:
    logger.warning(f"[GourmetPlugin] プロンプト読み込み失敗 (フォールバック使用): {e}")
    LOADED_PROMPTS = {}
    LOADED_GREETINGS = {}
    _PROMPTS_LOADED = False


class GourmetModePlugin(BaseModePlugin):
    """グルメコンシェルジュモード"""

    @property
    def name(self) -> str:
        return "gourmet"

    @property
    def display_name(self) -> str:
        return "グルメコンシェルジュ"

    @property
    def default_dialogue_type(self) -> str:
        return "live"

    def get_system_prompt(self, language: str = "ja", context: str | None = None) -> str:
        """
        GCS から読み込んだプロンプトを優先使用。
        読み込み失敗時はハードコードのフォールバック。
        """
        prompt = ""
        source = "none"

        # GCS/ローカルから読み込んだプロンプトを使用
        if _PROMPTS_LOADED:
            # Live API では concierge プロンプトを使用
            concierge_prompts = LOADED_PROMPTS.get("concierge", {})
            prompt = concierge_prompts.get(language, concierge_prompts.get("ja", ""))
            if prompt and not prompt.startswith("エラー:"):
                source = "concierge"

            # concierge プロンプトがなければ chat プロンプトを試す
            if not prompt or prompt.startswith("エラー:"):
                chat_prompts = LOADED_PROMPTS.get("chat", {})
                prompt = chat_prompts.get(language, chat_prompts.get("ja", ""))
                if prompt and not prompt.startswith("エラー:"):
                    source = "chat"

        # フォールバック: プロンプトが空、エラーメッセージ、または読み込み失敗時
        if not prompt or prompt.startswith("エラー:"):
            logger.warning("[GourmetPlugin] GCS/ローカルプロンプト使用不可 → フォールバック使用")
            prompt = self._fallback_prompt(language)
            source = "fallback"

        logger.info(
            f"[GourmetPlugin] system_prompt source={source}, "
            f"lang={language}, len={len(prompt)}, "
            f"preview={prompt[:120]!r}"
        )

        # 再接続コンテキスト追加
        if context:
            prompt += f"\n\n【これまでの会話の要約】\n{context}\n"
            prompt += (
                "\n【重要：必ず守ること】\n"
                "1. 直前の話者の発言に対して短い相槌を入れる\n"
                "2. 既に聞いた質問は絶対に繰り返さない\n"
                "3. 会話の流れを自然に引き継ぐ\n"
            )

        return prompt

    def _fallback_prompt(self, language: str) -> str:
        """ハードコードのフォールバック (GCS 読み込み失敗時)"""
        prompts = {
            "ja": (
                "あなたはグルメコンシェルジュAIです。\n"
                "ユーザーのリクエストに対して、即座におすすめのお店を提案してください。\n\n"
                "【絶対厳守ルール ― 必ず従うこと】\n"
                "1. 応答は短く簡潔に。1回の発話は最大3文まで。それ以上は絶対に話さない。\n"
                "2. ユーザーが話し終わるまで待ってから応答する。\n"
                "3. 相手が黙っていても、こちらから一方的に話し続けない。沈黙は許容する。\n"
                "4. 追加の質問（予算は？人数は？雰囲気は？等）は行わず、即座にお店を提案する。\n"
                "5. 長い説明、前置き、余計な装飾は一切不要。要点だけ伝える。\n\n"
                "【対話スタイル】\n"
                "- 親しみやすく、でも丁寧な口調\n"
                "- お店の名前、エリア、特徴を簡潔に伝える\n"
                "- 1回の提案では1〜2軒にとどめる\n\n"
                "【禁止事項】\n"
                "- 一人で長々と話し続けない\n"
                "- 同じ内容を言い換えて繰り返さない\n"
            ),
            "en": (
                "You are a Gourmet Concierge AI.\n"
                "Immediately suggest restaurants when users make a request.\n\n"
                "【Absolute Rules - MUST follow】\n"
                "1. Keep responses SHORT. Maximum 3 sentences per turn. Never exceed this.\n"
                "2. Wait for the user to finish speaking before responding.\n"
                "3. Do NOT keep talking on your own. Silence is OK.\n"
                "4. Do NOT ask follow-up questions (budget, party size, etc.). Suggest immediately.\n"
                "5. No long explanations, preambles, or filler. Only key points.\n\n"
                "【Prohibited】\n"
                "- Talking at length by yourself\n"
                "- Repeating the same content in different words\n"
            ),
            "ko": (
                "당신은 맛집 컨시어지 AI입니다.\n"
                "사용자의 요청에 즉시 맛집을 추천하세요.\n\n"
                "【핵심 규칙】\n"
                "- 사용자가 요청하면 추가 질문 없이 바로 맛집 추천\n"
                "- 예산, 인원, 분위기 등 물어보지 말 것\n"
                "짧고 간결하게 응답하세요 (1-2문장).\n"
            ),
            "zh": (
                "你是一个美食顾问AI。\n"
                "用户提出需求时，立即推荐餐厅。\n\n"
                "【核心规则】\n"
                "- 用户说出需求时，不要追问，直接推荐餐厅\n"
                "- 不要询问预算、人数、氛围等\n"
                "简短回复（1-2句）。\n"
            ),
        }
        return prompts.get(language, prompts["ja"])

    def get_initial_greeting(self, language: str = "ja", user_profile: dict | None = None) -> str:
        """
        初回挨拶。GCS から読み込んだ INITIAL_GREETINGS を優先使用。
        """
        # GCS から読み込んだ挨拶を使用
        if _PROMPTS_LOADED and LOADED_GREETINGS:
            concierge_greetings = LOADED_GREETINGS.get("concierge", {})
            greeting = concierge_greetings.get(language)
            if greeting:
                return greeting

        # フォールバック
        greetings = {
            "ja": "いらっしゃいませ！今日はどんなお食事をお探しですか？",
            "en": "Welcome! What kind of dining experience are you looking for today?",
            "ko": "어서오세요! 오늘은 어떤 식사를 찾고 계신가요?",
            "zh": "欢迎！今天想找什么样的餐厅呢？",
        }
        return greetings.get(language, greetings["ja"])

    def get_live_api_tools(self) -> list:
        """
        Live API 用 Function Calling ツール定義

        search_restaurants ツール:
          Gemini がユーザーのリクエストを受けて呼び出す。
          バックエンドで REST API ロジック（SupportAssistant + enrich_shops_with_photos）を実行し、
          ショップカードをクライアントに送信する。
        """
        search_restaurants = types.FunctionDeclaration(
            name="search_restaurants",
            description=(
                "ユーザーのリクエストに基づいてレストランを検索し、ショップカードを表示する。"
                "ユーザーが食事・レストラン・グルメに関するリクエストをしたら、"
                "追加の質問をせず即座にこのツールを呼び出すこと。"
                "呼び出す前に短い受けのセリフ（1文）を音声で返してからツールを呼ぶ。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "ユーザーのリクエスト内容（例: '渋谷でイタリアン', '新宿で焼肉'）",
                    },
                },
                "required": ["query"],
            },
        )
        return [types.Tool(function_declarations=[search_restaurants])]

    def get_memory_schema(self) -> dict:
        """グルメモード固有の長期記憶スキーマ"""
        return {
            "favorite_cuisines": [],
            "preferred_area": "",
            "budget_range": "",
            "dietary_restrictions": [],
            "past_searches": [],
        }
