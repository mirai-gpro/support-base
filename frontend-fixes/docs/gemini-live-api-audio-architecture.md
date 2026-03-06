# Gemini Live API: 音声アーキテクチャ設計書

> **作成日**: 2026-03-06
> **ソース**: Gemini による推奨パターン + 実装反映
> **対象**: `audio-manager.ts`, `core-controller.ts`, `concierge-controller.ts`

---

## 1. 設計原則（Gemini 推奨）

### 1.1 シングルトンリソース管理

| リソース | ライフサイクル | 理由 |
|----------|---------------|------|
| `AudioContext` | セッション全体で1つ | 作成/破棄の繰り返しはハードウェアリソース解放が追いつかず状態不整合・メモリリーク |
| `MediaStream` | セッション全体で1つ | 毎回 `getUserMedia` するとハードウェア初期化で数秒の遅延、iOS でマイクインジケーター点滅 |
| `AudioWorkletNode` | セッション全体で1つ | `addModule()` は同一 AudioContext に1回のみ。ノード再作成不要 |

### 1.2 半二重制御: フラグ方式

```
❌ 旧方式: stopStreaming() で AudioWorkletNode を disconnect → 再度 startStreaming() で再作成
✅ 新方式: canSendAudio フラグの ON/OFF で送信ゲートを制御（Node は繋ぎっぱなし）
```

### 1.3 VAD（音声区間検出）の役割分担

| 役割 | 担当 | 理由 |
|------|------|------|
| **ターン検知**（喋り終わり→応答生成） | Gemini サーバー側 VAD | 文脈・息継ぎを考慮した高精度検知。クライアントの3.5秒閾値と競合する |
| **帯域節約**（無音スキップ） | クライアント側（オプション） | 無音フレームをWSに送らないことでネットワーク節約。ターン切断はしない |

### 1.4 iOS Safari 受話口問題の解消

**問題**: `getUserMedia()` が有効な状態で `HTMLAudioElement` で音声再生すると、iOS が Audio Session を `PlayAndRecord`（通話モード）と判断し、音声が**受話口 (earpiece)** から小音量で出力される。

**解決策**: AI 音声再生も `HTMLAudioElement` ではなく **Web Audio API** (`AudioBufferSourceNode`) で行う。入力（マイク）と出力（再生）を同一 AudioContext 内で完結させることで、iOS がハンズフリー通話として認識し、スピーカーから出力される。

---

## 2. アーキテクチャ図

```
┌─────────────────── ブラウザ ───────────────────┐
│                                                  │
│  getUserMedia ──→ MediaStreamSource               │
│        │            │                            │
│        │            ▼                            │
│        │      AudioWorkletNode                    │
│        │         (PCM 16kHz)                     │
│        │            │                            │
│        │    canSendAudio?                        │
│        │      │ YES    │ NO                      │
│        │      ▼        ▼                         │
│        │   ws.send()  (破棄)                     │
│        │                                         │
│   AudioContext.destination  ◀─┐                  │
│        │                      │                  │
│        ▼                      │                  │
│     スピーカー          AudioBufferSourceNode     │
│                         (AI音声再生)             │
│                                                  │
└──────────────────────────────────────────────────┘
         ↕ WebSocket (JSON)
┌─────── サーバー ────────┐
│  Python → Gemini Live API │
│  (VAD・ターン検知はGemini)│
└──────────────────────────┘
```

---

## 3. AudioManager クラス設計

### 3.1 Public API

| メソッド | 用途 |
|----------|------|
| `ensureAudioContext()` | AudioContext のシングルトン確保（ユーザージェスチャー時に事前ウォームアップ） |
| `startStreaming(ws, langCode, onStop)` | 初回：シングルトン初期化。2回目以降：フラグONのみ |
| `stopStreaming()` | フラグOFFのみ。Node/Stream は維持 |
| `playPcmAudio(base64, sampleRate?)` | PCM 16-bit LE → Float32 → AudioBufferSourceNode で再生 |
| `playMp3Audio(base64)` | MP3 → decodeAudioData → AudioBufferSourceNode で再生 |
| `stopPlayback()` | 現在の再生を停止 |
| `isPlaying` (getter) | 再生中かどうか |
| `fullResetAudioResources()` | セッション終了時のみ全リソース解放 |
| `unlockAudioParams(element?)` | iOS Audio Session アンロック |
| `startLegacyRecording(onStop)` | MediaRecorder フォールバック（WS不使用時） |

### 3.2 内部フロー

```
初回 startStreaming():
  ensureAudioContext()     → AudioContext 生成（なければ）
  ensureMediaStream()      → getUserMedia（なければ）
  ensureWorkletNode()      → addModule + Node生成（なければ）
  canSendAudio = true      → 送信開始

2回目以降 startStreaming():
  ensureAudioContext()     → 既存を再利用
  ensureMediaStream()      → 既存を再利用（track が live なら）
  ensureWorkletNode()      → 既存を再利用
  canSendAudio = true      → 送信開始（ほぼ即時）

stopStreaming():
  canSendAudio = false     → 送信停止（これだけ）
```

---

## 4. コントローラー側の変更

### 4.1 音声再生パスの統一

```
旧: base64 → WAV Blob → URL.createObjectURL → HTMLAudioElement.play()
新: base64 → Int16 → Float32 → AudioBuffer → AudioBufferSourceNode.start()
```

### 4.2 変更箇所一覧

| ファイル | 旧実装 | 新実装 |
|----------|--------|--------|
| `core-controller.ts` playPcmAudio | WAV Blob + ttsPlayer | audioManager.playPcmAudio() |
| `core-controller.ts` rest_audio | ttsPlayer.src = data:audio/mp3 | audioManager.playMp3Audio() |
| `core-controller.ts` speakTextGCP | ttsPlayer.play() | audioManager.playMp3Audio() |
| `core-controller.ts` stopCurrentAudio | ttsPlayer.pause() | audioManager.stopPlayback() + ttsPlayer.pause() |
| `core-controller.ts` toggleRecording | !ttsPlayer.paused | audioManager.isPlaying |
| `core-controller.ts` _pendingGreetingAudio | data URI 保存 | raw base64 保存 |
| `concierge-controller.ts` playPcmAudioWithAvatar | WAV Blob + ttsPlayer | audioManager.playPcmAudio() |
| `concierge-controller.ts` rest_audio | ttsPlayer.play() | audioManager.playMp3Audio() |
| `concierge-controller.ts` speakTextGCP | ttsPlayer.play() | audioManager.playMp3Audio() |
| `concierge-controller.ts` sendMessage ack | ttsPlayer.play() | audioManager.playMp3Audio() |

### 4.3 ttsPlayer (HTMLAudioElement) の残存理由

`this.ttsPlayer` は LAM Avatar との連携（`lam.setExternalTtsPlayer(this.ttsPlayer)`）のために残しています。実際の音声再生には使用しませんが、LAM Avatar が ttsPlayer のイベントを監視している可能性があるため、互換性のために維持。

---

## 5. iOS 固有の注意事項

### 5.1 AudioContext の suspend/resume

iOS Safari は `AudioContext` をページロード時に `suspended` 状態で作成する。ユーザージェスチャー（click/touchstart）のイベントハンドラー内で `audioContext.resume()` を呼ぶ必要がある。

`enableAudioPlayback()` → `audioManager.ensureAudioContext()` でこれを処理。

### 5.2 MediaStream トラックの維持

iOS ではトラックを停止すると次回の `getUserMedia` でハードウェア初期化が走り、2-5秒の遅延が発生する。`ensureMediaStream()` はトラックが `live` であれば再利用する。

### 5.3 Audio Session カテゴリ

Web Audio API 内で入出力を完結させることで、iOS が Audio Session を適切に管理する。`HTMLAudioElement` を経由しないため、受話口ルーティングの問題が発生しない。

---

## 6. パフォーマンス比較

| 操作 | 旧実装（毎回破棄/再作成） | 新実装（シングルトン） |
|------|--------------------------|----------------------|
| マイク開始（初回） | ~1-3秒（Context + getUserMedia + addModule） | ~1-3秒（同じ） |
| マイク開始（2回目以降） | ~1-3秒（毎回同じ） | **~10ms**（フラグON） |
| マイク停止 | ~100ms（disconnect + close） | **~1ms**（フラグOFF） |
| AI音声再生 | WAV Blob 作成 + HTMLAudioElement | AudioBuffer 変換 + start() |

---

## 7. 今後の検討事項

1. **LAM Avatar 連携**: `setExternalTtsPlayer` が不要になった場合、`ttsPlayer` を完全に削除可能
2. **ストリーミング再生**: AI 音声が複数チャンクで到着する場合、`AudioBufferSourceNode` のキューイング機構が必要
3. **エコーキャンセレーション**: `getUserMedia` の `echoCancellation: true` で Web Audio API 再生のエコーも除去される（検証推奨）
4. **Safari AudioWorklet 互換性**: iOS 14.5+ で AudioWorklet がサポート。それ以前は ScriptProcessorNode へのフォールバックが必要
