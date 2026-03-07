# 設計書作成ガイドライン — AI実装時の仕様乖離を防ぐために

## 背景

AI (LLM) にコード実装を任せる際、以下のパターンで仕様と異なるコードが生成される:

1. **改善バイアス**: AIが「こちらの方が良い」と自己判断で値やロジックを変更する
2. **知識不足の補完**: 新しいAPI (Gemini Live API, A2E等) は学習データに正解がなく、古い事例や一般的パターンで代替する
3. **曖昧な記述の解釈**: 「〜と同様」「適切な値」等をAIが独自に解釈する

**実例**: `LONG_SPEECH_THRESHOLD` を仕様の `80` ではなく `500` で実装された (AIが「500の方が適切」と判断)

---

## 原則

### 原則1: 具体値を書く。参照で逃げない

```
❌ BAD: stt_stream.py と同じ閾値を使用する
❌ BAD: 適切な閾値を設定する
✅ GOOD: LONG_SPEECH_THRESHOLD = 80（固定値。変更不可）
✅ GOOD: MAX_AI_CHARS_BEFORE_RECONNECT = 800（固定値。変更不可）
```

数値、文字列、列挙値はすべて **リテラルで記載**する。
「同じ」「同様」「参照」は禁止。コピペでも良いから実値を書く。

---

### 原則2: 実装コードブロックを埋め込む

新しいAPI・不慣れな技術には、**そのまま使うべきコードブロック**を設計書に含める。

```
❌ BAD: Gemini Live API に接続し、音声ストリーミングを行う

✅ GOOD:
以下のコードをそのまま使用すること（改変禁止）:
```python
# Gemini Live API 接続設定
config = {
    "response_modalities": ["AUDIO"],
    "speech_config": {"language_code": "ja-JP"},
    "realtime_input_config": {
        "automatic_activity_detection": {
            "start_of_speech_sensitivity": "START_SENSITIVITY_HIGH",
            "end_of_speech_sensitivity": "END_SENSITIVITY_HIGH",
            "prefix_padding_ms": 100,
            "silence_duration_ms": 500,
        }
    },
    "context_window_compression": {
        "sliding_window": {"target_tokens": 32000}
    },
}
```\
```

AIは「参考コード」を見ると改善したくなる。**「そのまま使え」と明記**する。

---

### 原則3: 処理順序を番号付きで厳密に書く

```
❌ BAD: レスポンスからtool_call、音声、transcriptionを処理する

✅ GOOD:
Gemini レスポンス処理順序（この順番を厳守すること）:
  1. tool_call の有無を確認 → あれば _handle_tool_call() → continue
  2. server_content を取得 → なければ continue
  3. interrupted フラグ確認 → あれば割り込み処理 → continue
  4. input_transcription 処理
  5. output_transcription 処理
  6. audio data をバッファに蓄積
  7. turn_complete で _on_turn_complete()
```

**「この順番を厳守」**と書かないと、AIは効率化のために順序を変える。

---

### 原則4: 🚨マークで「AIが変えがちな箇所」を明示する

```
🚨 AI実装時の注意: この値は stt_stream.py での本番実績値。
   AIの知識ベースに正解がないため、独自判断で変更しないこと。
   変更が必要な場合は必ず人間に確認すること。

LONG_SPEECH_THRESHOLD = 80  🚨変更禁止
```

過去に乖離が起きた箇所、新技術に関わる箇所には必ず付ける。

---

### 原則5: 「やらないこと」を明記する

AIは「良かれと思って」追加実装する。やらないことを明示的に書く。

```
❌ BAD: (何も書かない → AIが「あった方がいい」と判断して追加)

✅ GOOD:
【やらないこと】
- 音声データのリアルタイムストリーミング送信（turn_complete まで必ずバッファする）
- A2E チャンク分割送信（1ターン=1リクエストで送る）
- 独自のエラーリカバリロジック追加
- パフォーマンス最適化のためのロジック変更
```

---

### 原則6: 移植元コードとの対応表を作る

既存コードからの移植がある場合、**行レベルの対応表**を作る。

```
| 機能 | 移植元 (stt_stream.py) | 移植先 (relay.py) | 検証値 |
|------|----------------------|------------------|--------|
| 再接続閾値 | L372: 80 | settings.LONG_SPEECH_THRESHOLD | == 80 |
| 累積上限 | L371: 800 | settings.MAX_AI_CHARS_BEFORE_RECONNECT | == 800 |
| tool_call検知 | L590: response直下 | _relay_gemini_to_client | server_contentより先 |
| 音声送信 | L620: turn_complete後 | _on_turn_complete | バッファ後一括 |
```

実装後にこの表をチェックリストとして使える。

---

### 原則7: 新技術には「AIの知識が不足している」と宣言する

```
## ⚠️ AI知識不足セクション

以下のAPIは 2024年後半〜2025年にリリースされた新機能であり、
AIの学習データに十分な情報がない可能性が高い。

- Gemini Live API (native audio, BidiGenerateContent)
- audio2exp-service (社内独自サービス)
- Gemini context_window_compression

これらについては:
- 設計書のコードブロックをそのまま使うこと
- ドキュメントURL: (リンク) を参照すること
- 不明点はAIが推測せず、人間に確認すること
```

AIに「お前はこの分野を知らない」と自覚させる。
自覚がないと、自信を持って間違ったコードを書く。

---

### 原則8: 検証条件を実行可能な形で書く

```
❌ BAD: 正しく動作すること

✅ GOOD:
## 検証条件
- assert settings.LONG_SPEECH_THRESHOLD == 80
- assert settings.MAX_AI_CHARS_BEFORE_RECONNECT == 800
- tool_call チェックが server_content チェックより前にあること
- turn_complete まで audio を WebSocket 送信しないこと
- A2E リクエストは 1ターンにつき1回であること
```

可能であればテストコードとして書き、CIで自動検証する。

---

## 設計書テンプレート

```markdown
# [機能名] 設計書

## ⚠️ AI知識不足セクション
(このモジュールで使う新技術・社内固有技術をリストアップ)

## 定数定義 (値の変更禁止)
| 定数名 | 値 | 根拠 |
|--------|-----|------|
| LONG_SPEECH_THRESHOLD | 80 | stt_stream.py 本番実績値 🚨変更禁止 |

## 処理フロー (この順序を厳守)
1. ...
2. ...

## 実装コードブロック (そのまま使用、改変禁止)
```python
# ...
```\

## やらないこと
- ...
- ...

## 移植元対応表
| 機能 | 移植元 | 移植先 | 検証値 |
|------|--------|--------|--------|

## 検証条件
- assert ...
- assert ...
```

---

## まとめ

| AIが起こす問題 | 防止策 |
|--------------|--------|
| 値を「改善」する | 原則1: リテラル記載 + 🚨変更禁止 |
| ロジックを「最適化」する | 原則3: 順序厳守 + 原則5: やらないこと |
| 知らないAPIを推測で実装する | 原則2: コードブロック + 原則7: 知識不足宣言 |
| 移植時に値を変える | 原則6: 対応表 |
| 検証できない | 原則8: assert で検証 |
