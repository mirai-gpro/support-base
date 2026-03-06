# フロントエンド WebSocket API 仕様書・実装指示書

> **対象**: フロントエンドチーム
> **バージョン**: v2.0
> **最終更新**: 2026-03-06
> **バックエンドリポジトリ**: support-base

---

## 1. 概要

バックエンドは **純粋な WebSocket** で Live API（音声対話 + テキスト入力）を提供しています。
Socket.IO は使用していません。REST API を叩く必要もありません（テキスト入力含む）。

**全体フロー:**

```
[セッション開始]  POST /api/v2/session/start
       ↓ session_id, ws_url を取得
[WebSocket接続]   WS /api/v2/live/{session_id}
       ↓ 音声・テキストの双方向通信
[セッション終了]  POST /api/v2/session/end
```

---

## 2. セッションライフサイクル

### 2.1 セッション開始（REST）

```
POST /api/v2/session/start
Content-Type: application/json
```

**リクエスト:**
```json
{
  "mode": "gourmet",
  "language": "ja",
  "dialogue_type": "live",
  "user_id": "optional-user-id"
}
```

| フィールド | 型 | 必須 | デフォルト | 説明 |
|-----------|------|------|-----------|------|
| `mode` | string | No | `"gourmet"` | モード名 |
| `language` | string | No | `"ja"` | `ja` / `en` / `ko` / `zh` |
| `dialogue_type` | string | No | `"live"` | `live` / `rest` / `hybrid` |
| `user_id` | string | No | `null` | ユーザー識別子（長期記憶用） |

**レスポンス:**
```json
{
  "session_id": "abc123-def456",
  "mode": "gourmet",
  "language": "ja",
  "dialogue_type": "live",
  "greeting": "いらっしゃいませ！今日はどんなお食事をお探しですか？",
  "ws_url": "/api/v2/live/abc123-def456"
}
```

**重要:**
- `greeting` はUIに表示してください（WebSocket接続前のウェルカムメッセージ）
- `ws_url` は相対パスです。WebSocket接続時にホスト名を付加してください

### 2.2 WebSocket 接続

```javascript
const ws = new WebSocket(`wss://${HOST}${ws_url}`);
```

- WebSocket 接続が成功すると、すぐに音声/テキストの送受信が可能
- 認証はセッションIDで行われる（`session_id` が URL に含まれる）
- セッションが存在しない場合、サーバーは `code=4004` でクローズ
- モードが不正な場合、サーバーは `code=4005` でクローズ

### 2.3 セッション終了（REST）

```
POST /api/v2/session/end
Content-Type: application/json

{ "session_id": "abc123-def456" }
```

WebSocket切断だけでもサーバー側は正常にクリーンアップしますが、
明示的に終了する場合はこのAPIを呼んでください。

---

## 3. WebSocket メッセージプロトコル

すべてのメッセージは **JSON文字列** です。`type` フィールドで種類を判別します。

### 3.1 クライアント → サーバー（送信）

#### `audio` — マイク音声の送信

```json
{
  "type": "audio",
  "data": "<base64エンコードされたPCM 16kHz 16bit モノラル>"
}
```

- **フォーマット**: PCM (Raw), 16kHz, 16bit, モノラル
- **送信間隔**: 連続的に送信（バッファサイズは任意、100ms〜250ms程度推奨）
- マイク入力をリアルタイムにbase64エンコードして送信

#### `text` — テキスト入力の送信

```json
{
  "type": "text",
  "data": "渋谷でイタリアンを探して"
}
```

- テキスト入力も WebSocket 経由で送信（REST API は不要）
- 送信後、音声と同じ経路で `transcription`（AI応答テキスト）と `audio`（AI音声）が返ってくる
- **`data` が空文字の場合は無視される**

#### `stop` — セッション停止

```json
{
  "type": "stop"
}
```

- WebSocket接続を終了する意図をサーバーに通知
- サーバーは `code=1000` で正常クローズ

---

### 3.2 サーバー → クライアント（受信）

#### `audio` — AI音声レスポンス

```json
{
  "type": "audio",
  "data": "<base64エンコードされたPCM 24kHz 16bit モノラル>"
}
```

- **フォーマット**: PCM (Raw), 24kHz, 16bit, モノラル
- **タイミング**: AIの1ターン分の音声がまとめて送信される（turn_complete 時）
- **再生方法**: base64デコード → AudioBuffer (24000Hz) → 再生

**音声再生のサンプル実装:**

```typescript
async function playPcm24kAudio(base64Data: string): Promise<void> {
  const audioCtx = new AudioContext({ sampleRate: 24000 });
  const binaryStr = atob(base64Data);
  const bytes = new Uint8Array(binaryStr.length);
  for (let i = 0; i < binaryStr.length; i++) {
    bytes[i] = binaryStr.charCodeAt(i);
  }

  // PCM 16bit signed → Float32
  const int16 = new Int16Array(bytes.buffer);
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) {
    float32[i] = int16[i] / 32768.0;
  }

  const audioBuffer = audioCtx.createBuffer(1, float32.length, 24000);
  audioBuffer.getChannelData(0).set(float32);

  const source = audioCtx.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(audioCtx.destination);
  source.start();
}
```

#### `transcription` — リアルタイム文字起こし

```json
{
  "type": "transcription",
  "role": "user" | "ai",
  "text": "テキスト断片 or 確定テキスト",
  "is_partial": true | false
}
```

| フィールド | 説明 |
|-----------|------|
| `role` | `"user"` = ユーザーの発話, `"ai"` = AIの応答 |
| `text` | `is_partial: true` の場合は**増分テキスト断片**、`false` の場合は**確定した全文** |
| `is_partial` | `true` = まだ話し中（部分）、`false` = 確定（ターン完了時） |

**受信シーケンス例:**

```
← { type: "transcription", role: "user", text: "渋谷で",      is_partial: true  }
← { type: "transcription", role: "user", text: "イタリアン",    is_partial: true  }
← { type: "transcription", role: "user", text: "渋谷でイタリアン", is_partial: false }  ← 確定
← { type: "transcription", role: "ai",   text: "おすすめの",    is_partial: true  }
← { type: "transcription", role: "ai",   text: "5軒を",       is_partial: true  }
← { type: "transcription", role: "ai",   text: "おすすめの5軒をご紹介します", is_partial: false }  ← 確定
```

**UI表示ルール:**
- `is_partial: true` → テキストを**追記**で表示（バッファに蓄積）
- `is_partial: false` → バッファをクリアして**確定テキストに置換**
- `role: "user"` → ユーザー吹き出しに表示
- `role: "ai"` → AI吹き出しに表示

#### `expression` — アバター表情データ

```json
{
  "type": "expression",
  "data": {
    "names": ["eyeBlinkLeft", "eyeBlinkRight", ..., "jawOpen", ...],
    "frames": [[0.0, 0.0, ..., 0.15, ...], [0.0, 0.0, ..., 0.18, ...], ...],
    "frame_rate": 30,
    "chunk_index": 0,
    "is_final": true
  }
}
```

→ **詳細は別紙「リップシンク仕様書」を参照**

#### `shop_cards` — レストラン検索結果

```json
{
  "type": "shop_cards",
  "shops": [
    {
      "name": "リストランテ ラッセ",
      "area": "六本木",
      "category": "イタリアン",
      "description": "旬の食材を活かした独創的なコース料理が魅力。",
      "rating": 4.5,
      "reviewCount": 150,
      "priceRange": "ランチ2,000円〜、ディナー6,000円〜8,000円",
      "location": "六本木駅徒歩3分",
      "image": "https://...",
      "highlights": ["自家製パスタ", "ソムリエ常駐", "個室あり"],
      "tips": "ランチコースがコスパ良し",
      "specialty": "トリュフリゾット",
      "atmosphere": "モダン・高級感",
      "features": "個室あり、ワインリスト充実",
      "hotpepper_url": "https://...",
      "maps_url": "https://...",
      "tabelog_url": "https://...",
      "gnavi_url": "https://...",
      "tripadvisor_url": "https://...",
      "tripadvisor_rating": 4.0,
      "tripadvisor_reviews": 200,
      "latitude": 35.6762,
      "longitude": 139.6503
    }
  ],
  "response": "ご希望に合うお店を5件ご紹介します。\n\n1. **リストランテ ラッセ**（六本木）: ..."
}
```

- `shops` が空配列の場合 → 検索結果なし（`response` にメッセージが入る）
- `response` はテキスト表示用（マークダウン形式）
- **ショップカード受信時にUIにカードを表示してください**

#### `rest_audio` — 店舗紹介TTS音声

```json
{
  "type": "rest_audio",
  "data": "<base64エンコードされたMP3音声>",
  "text": "まず1軒目、リストランテ ラッセです。六本木にあります。..."
}
```

- `shop_cards` の直後に送信される（1軒目の解説音声）
- **フォーマット**: MP3
- **再生タイミング**: `audio`（AI応答音声）の再生が完了した後に再生
- `text` は音声の内容テキスト（字幕表示用）

**再生のサンプル実装:**

```typescript
async function playMp3Audio(base64Data: string): Promise<void> {
  const audioCtx = new AudioContext();
  const binaryStr = atob(base64Data);
  const bytes = new Uint8Array(binaryStr.length);
  for (let i = 0; i < binaryStr.length; i++) {
    bytes[i] = binaryStr.charCodeAt(i);
  }

  const audioBuffer = await audioCtx.decodeAudioData(bytes.buffer);
  const source = audioCtx.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(audioCtx.destination);
  source.start();
}
```

#### `interrupted` — 割り込み（バージイン）検知

```json
{
  "type": "interrupted"
}
```

- AIが話している最中にユーザーが話し始めた場合に送信される
- **受信時の処理（必須）:**
  1. 現在再生中のAI音声を**即座に停止**
  2. 表情アニメーション（リップシンク）を**即座に停止**
  3. AI transcription バッファをクリア

#### `reconnecting` — サーバー再接続中

```json
{
  "type": "reconnecting",
  "reason": "char_limit" | "long_speech" | "incomplete" | "error"
}
```

| reason | 説明 |
|--------|------|
| `char_limit` | Gemini API のトークン上限に近づいたため再接続 |
| `long_speech` | 長い応答のため再接続 |
| `incomplete` | 応答が途切れたため再接続 |
| `error` | 接続エラーによる再接続（最大3回リトライ） |

- **受信時の処理**: 「接続中...」等のインジケーターを表示
- WebSocket接続自体は維持されたまま（サーバー内部でGemini APIを再接続している）
- 自分でWebSocketを再接続する必要はない

#### `reconnected` — 再接続完了

```json
{
  "type": "reconnected",
  "session_count": 2
}
```

- `session_count`: 通算セッション回数（再接続ごとにインクリメント）
- **受信時の処理**: インジケーターを非表示にし、通常状態に戻す
- 会話のコンテキストはサーバーが引き継いでいるため、フロントエンドでの特別な処理は不要

#### `error` — エラー通知

```json
{
  "type": "error",
  "message": "Gemini connection failed after 3 retries: ..."
}
```

- **受信時の処理**: エラーメッセージをUIに表示
- `reconnecting` の `reason: "error"` が3回続いた後に送信される致命的エラー
- この後サーバーはWebSocketをクローズする

---

## 4. メッセージのタイムライン

典型的な1ターンのメッセージフローを時系列で示します。

### 4.1 音声入力 → AI音声応答

```
[ユーザーが話す]
  → audio (x N)                     # マイク音声を連続送信
  ← transcription (user, partial)   # ユーザー発話の文字起こし（複数回）

[AIが応答を生成]
  ← transcription (ai, partial)     # AI応答テキスト（複数回）

[AIターン完了]
  ← transcription (user, final)     # ユーザー発話 確定
  ← transcription (ai, final)       # AI応答テキスト 確定
  ← audio                           # AI音声（1ターン分まとめて）
  ← expression                      # 表情データ（音声と同時）
```

### 4.2 テキスト入力 → AI応答

```
  → text                            # テキスト送信
  ← transcription (ai, partial)     # AI応答テキスト（複数回）

[AIターン完了]
  ← transcription (ai, final)       # AI応答テキスト 確定
  ← audio                           # AI音声
  ← expression                      # 表情データ
```

### 4.3 レストラン検索フロー

```
  → audio / text                    # 「渋谷でイタリアン」

[Gemini が search_restaurants ツールを呼び出し]
  ← shop_cards                      # 検索結果カード（即座に表示）
  ← rest_audio                      # 1軒目のTTS解説（非同期、shop_cardsの直後）

[Gemini が応答を生成]
  ← transcription (ai, partial)     # 「気になるお店はありますか？」
  ← transcription (ai, final)
  ← audio                           # AI音声
  ← expression                      # 表情データ
```

### 4.4 割り込み（バージイン）

```
  ← transcription (ai, partial)     # AIが話し始める
  ← audio                           # AI音声再生中

  → audio                           # ユーザーが話し始める
  ← interrupted                     # 割り込み検知 → 再生即停止！

  ← transcription (user, partial)   # ユーザーの新しい発話
  ...（通常フローに戻る）
```

---

## 5. 実装チェックリスト

### 必須実装

- [ ] `POST /api/v2/session/start` でセッション開始
- [ ] `WebSocket` 接続（`ws_url` を使用）
- [ ] マイク入力 → PCM 16kHz 16bit モノラル → base64 → `type: "audio"` で送信
- [ ] テキスト入力 → `type: "text"` で送信（REST APIではなくWebSocket経由）
- [ ] `type: "audio"` 受信 → PCM 24kHz をデコードして再生
- [ ] `type: "transcription"` 受信 → `is_partial` に基づくUI更新
- [ ] `type: "shop_cards"` 受信 → カードUI表示
- [ ] `type: "rest_audio"` 受信 → MP3 デコード・再生（AI音声の後）
- [ ] `type: "interrupted"` 受信 → 音声停止 + 表情停止
- [ ] `type: "reconnecting"` / `"reconnected"` 受信 → UI状態更新
- [ ] `type: "error"` 受信 → エラー表示
- [ ] `POST /api/v2/session/end` でセッション終了

### よくある間違い（絶対にやらないこと）

- **テキスト入力を REST API で送らない** — WebSocket の `type: "text"` を使う
- **Socket.IO を使わない** — 純粋な WebSocket
- **音声のサンプルレートを間違えない** — 送信: 16kHz、受信: 24kHz
- **partial transcription を確定テキストとして扱わない** — `is_partial: false` のみが確定
- **WebSocket再接続を自分でやらない** — `reconnecting`/`reconnected` はサーバー内部処理

---

## 6. 補助 REST API リファレンス

| エンドポイント | メソッド | 用途 |
|-------------|--------|------|
| `/api/v2/session/start` | POST | セッション開始 |
| `/api/v2/session/end` | POST | セッション終了 |
| `/api/v2/modes` | GET | 利用可能モード一覧 |
| `/api/v2/health` | GET | ヘルスチェック |

**REST モード（`dialogue_type: "rest"`）の場合のみ使用:**

| エンドポイント | メソッド | 用途 |
|-------------|--------|------|
| `/api/v2/rest/chat` | POST | テキストチャット |
| `/api/v2/rest/tts/synthesize` | POST | TTS + 表情生成 |
| `/api/v2/rest/stt/transcribe` | POST | 音声認識 |

> Live モード（`dialogue_type: "live"`）では上記 REST API は不要です。
> WebSocket のみですべて完結します。
