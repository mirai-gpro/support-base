# Audio Streaming 総合修正計画書

## 目的
Gemini Live API のリアルタイム音声再生において、現在の `audio-manager.ts` が抱える
**ストリーミング途切れ・割り込み破綻・iOS Safari 固有問題** を解決する。

本ドキュメントは Gemini 3.1 / ChatGPT 5.2 / iOS 専門分析 の3つの情報源を統合し、
**Gemini と ChatGPT に別々にコードを書かせ、Claude Code が最終実装を決定する** ワークフローの基盤とする。

---

## 1. 分析

### 1a) Gemini 案（Gemini 3.1）

#### 核心の指摘
- Live API の音声は **複数の小さなチャンク（数ms〜数百ms）** でストリーミングされる
- 現在の `playPcmAudio()` 内で `this.stopPlayback()` を呼ぶと **新チャンクごとに前の音が消える**
- `scheduledTime` によるギャップレス再生を推奨

#### 推奨パターン
```typescript
private nextStartTime: number = 0;

public async playPcmAudio(base64Data: string, sampleRate: number = 24000): Promise<void> {
  const ctx = await this.ensureAudioContext();
  const float32 = this.convertToFloat32(base64Data);
  const audioBuffer = ctx.createBuffer(1, float32.length, sampleRate);
  audioBuffer.copyToChannel(float32, 0);

  const source = ctx.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(ctx.destination);

  const currentTime = ctx.currentTime;
  const startAt = Math.max(currentTime, this.nextStartTime);
  source.start(startAt);
  this.nextStartTime = startAt + audioBuffer.duration;
}

public stopPlayback() {
  this.nextStartTime = 0;
  // 実行中のSourceNodeを全て止める処理が必要
}
```

#### Gemini固有の追加指摘
- **メモリ管理**: `source.onended` で `disconnect()` し参照クリア必須
- **iOS Safari**: 同時保持できる AudioBuffer/SourceNode に実質上限あり
- **ジッターバッファ**: ネットワーク揺らぎ時は `startAt + 0.1秒` 程度の遅延を検討

#### 評価
- 基本方針は正しいが、**実装が最小限で本番には不十分**
- `stopPlayback()` で全SourceNodeを停止する具体実装がない
- `interrupted` シグナルへの言及が曖昧

---

### 1b) ChatGPT 案（ChatGPT 5.2）

#### 核心の指摘（Geminiと同じ + 追加）
- **`serverContent.interrupted === true`** が API 仕様に存在（公式ドキュメントで確認済み）
- ただし実際に発火しないバグ報告あり → **クライアント側VADフォールバック必須**
- `DataView.getInt16(i, true)` でエンディアン安全に（`Int16Array` はホストエンディアン依存）
- チャンク境界の **クリックノイズ対策**（2-5ms フェード）

#### 推奨パターン
```typescript
class AudioPlaybackQueue {
  private ctx: AudioContext | null = null;
  private nextTime = 0;
  private scheduled: AudioBufferSourceNode[] = [];

  stopAll() {
    for (const s of this.scheduled) {
      try { s.stop(); } catch {}
    }
    this.scheduled = [];
    if (this.ctx) this.nextTime = this.ctx.currentTime;
  }

  async enqueuePcm16leBase64(base64: string, sampleRate = 24000) {
    const ctx = await this.ensure();

    // DataView で LE 指定（エンディアン安全）
    const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
    const n = u8.byteLength / 2;
    const f32 = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const v = dv.getInt16(i * 2, true); // little-endian
      f32[i] = v / 32768;
    }

    const buf = ctx.createBuffer(1, f32.length, sampleRate);
    buf.copyToChannel(f32, 0);

    const t0 = ctx.currentTime;
    if (this.nextTime < t0 + 0.01) this.nextTime = t0 + 0.01;

    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    src.start(this.nextTime);

    this.scheduled.push(src);
    src.onended = () => {
      this.scheduled = this.scheduled.filter(x => x !== src);
    };

    this.nextTime += buf.duration;
  }
}
```

#### ChatGPT固有の追加指摘
- **AudioWorklet リングバッファ方式**: 高頻度チャンク時の GC/ジッタ対策
- **`generation_complete` 待ちの罠**: interrupted 時は `generation_complete` が来ず `turn_complete` が来る
- **状態機械**: `turnComplete / generationComplete / interrupted` を含む設計が必要

#### 評価
- **3つの中で最も正確・具体的**（公式ドキュメントとの照合で確認済み）
- `scheduled` 配列で全SourceNodeを管理 → `stopAll()` が確実
- DataView によるエンディアン安全は堅牢だが、実用上は不要な環境が多い
- `nextTime < t0 + 0.01` のスレッショルドが有用

---

### 1c) iOS 対策案（ChatGPT 5.2 iOS専門回答）

#### P0: AudioContext のバックグラウンド復帰
- iOS Safari はフォアグラウンド離脱で `AudioContext` が `interrupted/suspended` になる
- 復帰後も **自動復帰しない** — 再タップ（ユーザーアクティベーション）を要求されるケースあり
- `resume()` が reject する場合は **AudioContext を作り直す** のが最終手段

##### 推奨実装
```
visibilitychange / pageshow で:
1. await ctx.resume() を試行
2. 失敗なら ユーザー操作で resume させる導線を表示
3. nextPlayTime = ctx.currentTime + smallEpsilon にリセット
4. 再生キューを stop + clear
```

#### P0: Base64 (atob) のメインスレッドブロック
- 40ms間隔 × 25回/秒の `atob + 変換` は iPhone SE 級で **メインスレッド詰まり → 音切れ**
- **最優先**: WebSocket をバイナリフレーム（ArrayBuffer）化、Base64 廃止
- **次善**: 変換処理を Web Worker へ逃がす

#### P1: サイレントスイッチ
- iOS Safari で **Web Audio API がミュートされ得る**（HTMLAudio/video は鳴るのに）
- 回避策: 無音 `<audio>` でメディアチャネルを起こすワークアラウンド
- **実機検証必須**

#### P1: SourceNode 増殖防止
- **先読み制限**: `nextPlayTime - ctx.currentTime > 0.5` なら enqueue 一時停止
- `onended` で `disconnect()` は必須だが **GC が追いつかないケースあり**
- ネットワーク遅延でチャンク一括到着 → node 激増パターンに注意

#### P2: AudioWorklet
- iOS 17+ で基本OK
- `SharedArrayBuffer` は避けて `MessagePort` 経由が無難（WebKit バグ報告あり）
- `ScriptProcessorNode` は deprecated で不推奨

#### P2: マイク入力との同時使用
- `getUserMedia` の `echoCancellation: true` は設定済み（現行コード確認済み）
- **マイク許可開始で出力経路が変わる可能性**（スピーカー → 受話口）
- 半二重（`canSendAudio` フラグ）方針は正しい

---

## 2. 総合修正案

### 現在のコードの問題点サマリ

| # | 問題 | 影響度 | 該当箇所 |
|---|------|--------|----------|
| 1 | `playPcmAudio()` 内で毎回 `stopPlayback()` | **致命的** — チャンクが途切れる | L269 |
| 2 | `currentSource` が1つしか保持されない | **致命的** — 複数チャンク管理不可 | L46 |
| 3 | `atob` + `Int16Array` のメインスレッド負荷 | **高** — iOS低スペック端末で音切れ | L273-278 |
| 4 | バックグラウンド復帰時の AudioContext 復旧なし | **高** — iOS で音が出なくなる | (未実装) |
| 5 | `Int16Array(bytes.buffer)` がホストエンディアン依存 | **低** — 実用上問題なし | L276 |
| 6 | SourceNode のメモリリーク | **中** — 長時間使用で劣化 | L318-326 |

### 修正方針

#### Phase 1: ギャップレス再生（最重要）
- `playPcmAudio()` → **キュー方式**に書き換え
- `stopPlayback()` の毎回呼び出し廃止
- `nextStartTime` + `scheduled: AudioBufferSourceNode[]` で管理
- 先読み制限（0.5秒キャップ）
- `onended` で `disconnect()` + 配列からの除去

#### Phase 2: 割り込み対応
- `interrupted` メッセージ受信 → `stopAll()` + キュークリア
- `core-controller.ts` の `case 'interrupted'` は既に存在（L373-376）
  - 現在は `stopCurrentAudio()` を呼んでいるので、新しい `stopAll()` に繋がるようにする

#### Phase 3: iOS 対策
- `visibilitychange` ハンドラで AudioContext 復帰 + キューリセット
  - `core-controller.ts` L214-250 に既存の `visibilitychange` があるが、AudioContext 復帰が不十分
- `resume()` 失敗時の AudioContext 再生成パス追加

#### Phase 4: パフォーマンス最適化（後続）
- WebSocket バイナリフレーム化（Python 中継 + フロント両方）— **別PR推奨**
- Web Worker での変換処理 — **別PR推奨**

### 修正対象ファイル

| ファイル | 変更内容 |
|---------|---------|
| `audio-manager.ts` | キュー方式再生、stopAll、先読み制限、onended cleanup |
| `core-controller.ts` | visibilitychange でのキューリセット、interrupted 連携強化 |

### 新規プロパティ（audio-manager.ts）

```typescript
// 既存（削除）
private currentSource: AudioBufferSourceNode | null = null;

// 新規（追加）
private scheduledSources: AudioBufferSourceNode[] = [];
private nextPlayTime: number = 0;
private readonly MAX_SCHEDULE_AHEAD = 0.5; // 秒 — 先読み上限
```

### playPcmAudio 新設計（概要）

```
playPcmAudio(base64Data, sampleRate=24000):
  1. ensureAudioContext()
  2. Base64 → Float32 変換（既存ロジック維持、Phase4でWorker化）
  3. AudioBuffer 生成
  4. 先読み制限チェック:
     - nextPlayTime - ctx.currentTime > MAX_SCHEDULE_AHEAD なら待機 or 破棄
  5. nextPlayTime が過去なら currentTime + 0.01 にリセット
  6. source.start(nextPlayTime)
  7. nextPlayTime += buffer.duration
  8. scheduledSources に追加
  9. source.onended → disconnect() + scheduledSources から除去
  10. ★ stopPlayback() は呼ばない
```

### stopAll 新設計（概要）

```
stopAll():
  1. playbackGeneration++ （既存の世代管理維持）
  2. scheduledSources の全要素に stop() + disconnect()
  3. scheduledSources = []
  4. nextPlayTime = 0
  5. _isPlaying = false
```

### core-controller.ts 変更点

```
// 1. case 'audio' — playPcmAudio の then() を修正
//    複数チャンクが来るので、最後のチャンクの onended でのみ isAISpeaking=false にする
//    → turn_complete or generation_complete メッセージで isAISpeaking を制御するのが理想

// 2. case 'interrupted' — stopAll() を呼ぶ（現行の stopCurrentAudio() → 内部で stopAll()）

// 3. visibilitychange — AudioContext 復帰 + キューリセット追加
//    既存: WS再接続 + UI状態リセット
//    追加: audioManager.ensureAudioContext() + audioManager.stopAll()
//    失敗時: audioManager.fullResetAudioResources() → 再生成
```

---

## 3. Gemini / ChatGPT へのコード生成指示

### 共通プロンプト（両方に送る前提）

```
以下の audio-manager.ts を、ストリーミング音声のギャップレス再生に対応するよう修正してください。

【現在のコード】
(audio-manager.ts の全文を貼り付け)

【修正要件】
1. playPcmAudio() をキュー方式に変更:
   - stopPlayback() の毎回呼び出しを廃止
   - nextPlayTime でスケジューリング再生（AudioBufferSourceNode.start(when)）
   - nextPlayTime が過去なら currentTime + 0.01 にリセット
   - 先読み制限: nextPlayTime - currentTime > 0.5 なら enqueue を遅延

2. scheduledSources: AudioBufferSourceNode[] で全ノードを管理:
   - source.onended で disconnect() + 配列から除去
   - stopAll() で全ノード stop() + disconnect() + 配列クリア + nextPlayTime リセット

3. stopPlayback() を stopAll() にリネーム・拡張:
   - 既存の playbackGeneration++ は維持
   - scheduledSources 全停止

4. _isPlaying の管理:
   - playPcmAudio() 呼び出し時に true
   - scheduledSources が空になったら false（最後の onended で判定）

5. iOS Safari 対策:
   - resumeAudioContext() メソッド追加:
     - ctx.resume() を試行
     - 失敗時は AudioContext を close → 再生成
     - nextPlayTime = 0、scheduledSources = []
   - 外部（core-controller.ts の visibilitychange）から呼べるように public

6. 既存の playMp3Audio() は変更しない（MP3は単発再生のため現行ロジックで問題なし）

7. 既存の ensureAudioContext / ensureMediaStream / ensureWorkletNode /
   startStreaming / stopStreaming / fullResetAudioResources は変更しない

8. レガシー録音コード（startLegacyRecording 等）は変更しない

【制約】
- TypeScript で記述
- 外部ライブラリ追加不可
- AudioWorklet リングバッファ方式は今回のスコープ外（Phase 4）
- Base64→Float32 の変換ロジックは既存のまま維持（Phase 4 でバイナリ化）
- コメントは日本語
```

### Gemini への追加指示
```
あなたが以前提案した scheduledTime パターンをベースに、
メモリ管理（onended で disconnect + 参照クリア）を含めた完全な実装を書いてください。
iOS Safari での AudioBuffer/SourceNode の実質上限を考慮し、
先読み制限（0.5秒）を必ず含めてください。
```

### ChatGPT への追加指示
```
あなたが以前提案した AudioPlaybackQueue パターンをベースに、
scheduled 配列による全ノード管理を含めた完全な実装を書いてください。
DataView によるエンディアン安全な変換は、コメントで注記するに留め、
既存の Int16Array パターンを維持してください（実用上問題ないため）。
iOS Safari のバックグラウンド復帰（resumeAudioContext）を public メソッドとして含めてください。
```

---

## 4. 実装テスト計画

### テストシナリオ

| # | テスト | 確認項目 |
|---|--------|---------|
| 1 | 通常会話（短い応答） | 音声が途切れずに再生される |
| 2 | 長い応答（10秒以上） | 複数チャンクがギャップレスで再生される |
| 3 | ユーザー割り込み | 再生が即座に停止し、新しい応答に切り替わる |
| 4 | 連続質問（応答中に次の質問） | 前の応答が停止し、新しい応答が始まる |
| 5 | バックグラウンド→復帰（iOS） | AudioContext が復旧し、次の再生が正常 |
| 6 | 長時間セッション（5分以上） | メモリリークなし（SourceNode が蓄積しない） |
| 7 | ネットワーク遅延シミュレーション | チャンク一括到着時に先読み制限が機能する |

### ログ出力（デバッグ用）

実装に以下のログを含める（本番では削除可能なようにフラグ制御）:

```typescript
private readonly DEBUG_AUDIO = true;

// playPcmAudio 内:
if (this.DEBUG_AUDIO) {
  console.log(`[AudioQueue] enqueue: startAt=${startAt.toFixed(3)}, ` +
    `duration=${buffer.duration.toFixed(3)}, ` +
    `ahead=${(this.nextPlayTime - ctx.currentTime).toFixed(3)}s, ` +
    `scheduled=${this.scheduledSources.length}`);
}

// onended 内:
if (this.DEBUG_AUDIO) {
  console.log(`[AudioQueue] ended: remaining=${this.scheduledSources.length}`);
}

// stopAll 内:
if (this.DEBUG_AUDIO) {
  console.log(`[AudioQueue] stopAll: cleared ${this.scheduledSources.length} nodes`);
}
```

---

## 5. ワークフロー

```
Step 1: 本ドキュメント完成 ← 今ここ
Step 2: Gemini に共通プロンプト + Gemini追加指示を送り、コード生成
Step 3: ChatGPT に共通プロンプト + ChatGPT追加指示を送り、コード生成
Step 4: Claude Code が両方のコードを分析・比較し、最終実装を決定
Step 5: audio-manager.ts に実装、core-controller.ts を修正
Step 6: デバッグログ付きでテスト実行
Step 7: テストログを Gemini / ChatGPT に分析させ、修正案を出させる
Step 8: 最終修正 → 本番デプロイ
```
