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

## 適用方法

`frontend-fixes/src/scripts/chat/` 内の3ファイルで、
`gourmet-sp2` の `src/scripts/chat/` 内の同名ファイルを**上書き**してください。

```bash
# gourmet-sp2 リポジトリのルートで:
cp -v frontend-fixes/src/scripts/chat/*.ts src/scripts/chat/
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

### 動作確認（DevTools）

1. **リップシンク**: `expression` メッセージ受信時に `[Concierge] Expression sync: N frames queued` ログが出ること。Nが0でないこと。
2. **セッション開始**: Network タブで `/api/v2/session/start` のリクエストボディに `mode`, `language`, `dialogue_type`, `user_id` が含まれること。
3. **二重応答なし**: 音声入力後、ack（「はい」等）の後にバックエンドからの応答のみ表示されること。フォールバック応答が出ないこと。
4. **挨拶文**: セッション開始時のレスポンスの `greeting` フィールドの値が画面に表示されること。
