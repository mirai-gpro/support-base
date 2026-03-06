# フロントエンド実装引継ぎ書

> **作成日**: 2026-03-06
> **作成元**: バックエンドチーム（support-base リポジトリ）
> **適用先**: `mirai-gpro/gourmet-sp2` リポジトリ
> **対象ブランチ**: `gourmet-sp2` の `claude/test-gourmet-frontend-zM03V` またはメインブランチ

---

## 1. この引継ぎ書について

バックエンドチームが WebSocket プロトコル仕様書（`05-websocket-protocol-fix-spec.md`）を元に
フロントエンドのバグを調査・修正しました。

修正済みファイルと関連ドキュメントはすべて **`support-base` リポジトリ** に格納されています。
フロントエンドチームは本書の手順に従って `gourmet-sp2` リポジトリへ適用・デプロイしてください。

---

## 2. support-base リポジトリの構成

```
support-base/
├── docs/
│   ├── FRONTEND_WEBSOCKET_SPEC.md      ← WebSocket API 完全仕様書（必読）
│   ├── FRONTEND_LIPSYNC_SPEC.md        ← リップシンク（A2E）仕様書（必読）
│   └── FRONTEND_IMPLEMENTATION_HANDOFF.md  ← 本書
└── frontend-fixes/
    ├── CHANGES.md                       ← 修正内容の詳細説明
    └── src/scripts/chat/
        ├── core-controller.ts           ← 修正済み（2件のバグ修正）
        └── concierge-controller.ts      ← 修正済み（3件のバグ修正）
```

---

## 3. 修正内容サマリー（4件）

| ID | 重要度 | ファイル | 内容 |
|----|--------|----------|------|
| **BUG1** | **致命的** | `concierge-controller.ts` | リップシンクが完全に動かない（`f.weights[i]` → `f[i]`） |
| **C5** | 重要 | `core-controller.ts` | 音声入力後の二重応答表示（フォールバック応答の削除） |
| **BUG2** | 重要 | 両ファイル | セッション開始パラメータの不一致（`user_id`, `dialogue_type`） |
| **BUG3** | 軽微 | `concierge-controller.ts` | 挨拶文のフィールド名不一致（`initial_message` → `greeting`） |

**詳細は `frontend-fixes/CHANGES.md` を参照してください。**

---

## 4. 実装手順

### Step 1: support-base リポジトリをクローン

```bash
git clone https://github.com/mirai-gpro/support-base.git
cd support-base
git checkout claude/apply-support-base-fixes-C7e5J
```

### Step 2: 修正内容を確認

```bash
# 変更点ノートを読む
cat frontend-fixes/CHANGES.md

# 修正済みファイルを確認
ls -la frontend-fixes/src/scripts/chat/
# → core-controller.ts
# → concierge-controller.ts
# ★ audio-manager.ts は存在しない（意図的に除外）
```

### Step 3: gourmet-sp2 リポジトリへファイルをコピー

```bash
# gourmet-sp2 リポジトリのルートで実行
cd /path/to/gourmet-sp2

# 2ファイルのみコピー
cp -v /path/to/support-base/frontend-fixes/src/scripts/chat/core-controller.ts \
      src/scripts/chat/core-controller.ts

cp -v /path/to/support-base/frontend-fixes/src/scripts/chat/concierge-controller.ts \
      src/scripts/chat/concierge-controller.ts
```

### ⚠️ 絶対にやってはいけないこと

> **`audio-manager.ts` は絶対にコピー・変更しないでください。**
>
> iPhone 16/17 のマイク・音声制御に関するセキュリティ対策が
> 非常に微妙なバランスで成立しています。
> 変更すると iOS での音声入力が壊れる可能性があります。
>
> `frontend-fixes/` フォルダに `audio-manager.ts` が含まれていないのは意図的です。

### Step 4: ビルド

```bash
npm run build
```

ビルドエラーが出た場合は、型エラーの箇所を確認してください。
修正ファイルは既存の型定義と互換性があるはずです。

### Step 5: 残骸チェック（grep 確認）

ビルド成功後、以下のコマンドで修正漏れがないか確認します：

```bash
# すべて gourmet-sp2 リポジトリのルートで実行

# 1. f.weights パターンが残っていないこと（BUG1 の残骸）
grep -rn 'f\.weights' src/scripts/chat/
# → 0件であること

# 2. initial_message が残っていないこと（BUG3 の残骸）
grep -rn 'initial_message' src/scripts/chat/
# → 0件であること

# 3. user_info が残っていないこと（BUG2 の残骸）
grep -rn 'user_info' src/scripts/chat/
# → 0件であること

# 4. fallbackResponse がコール箇所に残っていないこと（C5 の残骸）
grep -rn 'fallbackResponse' src/scripts/chat/core-controller.ts
# → generateFallbackResponse の定義のみ（呼び出し箇所は0件）
```

**すべての grep チェックをパスしたら、修正は正しく適用されています。**

---

## 5. 動作確認（デプロイ後）

### 5.1 リップシンク（BUG1）

1. ブラウザの DevTools → Console を開く
2. アバターに話しかける（またはテキスト送信）
3. **確認**: `[Concierge] Expression sync: N frames queued` のログが出ること
4. **確認**: `N` が `0` でないこと（通常 10〜50 程度）
5. **確認**: アバターの口が音声に合わせて動くこと

### 5.2 セッション開始パラメータ（BUG2）

1. DevTools → Network タブを開く
2. ページをリロードしてセッションを開始
3. `/api/v2/session/start` リクエストを探す
4. **確認**: リクエストボディに以下の4フィールドが含まれること
   ```json
   {
     "mode": "concierge" または "gourmet",
     "language": "ja",
     "dialogue_type": "live",
     "user_id": "user_xxxxx..."
   }
   ```
5. **確認**: `user_info` でネストされていないこと

### 5.3 二重応答なし（C5）

1. 音声入力モードで話しかける
2. **確認**: ack応答（「はい」等）の後、バックエンドからの応答のみが表示されること
3. **確認**: 「お手伝いできることがあれば…」等のフォールバック応答が出ないこと

### 5.4 挨拶文（BUG3）

1. セッション開始時のレスポンスを DevTools で確認
2. **確認**: レスポンスの `greeting` フィールドの値が画面に表示されること
3. **確認**: デフォルトの固定文言ではなく、パーソナライズされた挨拶が出ること

---

## 6. 参考ドキュメント

以下のドキュメントは `support-base/docs/` にあります。
WebSocket 通信やリップシンクの仕組みを理解する必要がある場合に参照してください。

| ドキュメント | 内容 |
|-------------|------|
| `FRONTEND_WEBSOCKET_SPEC.md` | WebSocket API の完全仕様書。セッションライフサイクル、全メッセージ型、タイムライン図、サンプルコードを含む |
| `FRONTEND_LIPSYNC_SPEC.md` | ARKit 52ブレンドシェイプの説明、A2Eデータ構造、同期方法、ExpressionPlayer 実装例、デバッグツール |
| `CHANGES.md`（frontend-fixes/内） | 今回の4件の修正の詳細な技術的説明（修正前/修正後のコード比較付き） |

---

## 7. トラブルシューティング

### ビルドが通らない場合

- TypeScript の型エラーが出る場合、修正ファイルが正しいディレクトリにコピーされたか確認
- `src/scripts/chat/core-controller.ts` と `src/scripts/chat/concierge-controller.ts` の2ファイルのみが対象

### リップシンクがまだ動かない場合

- DevTools Console で `expression` メッセージが届いているか確認
- `frames` が空配列 `[]` の場合はバックエンド側（A2Eサービス）の問題
- `frames` にデータがあるのに口が動かない場合は、VRM モデルのブレンドシェイプ名を確認

### 音声入力が iOS で動かない場合

- `audio-manager.ts` が変更されていないか確認（`git diff src/scripts/chat/audio-manager.ts`）
- 変更されていた場合は `git checkout src/scripts/chat/audio-manager.ts` で元に戻す

---

## 8. 連絡先

修正内容に関する質問はバックエンドチームまで。
特に以下の場合はすぐに連絡してください：

- grep チェックで想定外の結果が出た場合
- バックエンドのレスポンス形式が仕様書と異なる場合
- A2E（リップシンク）のフレームデータが届かない場合
