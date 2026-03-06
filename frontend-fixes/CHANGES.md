# フロントエンド修正パッチ — 変更点ノート

> **作成日**: 2026-03-06
> **作成者**: バックエンドチーム
> **対象ブランチ**: gourmet-sp2 / claude/test-gourmet-frontend-zM03V
> **元の仕様書**: docs/05-websocket-protocol-fix-spec.md

---

## 概要

仕様書⑤の変更の大半は既に適用済みでしたが、**リップシンクが動かない致命的バグ**を含む
4件の未修正問題を発見・修正しました。

---

## 修正一覧

### BUG1 (致命的): リップシンクが完全に動かない — `applyExpressionFromTts()`

**ファイル**: `concierge-controller.ts` L398
**症状**: アバターの口が一切動かない

**原因**: バックエンドが送信する `expression.frames` は `number[][]`（2次元配列）です。

```
バックエンドが送信するデータ:
frames: [
  [0.0, 0.0, ..., 0.15, ...],   ← number[] (52要素の配列)
  [0.0, 0.0, ..., 0.22, ...],
  ...
]
```

しかし、フロントエンドのコードは `f.weights[i]` でアクセスしていました:

```typescript
// ❌ 修正前: f は number[] なのに f.weights[i] でアクセス → undefined
const frames = expression.frames.map((f: { weights: number[] }) => {
    frame[name] = f.weights[i];  // ← f.weights は undefined → 全値 undefined
});
```

```typescript
// ✅ 修正後: f[i] で直接アクセス
const frames = expression.frames.map((f: number[]) => {
    frame[name] = f[i];  // ← 正しく 0.0〜1.0 の値を取得
});
```

**これがリップシンクが動かなかった根本原因です。**

---

### C5 (重要): 音声入力時の二重応答表示 — `handleStreamingSTTComplete()`

**ファイル**: `core-controller.ts` L664-689
**症状**: 音声入力後に「フォールバック応答」と「バックエンドからの正式応答」が二重表示される

**原因**: `handleStreamingSTTComplete()` 内に、バックエンド応答を待たずに
ローカルで生成するフォールバック応答（`generateFallbackResponse()`）が残っていた。

```typescript
// ❌ 修正前: フォールバック応答を生成 → バックエンドの応答と二重表示
const fallbackResponse = this.generateFallbackResponse(cleanText);
this.addMessage('assistant', fallbackResponse);
// ... さらに3秒後に additionalResponse も追加
```

```typescript
// ✅ 修正後: ack再生後、sendMessage() → WS送信 → バックエンド応答を待つ
if (firstAckPromise) await firstAckPromise;
if (this.els.userInput.value.trim()) {
  this.isFromVoiceInput = true;
  this.sendMessage();
}
```

---

### BUG2 (重要): セッション開始パラメータの不一致

**ファイル**: `core-controller.ts` L479, `concierge-controller.ts` L87-91
**症状**: `user_id` がバックエンドに伝わらない、`dialogue_type` が未指定

**バックエンドが期待するリクエスト**:
```json
{
  "mode": "gourmet",
  "language": "ja",
  "dialogue_type": "live",
  "user_id": "user_123..."
}
```

**core-controller.ts (修正前)**:
```json
{ "user_info": {}, "language": "ja" }
```
→ `mode` 未指定（デフォルトgourmetになるが明示的でない）、`user_id` 未送信、`dialogue_type` 未送信

**concierge-controller.ts (修正前)**:
```json
{ "user_info": { "user_id": "..." }, "language": "ja", "mode": "concierge" }
```
→ `user_id` が `user_info` 内にネストされておりバックエンドに届かない

**修正後（両方）**:
```json
{ "mode": "concierge", "language": "ja", "dialogue_type": "live", "user_id": "user_123..." }
```

---

### BUG3 (軽微): 挨拶文のフィールド名不一致

**ファイル**: `concierge-controller.ts` L104
**症状**: バックエンドからの個人化された挨拶文が表示されない（常にフォールバック）

```typescript
// ❌ 修正前: バックエンドが返すのは "greeting" だが "initial_message" を参照
const greetingText = data.initial_message || this.t('initialGreetingConcierge');

// ✅ 修正後
const greetingText = data.greeting || this.t('initialGreetingConcierge');
```

---

### BUG4 (重要): テキスト入力時に音声が再生されない — `isUserInteracted` フラグ

**ファイル**: `core-controller.ts`, `concierge-controller.ts` の `sendMessage()`
**症状**: テキスト入力（Sendボタン/Enterキー）で送信後、LLMからの音声応答が再生されない

**原因**: `isUserInteracted` フラグが `enableAudioPlayback()` 経由でしか `true` にならず、
この関数はマイクボタン/スピーカーボタンのクリック時にしか呼ばれない。
テキスト入力（Send ボタン/Enter キー）では `isUserInteracted` が `false` のまま残り、
全ての音声再生が `if (this.isUserInteracted)` ガードでスキップされる。

```typescript
// ❌ 修正前: sendMessage() 内で isUserInteracted が有効化されない
protected async sendMessage() {
  this.unlockAudioParams(); // ← isUserInteracted を変更しない
  ...
}
```

```typescript
// ✅ 修正後: sendMessage() 冒頭で isUserInteracted を有効化
protected async sendMessage() {
  this.enableAudioPlayback(); // ← isUserInteracted = true にする
  ...
}
```

---

### BUG5 (重要): 挨拶音声がブラウザ自動再生ポリシーでブロックされる

**ファイル**: `core-controller.ts` の `bindEvents()` + `speakTextGCP()`
**症状**: ページ読み込み後、挨拶音声が無音。`isUserInteracted=false` のまま

**原因**: ブラウザの自動再生ポリシーにより、ユーザーがページを操作（クリック/タッチ/キー入力）
するまで音声再生が許可されない。`enableAudioPlayback()` がマイク/スピーカーボタンのクリック時
にしか呼ばれないため、テキスト入力フィールドをクリックしても音声は有効化されない。

**修正**:
1. `bindEvents()` で document レベルの click/touchstart/keydown リスナーを追加
2. 初回操作時に `enableAudioPlayback()` を呼び出し
3. 挨拶音声を `_pendingGreetingAudio` に保留し、初回操作時に再生

---

### BUG6 (重大): Live API 音声入力の二重処理

**ファイル**: `core-controller.ts`, `concierge-controller.ts` の `handleStreamingSTTComplete()`
**症状**: 音声入力後、Geminiが2回応答を生成 → 1回目の音声が2回目で上書きされる

**原因**: Live API では音声チャンクがリアルタイムで Gemini に送信され、Gemini の VAD
(Voice Activity Detection) がユーザーの喋り終わりを自動検知して応答を生成する。
しかし `handleStreamingSTTComplete()` がユーザーの発言テキストを `sendMessage()` 経由で
再送信していたため、Gemini が同じ内容を2回処理していた。

```
❌ 修正前:
音声→Gemini→応答① → transcription到着 → sendMessage(テキスト再送信) → Gemini→応答②
→ 応答①の音声が応答②で上書きされる

✅ 修正後:
音声→Gemini→応答① → transcription到着 → 表示のみ（再送信しない）
→ 応答①のaudio/expressionをそのまま再生
```

---

### BUG7 (軽微): audio到着時にマイクが停止されない

**ファイル**: `concierge-controller.ts` の `handleWsMessage()` case 'audio'
**症状**: AI音声再生中にマイクが録音したままでエコー/ハウリングの可能性

**修正**: `audio` メッセージ受信時に `if (this.isRecording) this.stopStreamingSTT()` を追加

---

### アーキテクチャ改善: audio-manager.ts 全面書き直し

**ファイル**: `audio-manager.ts`（新規作成 — 旧ファイルを**上書き**）
**設計書**: `docs/gemini-live-api-audio-architecture.md`

Gemini Live API の推奨パターンに基づき、以下の問題を根本解決:

1. **AudioContext/MediaStream/AudioWorkletNode の毎回破棄・再作成**
   → **シングルトン維持** + フラグ制御（`canSendAudio`）に変更
   → マイク開始が初回1-3秒 → 2回目以降 ~10ms に高速化

2. **iOS 受話口問題**（getUserMedia 中に HTMLAudioElement が earpiece ルーティング）
   → 全音声再生を **Web Audio API** (`AudioBufferSourceNode`) に統一
   → 入力/出力が同一 AudioContext 内で完結し、スピーカーから出力

3. **クライアントVAD とサーバーVAD の競合**
   → ターン検知は Gemini サーバー側 VAD に委任
   → クライアント側 VAD はレガシー録音モードのみ使用

4. **40秒初期化遅延**
   → ack TTS プリジェネレーションを fire-and-forget 化
   → UI（マイク/入力）はセッション開始直後に有効化

---

## 適用方法

`frontend-fixes/src/scripts/chat/` 内の **3ファイル** で、
`gourmet-sp2` の `src/scripts/chat/` 内の同名ファイルを**上書き**してください。

```bash
# gourmet-sp2 リポジトリのルートで:
cp -v frontend-fixes/src/scripts/chat/audio-manager.ts src/scripts/chat/
cp -v frontend-fixes/src/scripts/chat/core-controller.ts src/scripts/chat/
cp -v frontend-fixes/src/scripts/chat/concierge-controller.ts src/scripts/chat/
npm run build
```

## 修正後の確認項目

```bash
# 残骸チェック（仕様書の grep 確認をそのまま実行）
grep -rn 'f\.weights' src/scripts/chat/         # → 0件であること
grep -rn 'initial_message' src/scripts/chat/     # → 0件であること
grep -rn 'user_info' src/scripts/chat/           # → 0件であること
grep -rn 'fallbackResponse' src/scripts/chat/core-controller.ts  # → generateFallbackResponse定義のみ
```

### 動作確認（DevTools Console）

以下のログが表示されることを確認:

1. **音声受信**: `[Concierge] WS audio received: XXXXX chars` — 値が0でないこと
2. **音声再生**: `[Concierge] PCM audio play() completed` — 表示されること
3. **音声スキップなし**: `[Concierge] PCM audio SKIPPED` が表示されないこと（表示される場合は `isUserInteracted` の問題）
4. **リップシンク**: `[Concierge] Expression sync: N frames queued` — Nが0でないこと
5. **同期再生**: `[Concierge] Both audio+expression ready, starting synced playback` — 表示されること
6. **挨拶TTS**: `[Concierge] speakTextGCP: audio=XXXXX chars` — 値が0でないこと
7. **マイク制御**: `[AudioManager] Streaming started (flag ON)` / `Streaming stopped (flag OFF)` — フラグ制御で即時切替

### その他の確認

1. **セッション開始**: Network タブで `/api/v2/session/start` のリクエストボディに `mode`, `language`, `dialogue_type`, `user_id` が含まれること。
2. **二重応答なし**: 音声入力後、ack（「はい」等）の後にバックエンドからの応答のみ表示されること。フォールバック応答が出ないこと。
3. **挨拶文**: セッション開始時のレスポンスの `greeting` フィールドの値が画面に表示されること。
