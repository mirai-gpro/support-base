# フロントエンド リップシンク（A2E）仕様書・実装指示書

> **対象**: フロントエンドチーム
> **バージョン**: v2.0
> **最終更新**: 2026-03-06
> **前提**: WebSocket仕様書（FRONTEND_WEBSOCKET_SPEC.md）を先に読んでください

---

## 1. リップシンクとは何か

リップシンク（Lip Sync）とは、**AIの音声に合わせてアバターの口や表情を動かす仕組み**です。

バックエンドの **A2E（Audio to Expression）サービス** が音声データを解析し、
**52次元のARKitブレンドシェイプ**（顔の各パーツの動き）を30fpsのフレームデータとして生成します。

フロントエンドの役割は、このフレームデータを受け取り、
**音声再生と同期しながらアバターの3Dモデルに適用する**ことです。

---

## 2. ARKit ブレンドシェイプとは

ARKit ブレンドシェイプは、Apple が定義した **52個の顔パーツの動き** を表す規格です。
各値は `0.0`（動いていない）〜 `1.0`（最大に動いている）の範囲です。

### 52個のブレンドシェイプ一覧

```
目:
  eyeBlinkLeft, eyeBlinkRight           — まばたき
  eyeLookDownLeft, eyeLookDownRight     — 下を見る
  eyeLookInLeft, eyeLookInRight         — 内側を見る
  eyeLookOutLeft, eyeLookOutRight       — 外側を見る
  eyeLookUpLeft, eyeLookUpRight         — 上を見る
  eyeSquintLeft, eyeSquintRight         — 目を細める
  eyeWideLeft, eyeWideRight             — 目を見開く

口（リップシンクの核心）:
  jawOpen                               — 口を開ける ★最重要
  jawForward, jawLeft, jawRight         — 顎の動き
  mouthClose                            — 口を閉じる
  mouthFunnel                           — 「お」の口
  mouthPucker                           — すぼめる（「う」の口）
  mouthSmileLeft, mouthSmileRight       — 笑顔
  mouthFrownLeft, mouthFrownRight       — への字口
  mouthDimpleLeft, mouthDimpleRight     — えくぼ
  mouthStretchLeft, mouthStretchRight   — 口を横に伸ばす
  mouthRollLower, mouthRollUpper        — 唇を巻く
  mouthShrugLower, mouthShrugUpper      — 唇をすぼめる
  mouthPressLeft, mouthPressRight       — 唇を押す
  mouthLowerDownLeft, mouthLowerDownRight — 下唇を下げる
  mouthUpperUpLeft, mouthUpperUpRight   — 上唇を上げる
  mouthLeft, mouthRight                 — 口を横にずらす

眉:
  browDownLeft, browDownRight           — 眉を下げる
  browInnerUp                           — 眉の内側を上げる
  browOuterUpLeft, browOuterUpRight     — 眉の外側を上げる

頬:
  cheekPuff                             — 頬を膨らます
  cheekSquintLeft, cheekSquintRight     — 頬を上げる

鼻:
  noseSneerLeft, noseSneerRight         — 鼻をしかめる

舌:
  tongueOut, tongueUp                   — 舌を出す/上げる
```

### リップシンクで特に重要なブレンドシェイプ

| 名前 | 役割 | 備考 |
|------|------|------|
| `jawOpen` | 口の開き具合 | **最も重要**。バックエンドで1.8倍にスケール済み |
| `mouthFunnel` | 「お」の形 | 母音の表現 |
| `mouthPucker` | 「う」の形 | 母音の表現 |
| `mouthSmileLeft/Right` | 笑顔 | 感情表現 |
| `mouthClose` | 口を閉じる | 子音「m」「b」「p」 |

---

## 3. WebSocket メッセージ仕様

### `expression` メッセージの構造

```json
{
  "type": "expression",
  "data": {
    "names": [
      "eyeBlinkLeft",
      "eyeBlinkRight",
      "eyeLookDownLeft",
      ...
      "jawOpen",
      ...
      "cheekSquintRight"
    ],
    "frames": [
      [0.0, 0.0, 0.0, ..., 0.15, ..., 0.0],
      [0.0, 0.0, 0.0, ..., 0.22, ..., 0.0],
      [0.0, 0.0, 0.0, ..., 0.18, ..., 0.0],
      ...
    ],
    "frame_rate": 30,
    "chunk_index": 0,
    "is_final": true
  }
}
```

| フィールド | 型 | 説明 |
|-----------|------|------|
| `names` | `string[]` | 52個のブレンドシェイプ名（順序固定） |
| `frames` | `number[][]` | N×52 の2次元配列。Nはフレーム数。各要素は 0.0〜1.0 |
| `frame_rate` | `number` | フレームレート（通常 `30`）。1秒あたりのフレーム数 |
| `chunk_index` | `number` | チャンク番号（現在は常に `0`） |
| `is_final` | `boolean` | このチャンクが最後かどうか（現在は常に `true`） |

### データサイズの目安

| AI発話の長さ | 音声の秒数 | フレーム数 (30fps) | frames配列サイズ |
|-------------|-----------|-------------------|-----------------|
| 短い返答 | ~2秒 | ~60 | 60 × 52 = 3,120 values |
| 通常の返答 | ~5秒 | ~150 | 150 × 52 = 7,800 values |
| 長い返答 | ~10秒 | ~300 | 300 × 52 = 15,600 values |

---

## 4. タイミングと同期

### 4.1 メッセージの到着順序

`audio` と `expression` は **同じターンの完了時にほぼ同時に** 送信されます。

```
[AIターン完了]
  ← transcription (ai, final)   # 1. テキスト確定
  ← audio                       # 2. 音声データ（PCM 24kHz）
  ← expression                  # 3. 表情データ（30fps フレーム列）
```

**重要:** `audio` が先に来る保証があります（`expression` はA2E処理の後に送信されるため）。

### 4.2 音声と表情の同期方法

音声と表情は**同じ音声データから生成**されているため、
**同時に再生を開始すれば自動的に同期**します。

```
音声:    |■■■■■■■■■■■■■■■■■■■■| (5秒)
表情:    |●●●●●●●●●●●●●●●●●●●●| (150フレーム @ 30fps = 5秒)
         ↑
         同時にスタート
```

### 4.3 再生アルゴリズム

```
1. audio メッセージ受信 → 音声バッファに格納（まだ再生しない）
2. expression メッセージ受信 → フレームバッファに格納
3. 両方揃ったら → 音声再生開始 & フレーム再生開始を同時に行う
```

ただし、A2Eサービスが無効の場合（`expression` が来ない場合）もあるため、
以下のフォールバックを実装してください:

```
- audio 受信後、200ms以内に expression が来なければ → 音声のみ再生開始
- expression が後から来たら → 音声の経過時間に合わせてフレームを途中から再生
```

---

## 5. フロントエンド実装ガイド

### 5.1 アーキテクチャ

```
WebSocket受信
    ├── type: "audio"      → AudioPlayer.enqueue(data)
    ├── type: "expression"  → ExpressionPlayer.enqueue(data)
    └── type: "interrupted" → AudioPlayer.stop() + ExpressionPlayer.stop()

AudioPlayer (音声再生)
    └── PCM 24kHz デコード → AudioContext で再生

ExpressionPlayer (表情再生)
    └── フレームキュー → requestAnimationFrame ループで消費
        └── 各フレームの値を 3Dモデルのブレンドシェイプに適用
```

### 5.2 ExpressionPlayer の実装

```typescript
class ExpressionPlayer {
  private frameQueue: number[][] = [];
  private nameToIndex: Map<string, number> = new Map();
  private blendShapeNames: string[] = [];
  private frameRate: number = 30;
  private isPlaying: boolean = false;
  private startTime: number = 0;
  private currentFrameIndex: number = 0;

  /**
   * expression メッセージ受信時に呼ぶ
   */
  enqueue(data: {
    names: string[];
    frames: number[][];
    frame_rate: number;
    chunk_index: number;
    is_final: boolean;
  }): void {
    this.blendShapeNames = data.names;
    this.frameRate = data.frame_rate;

    // names の順序をマップに保存（初回のみ）
    if (this.nameToIndex.size === 0) {
      data.names.forEach((name, index) => {
        this.nameToIndex.set(name, index);
      });
    }

    // フレームをキューに追加
    this.frameQueue.push(...data.frames);
  }

  /**
   * 再生開始（audio の再生開始と同時に呼ぶ）
   */
  play(): void {
    this.isPlaying = true;
    this.startTime = performance.now();
    this.currentFrameIndex = 0;
    this._tick();
  }

  /**
   * 即座に停止（interrupted 時に呼ぶ）
   */
  stop(): void {
    this.isPlaying = false;
    this.frameQueue = [];
    this.currentFrameIndex = 0;
    // アバターを中立表情にリセット
    this._applyNeutralFace();
  }

  /**
   * requestAnimationFrame ループ
   */
  private _tick(): void {
    if (!this.isPlaying) return;

    const elapsed = (performance.now() - this.startTime) / 1000; // 秒
    const targetFrame = Math.floor(elapsed * this.frameRate);

    if (targetFrame < this.frameQueue.length) {
      const frame = this.frameQueue[targetFrame];
      this._applyFrame(frame);
      this.currentFrameIndex = targetFrame;
      requestAnimationFrame(() => this._tick());
    } else {
      // 全フレーム再生完了
      this.isPlaying = false;
      // 最後のフレームを維持するか、中立に戻すかは要件次第
    }
  }

  /**
   * 1フレームの値を3Dモデルに適用
   */
  private _applyFrame(frame: number[]): void {
    // ★★★ ここを各自の3Dエンジンに合わせて実装 ★★★
    //
    // 例: Three.js + VRM の場合
    //   this.blendShapeNames.forEach((name, i) => {
    //     vrmModel.expressionManager.setValue(name, frame[i]);
    //   });
    //
    // 例: Gaussian Splatting の場合
    //   avatar.setBlendShapes(this.blendShapeNames, frame);
    //
    // 例: jawOpen だけ使うシンプル実装
    //   const jawIndex = this.nameToIndex.get("jawOpen");
    //   if (jawIndex !== undefined) {
    //     avatar.setMouthOpen(frame[jawIndex]);
    //   }
  }

  /**
   * 中立表情にリセット
   */
  private _applyNeutralFace(): void {
    const neutralFrame = new Array(this.blendShapeNames.length).fill(0);
    this._applyFrame(neutralFrame);
  }
}
```

### 5.3 音声再生と表情の統合

```typescript
class LiveAudioExpressionSync {
  private audioPlayer: AudioPlayer;
  private expressionPlayer: ExpressionPlayer;
  private pendingAudio: string | null = null;
  private pendingExpression: ExpressionData | null = null;
  private expressionTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(audioPlayer: AudioPlayer, expressionPlayer: ExpressionPlayer) {
    this.audioPlayer = audioPlayer;
    this.expressionPlayer = expressionPlayer;
  }

  /**
   * audio メッセージ受信時
   */
  onAudioReceived(base64Data: string): void {
    this.pendingAudio = base64Data;
    this._tryStartPlayback();

    // expression が来ない場合のフォールバック（200ms）
    this.expressionTimer = setTimeout(() => {
      if (this.pendingAudio) {
        // expression なしで音声のみ再生
        this.audioPlayer.play(this.pendingAudio);
        this.pendingAudio = null;
      }
    }, 200);
  }

  /**
   * expression メッセージ受信時
   */
  onExpressionReceived(data: ExpressionData): void {
    this.pendingExpression = data;
    this.expressionPlayer.enqueue(data);

    if (this.expressionTimer) {
      clearTimeout(this.expressionTimer);
      this.expressionTimer = null;
    }

    this._tryStartPlayback();
  }

  /**
   * 音声と表情が両方揃ったら同時再生開始
   */
  private _tryStartPlayback(): void {
    if (this.pendingAudio && this.pendingExpression) {
      // 同時スタート
      this.audioPlayer.play(this.pendingAudio);
      this.expressionPlayer.play();

      this.pendingAudio = null;
      this.pendingExpression = null;
    }
  }

  /**
   * interrupted 時
   */
  onInterrupted(): void {
    this.audioPlayer.stop();
    this.expressionPlayer.stop();
    this.pendingAudio = null;
    this.pendingExpression = null;
    if (this.expressionTimer) {
      clearTimeout(this.expressionTimer);
      this.expressionTimer = null;
    }
  }
}
```

### 5.4 WebSocket メッセージハンドラとの統合

```typescript
const sync = new LiveAudioExpressionSync(audioPlayer, expressionPlayer);

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);

  switch (msg.type) {
    case "audio":
      sync.onAudioReceived(msg.data);
      break;

    case "expression":
      sync.onExpressionReceived(msg.data);
      break;

    case "interrupted":
      sync.onInterrupted();
      break;

    case "transcription":
      handleTranscription(msg);
      break;

    case "shop_cards":
      handleShopCards(msg);
      break;

    case "rest_audio":
      // AI音声(audio)の再生完了後にキューイング
      audioPlayer.enqueueMp3AfterCurrent(msg.data, msg.text);
      break;

    case "reconnecting":
      showReconnectingUI(msg.reason);
      break;

    case "reconnected":
      hideReconnectingUI();
      break;

    case "error":
      showError(msg.message);
      break;
  }
};
```

---

## 6. rest_audio（店舗TTS）のリップシンク

`rest_audio` は `expression` メッセージを伴いません（MPF音声のため）。

店舗紹介TTS音声のリップシンクが必要な場合は、
**REST API の `/api/v2/rest/tts/synthesize` を使用**してください。
このエンドポイントは音声と同時に表情データも返します。

ただし Live モードでは通常不要です。`rest_audio` の再生中はアバターを
idle 状態（軽いまばたき等）にしておくのが推奨です。

---

## 7. トラブルシューティング

### Q: expression が届かない

**確認事項:**
- A2Eサービスが有効か → `GET /api/v2/health` の `a2e_available` を確認
- `a2e_available: false` の場合、バックエンドの `A2E_SERVICE_URL` 環境変数が未設定

### Q: 口が動かない / 動きが小さすぎる

**確認事項:**
- `jawOpen` の値をログ出力して確認（バックエンドで1.8倍スケール済みで 0.08〜0.15 程度が期待値）
- `_applyFrame()` で正しいブレンドシェイプに値を適用しているか
- 3Dモデルが ARKit ブレンドシェイプに対応しているか

### Q: 音声と口の動きがズレる

**確認事項:**
- `audio` と `expression` の再生開始タイミングが同時か
- `requestAnimationFrame` の代わりに `setInterval` を使っていないか（精度が低い）
- `frame_rate` の値（通常 `30`）を正しく使っているか

### Q: 割り込み時にアバターが固まる

**確認事項:**
- `interrupted` 受信時に `ExpressionPlayer.stop()` を呼んでいるか
- `stop()` で中立表情にリセットしているか

---

## 8. 実装チェックリスト

- [ ] `expression` メッセージのパースと検証
- [ ] `names` 配列から各ブレンドシェイプ名のインデックスマッピング構築
- [ ] `frames` 配列のキューイング
- [ ] `frame_rate` に基づく `requestAnimationFrame` 再生ループ
- [ ] 音声再生との同期開始（`audio` + `expression` 両方揃ったら同時開始）
- [ ] `expression` が来ない場合のフォールバック（200ms待って音声のみ再生）
- [ ] `interrupted` 受信時の即座停止 + 中立表情リセット
- [ ] 3Dモデルへのブレンドシェイプ値適用（`_applyFrame` の実装）
- [ ] アイドル時の自然なアニメーション（まばたき等、表情データがない間）

---

## 付録A: フレームデータの可視化（デバッグ用）

開発中に表情データの内容を確認するための簡易デバッグツール:

```typescript
function debugExpressionFrame(names: string[], frame: number[]): void {
  // 非ゼロの値だけログ出力
  const nonZero = names
    .map((name, i) => ({ name, value: frame[i] }))
    .filter(({ value }) => value > 0.001)
    .sort((a, b) => b.value - a.value);

  console.table(nonZero);
}

// expression 受信時に呼ぶ
// debugExpressionFrame(msg.data.names, msg.data.frames[0]);
```

出力例:
```
┌─────┬──────────────────┬───────┐
│ idx │ name             │ value │
├─────┼──────────────────┼───────┤
│ 0   │ jawOpen          │ 0.180 │
│ 1   │ mouthFunnel      │ 0.120 │
│ 2   │ mouthSmileLeft   │ 0.050 │
│ 3   │ mouthSmileRight  │ 0.048 │
│ 4   │ browInnerUp      │ 0.030 │
└─────┴──────────────────┴───────┘
```

---

## 付録B: 簡易テスト方法

WebSocket接続テストは、ブラウザのDevConsoleで実行可能です:

```javascript
// 1. セッション開始
const res = await fetch("https://YOUR_HOST/api/v2/session/start", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ mode: "gourmet", language: "ja", dialogue_type: "live" })
});
const session = await res.json();
console.log("Session:", session);

// 2. WebSocket接続
const ws = new WebSocket(`wss://YOUR_HOST${session.ws_url}`);

ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  console.log(`[${msg.type}]`, msg);

  if (msg.type === "expression") {
    const jawIdx = msg.data.names.indexOf("jawOpen");
    const jawValues = msg.data.frames.map(f => f[jawIdx]);
    console.log("jawOpen values:", jawValues.slice(0, 10), "...");
    console.log(`frames: ${msg.data.frames.length}, frame_rate: ${msg.data.frame_rate}`);
  }
};

// 3. テキスト送信
ws.send(JSON.stringify({ type: "text", data: "渋谷でおすすめのイタリアンを教えて" }));

// 4. 応答を確認（onmessage でログ出力される）
```
