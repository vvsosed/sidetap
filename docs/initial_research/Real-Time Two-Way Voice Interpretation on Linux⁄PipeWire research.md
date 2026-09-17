# Real-Time Two-Way Voice Interpretation on Linux/PipeWire: Survey, Model Landscape, and a Buildable Python Architecture

## TL;DR
- **Build a cascaded pipeline first** (streaming ASR → LLM/MT → streaming TTS) glued into your existing meetscribe codebase, and inject the translated speech through a `pw-loopback`-created `Audio/Source` virtual mic; this is the only path that gives you full control over both directions on Linux today, at ~1.3–3 s end-to-end latency and roughly $0.02–0.05 per conversation-minute per direction.
- **The single-box speech-to-speech APIs (Gemini Live, OpenAI Realtime) are tempting but a poor fit for a literal interpreter**: they are built as conversational *agents* with their own turn-taking/barge-in, ~10-minute WebSocket connection limits, and audio-token billing. gpt-realtime-2.1 costs ~$0.019/min for audio heard + ~$0.077/min for audio spoken ($32/$64 per 1M tokens); Gemini 3.x Flash Live is ~$0.005/min in / ~$0.018/min out. Use Gemini Live only as an experimental "audio-in→translated-audio-out" fast path, prompted as a simultaneous interpreter, with session-resumption to survive the connection cap.
- **For EN↔RU specifically**: Whisper's `translate` task only goes *into* English, so it covers RU→EN but not EN→RU — do not rely on it bidirectionally. Deepgram Nova-3 multilingual, Google Chirp 3, and Speechmatics all handle Russian ASR; ElevenLabs Flash v2.5, Google Chirp 3 HD, and XTTS-v2 all speak Russian for TTS. Realistically your quality ceiling is "good gist with 1.5–3 s lag," not conference-grade simultaneous interpretation.

## Key Findings

1. **Every major calling platform now ships built-in speech-to-speech translation, but Russian coverage in the *voice* (not caption) tier is thin.** Google Meet's Gemini speech translation preserves the speaker's voice; with Gemini 3.5 Live Translate, Google states it "streams speech-to-speech translation across 70-plus languages," expanding Meet "from five English-paired languages to 2,000-plus combinations" (Implicator.ai, Nov 2025) — but Google's own Meet support doc still lists a limited English-anchored set and enforces a **90-minute session cap** and an AI Pro/Ultra subscription. Microsoft Teams' Interpreter agent does bidirectional speech-to-speech with voice simulation but supports exactly nine languages — "Chinese (Mandarin), English, French, German, Italian, Japanese, Korean, Portuguese, Spanish" (Microsoft support doc) — with **Russian excluded**, and needs a Copilot license (20 h/user/mo). Zoom's voice translator beta covers only five languages (English, Chinese, French, Japanese, Spanish). None of these solve your case of arbitrary messengers (Telegram, Discord, Slack huddles, Signal) on Linux — which is exactly why the DIY virtual-mic approach retains its value.

2. **The virtual-microphone injection path — your biggest unknown — is a solved problem with `libpipewire-module-loopback`.** A loopback module with `playback.props.media.class = Audio/Source` presents a normal input device that Zoom/Chrome/Electron enumerate. This is strictly preferred over null-sink/remap tricks for a mic.

3. **A cascaded stack built from best-of-breed streaming components can hit ~1.3–2.0 s glass-to-glass** from central Europe if you pin everything to Frankfurt (europe-west3, ~30 ms RTT from central Europe). The dominant latency terms are your VAD endpointing / simultaneous-translation policy and TTS TTFB, not raw network.

4. **Incremental/simultaneous translation policy is what separates a usable interpreter from a stuttering one.** Use LocalAgreement-2 or AlignAtt (both implemented in UFAL's WhisperLiveKit/SimulStreaming) to commit only stable prefixes, and only send *committed* text to MT+TTS so the synthesized voice never has to "take back" a word.

## Details

### PART 1 — Survey of existing applications and services (2026)

**Built-in calling-platform features**

| Platform | Feature | Speech-to-speech? | EN↔RU voice? | Notes / limits |
|---|---|---|---|---|
| Google Meet | Speech Translation (Gemini 3.5 Live Translate) | Yes, voice-matched | EN↔RU likely (70+ langs, 2,000+ pairs claimed; launched EN↔ES) | 90-min cap; AI Pro/Ultra sub; web only; one subscribed participant suffices |
| Microsoft Teams | Interpreter agent | Yes, with voice simulation | **No — 9 langs, Russian excluded** (zh/EN/FR/DE/IT/JA/KO/PT/ES) | Copilot license; 20 h/user/mo; now in calls incl. PSTN/VoIP; uses Azure AI Speech |
| Zoom | AI Companion captions (46 langs incl. Russian) + Voice Translator beta (5 langs) | Captions yes; voice only 5 langs (no RU) | RU **captions** yes; RU **voice** no | Translated captions ~$5/user/mo add-on; users report poor RU caption quality |
| Webex / Interprefy / KUDO | RSI + AI captions | Human + AI | Yes via human interpreters | Enterprise, not a Linux desktop tool |
| Telegram / WhatsApp / Discord / Signal | — | No live voice translation | — | Not applicable → your use case |

**Standalone / commercial voice-translation products.** DeepL is the most notable mover: it announced **general availability of the DeepL Voice API on Feb 2, 2026** — "This innovative product empowers developers to integrate real-time voice transcription and translation... stream audio and receive transcriptions in the source language, along with translations into up to five target languages" — and launched the **Voice-to-Voice** product suite on April 16, 2026 (virtual meetings, in-person, and an API in early access). DeepL's real-time API uses a two-step flow (`POST /v3/voice/realtime` → ephemeral `wss://` URL), recommends 50–250 ms source chunks, and emits "concluded" (finalized) vs interim segments. Voice preservation, initially promised "by end of 2026," has since shipped per DeepL's Sept 2026 release (voice-to-voice with voice preservation "now generally available in 30+ languages"). Palabra.ai, Camb.ai, Sanas (accent conversion), Transync AI, and JotMe target the same space; most are Windows/web/mobile and closed-source. None are Linux-native desktop apps you can wire into an arbitrary messenger's audio graph.

**Open-source projects (the ones worth studying)**

| Project | What it does | License | Linux | EN↔RU | Latency |
|---|---|---|---|---|---|
| **KoljaB/RealtimeSTT** + **RealtimeTTS** | Turnkey streaming STT (faster-whisper/sherpa) and streaming TTS (Qwen/Coqui/Piper/Kokoro/Azure/Eleven/Orpheus); ships a `translator.py` demo | MIT (engines vary) | Yes | Yes (via engine choice) | STT partials sub-second; TTS TTFA ~80 ms (Qwen, RTX 4090) |
| **QuentinFuxa/WhisperLiveKit** (UFAL research packaged) | Simultaneous ASR with **AlignAtt/SimulStreaming** and **LocalAgreement**, NLLB translation (200 langs), Sortformer diarization, OpenAI/Deepgram-compatible API | Apache-2.0 | Yes | Yes (NLLB) | "order-of-magnitude" lower than whole-utterance Whisper |
| **ufal/SimulStreaming** | Research core: AlignAtt on Whisper large-v3 with beam search, prompting, translation | (research) | Yes | RU ASR yes; translate-into-EN via Whisper | Simultaneous |
| **mohammed-bahumaish/realtime-speech-translation** | Electron app: mic→Deepgram STT→Google Translate→Deepgram TTS→**virtual mic** (BlackHole/VB-Cable) | Open | Cross-platform | Yes | Cascade |
| **kensonhui/Realtime-Speech-to-Speech-Translation** | Client/server Whisper→SpeechT5, pipes to virtual mic for video calls | Open | Yes | RU→EN (Whisper translate) | 1.5 s on A100 |
| **pietropecchi/Voice-to-Screen** | Vosk + GoogleTranslator, PulseAudio `module-remap-source` virtual mic | Open | Yes (Pulse) | Partial | — |
| **AbdullahHendy/live-translation** | WebSocket PCM streaming STT+translation; explicitly documents PipeWire/16 kHz mono capture | Open | Yes (PipeWire) | Yes | — |
| **Meta Seamless** (SeamlessM4T v2 / SeamlessStreaming / Expressive) | End-to-end S2ST/S2TT incl. streaming read/write policy (EMMA) | **CC-BY-NC 4.0 — non-commercial** | Yes (GPU) | **Yes, RU supported** (`tgt_lang="rus"`) | Streaming; needs A100/V100-class, Large >32 GB |

The Seamless license (CC-BY-NC 4.0) is fine for your personal use but blocks any commercial deployment. XTTS-v2 (CPML, non-commercial), F5-TTS (CC-BY-NC), and Fish Speech (CC-BY-NC-SA) share that restriction; Piper (now GPL-3.0 in the `OHF-Voice/piper1-gpl` fork; old MIT weights still usable), Kokoro (Apache-2.0), Chatterbox and Bark (permissive) are commercially safe.

### PART 2 — Model / API landscape for EN↔RU

**(a) Cascaded components**

*Streaming ASR:*

| ASR | Streaming | RU | Latency | Price | Notes |
|---|---|---|---|---|---|
| **Deepgram Nova-3** | Yes (WebSocket) | **Yes; code-switch across 10 langs** — "English, Spanish, French, German, Hindi, Russian, Portuguese, Japanese, Italian, and Dutch" (Deepgram) | ~450 ms median streaming (p95 <300 ms claimed) | ~$0.0077/min mono PAYG; multilingual billed higher (~$0.0092–0.013/min) | Keyterm prompting; March 2026 update cut streaming mean WER ~21%; best latency/price combo |
| **Google Chirp 3 (STT v2)** | Yes (StreamingRecognize) | **ru-RU GA**; uk-UA GA | streaming-grade | per-minute STT v2 | EU multi-region + europe-west2/3/4 |
| **Google Chirp 2** | Yes | Yes | — | — | Supports **speech translation** (asymmetric pairs) |
| **AssemblyAI Universal-Streaming** | Yes | English-strong | low | — | Best English WER (~2.1% clean) |
| **Speechmatics** (real-time + translation) | Yes | Yes | low | — | Real-time translation product; used in production event stacks (VOLO) |
| **OpenAI gpt-4o-transcribe / Whisper** | Semi | RU ASR yes | — | — | Whisper `translate` = **into English only** |
| **faster-whisper (offline)** | via WhisperLiveKit | RU ASR excellent | GPU-bound | free | large-v3-turbo = 4× faster, ~0.3% WER hit |

*MT:*
- **Google Cloud Translation** — NMT text **$20/1M chars** (first 500K free/mo as a $10 credit). **Translation LLM** (Gemini-based): "$10 per million characters input and $10 per million characters output, making it cost equivalent with NMT" (official pricing page). Adaptive LLM translation $25+$25/1M. Custom AutoML models tiered $80/$60/$40/$30 per 1M.
- **Gemini Flash / Flash-Lite** as MT: Flash-Lite ~$0.10/1M input text tokens; very low latency, good for "translate this committed clause" calls, and promptable for register/formality.
- **DeepL** text + new Voice API (transcription + translation combined).
- **Offline**: NLLB-200 (bundled in WhisperLiveKit), Opus-MT — CPU-friendly, lower quality on idiom.

*Streaming TTS (Russian-capable):*

| TTS | RU | TTFB | Price | License/host |
|---|---|---|---|---|
| **ElevenLabs Flash v2.5** | **Yes** — "ultra-low latency (~75ms†) across 32 languages" incl. Russian (ElevenLabs docs) | **~75 ms** (vendor) | $0.05/1K chars (~$0.04/min on agent platforms) | Cloud |
| **Google Chirp 3: HD** | **Yes ru-RU** (30 shared voices, e.g. `ru-RU-Chirp3-HD-Kore`) | bidirectional streaming supported ("low-latency real-time communication using text streaming"); **no official ms figure** | ~$30/1M chars | Cloud (`eu` multi-region; europe-west2 single-region) |
| **Deepgram Aura-2** | **No Russian** (EN/ES/DE/FR/NL/IT/JA only) | sub-200 ms TTFB | — | Cloud — rules it out for RU |
| **Cartesia Sonic / Rime / PlayHT** | varies | low | — | Cloud |
| **Piper (offline)** | Yes (community RU voices) | ~40 ms TTFA, RTF ~0.03 | free | GPL-3.0 fork; robotic |
| **Kokoro (offline)** | limited (8 langs, RU not core) | fast on CPU | free | Apache-2.0 |
| **XTTS-v2 (offline, cloning)** | **Yes (17 langs incl. Russian)**, cross-lingual clone | ~600 ms TTFA | free | CPML non-commercial |

**(b) End-to-end / single-box speech-to-speech**

- **Gemini Live API** (`gemini-2.5-flash-native-audio`, `gemini-3.1-flash-live-preview`, and newer 3.8 Live): native audio S2S over WebSocket via `google-genai` SDK; ~$0.005/min audio in, ~$0.018/min out on 3.x Flash Live (25 audio tokens/sec). **Audio-only sessions cap at 15 min of context and each WebSocket connection is limited to ~10 min** — you must use `session_resumption` (resumption tokens valid ~2 h on the Developer API) + `contextWindowCompression` for longer calls, listening for the `GoAway`/`go_away` message to reconnect. Has affective dialog, proactive turn-taking, barge-in. Can be *prompted* to act as an interpreter, but its agentic turn detection fights literal interpretation.
- **OpenAI Realtime (`gpt-realtime`, `gpt-realtime-2`)**: S2S single model; $32/1M audio-in, $64/1M audio-out. In per-minute terms that is ~$0.019/min heard + ~$0.077/min spoken (600 input tokens/min, 1,200 output tokens/min); the `mini` is $10/$20 per 1M (~$0.016/min). Azure exposes a dedicated **`gpt-realtime-translate`** deployment + `/openai/v1/realtime/translations` flow purpose-built for continuous speech translation — worth evaluating for your exact use case.
- **Meta SeamlessStreaming** — genuine simultaneous S2ST via EMMA read/write policy, RU supported, but CC-BY-NC and GPU-heavy.
- **Whisper translate** — RU→EN only.
- **Kyutai Hibiki, Moshi, Qwen-Omni, Ultravox** — Hibiki is FR→EN focused; not EN↔RU production paths today.

**(c) Voice cloning / preservation**: Google Meet and Teams both do it server-side; open S2S (Seamless Expressive, XTTS-v2 cloning) can too, and DeepL now offers it in its cloud product. For your DIY case it adds latency (XTTS ~600 ms TTFA vs Flash 75 ms) and is not worth it for a v1 — use a fixed high-quality voice per direction.

**Regional/latency note (central Europe)**: Pin everything to **Frankfurt (europe-west3)** — central Europe→Frankfurt is ~30–32 ms RTT (min ~29 ms; WonderNetwork). Amsterdam (europe-west4) ~37 ms. Chirp 3 STT v2 is available in europe-west3; Chirp 3 HD **TTS** single-region is only europe-west2 (London) — use the `eu` multi-region endpoint for TTS. Deepgram/ElevenLabs/OpenAI route via their own EU edge. Frankfurt is the lowest-latency major GCP region for this deployment.

### PART 3 — The PipeWire / Linux audio architecture

**Overall graph (interpreter-style, both directions):**

```
[Messenger app playback]  --(additive pw-link tap)-->  [capture node: remote_en]
                          \--(unchanged)-->  [your headphones, original @ reduced gain]

remote_en (EN) --> ASR --> MT --> TTS(RU) --> [your headphones, RU @ full gain]

[USB mic, RU] --> ASR --> MT --> TTS(EN) --> [tts_out sink] --> loopback --> [VirtMic: Audio/Source] --> Messenger input
```

**1. Capturing the remote party (additive tap).** You already do this. Keep the additive `pw-link` from the app's `Stream/Output/Audio` to your capture node so the user still hears the original. The alternative — routing the app into a `module-null-sink` and monitoring it — steals the audio from the speakers unless you also loopback the null-sink monitor to the real output. Additive `pw-link` is cleaner for "hear original + tap." For persistence in WirePlumber 0.5+, use SPA-JSON `link.rules` under `~/.config/wireplumber/wireplumber.conf.d/`, not 0.4 Lua.

**2. The virtual microphone (the key new piece).** Create a real `Audio/Source` with a loopback module. Drop this in `~/.config/pipewire/pipewire.conf.d/90-translator-mic.conf`:

```
# ~/.config/pipewire/pipewire.conf.d/90-translator-mic.conf
context.modules = [
  { name = libpipewire-module-loopback
    args = {
      node.description = "Translator Virtual Mic"
      capture.props = {
        node.name       = "translator_tts_sink"
        media.class     = Audio/Sink          # apps/pw-cat WRITE here
        audio.position  = [ MONO ]
        audio.rate      = 48000
      }
      playback.props = {
        node.name        = "translator_virtmic"
        node.description = "Translator Virtual Mic"
        media.class      = Audio/Source        # messengers SEE this as a mic
        audio.position   = [ MONO ]
        node.passive     = false
        # target.object left unset so it's a free-standing source
      }
    }
  }
]
```

After `systemctl --user restart pipewire pipewire-pulse`, `wpctl status` shows a sink `translator_tts_sink` and a source `translator_virtmic`. In Zoom/Chrome select "Translator Virtual Mic" as the microphone. Anything written into `translator_tts_sink` appears on the source. (Equivalent one-liner for prototyping: `pw-loopback --capture-props='media.class=Audio/Sink node.name=translator_tts_sink' --playback-props='media.class=Audio/Source node.name=translator_virtmic node.description="Translator Virtual Mic"'`.) Note the community consensus: for a virtual *mic*, loopback modules are the correct tool — null sinks / coupled streams are not.

*Chrome/Electron quirks:* Chrome enumerates PipeWire sources fine but applies its own AEC/AGC/NS by default. Because your virtual mic carries clean TTS, disable Chrome's processing (`chrome://flags` → disable "Chrome-wide echo cancellation", or in Meet/Zoom-web turn off noise suppression) so it doesn't gate/duck the synthetic speech. Give the node a stable, human `node.description` — Chrome shows that string.

**3. Feeding PCM into the virtual sink from Python.** Options ranked:

| Approach | Verdict |
|---|---|
| **`sounddevice` (PortAudio) → PipeWire pulse/ALSA compat**, target `translator_tts_sink` | **Recommended.** Simplest, robust, you already use it; set `blocksize` ~480–960 frames (10–20 ms @ 48 k), `latency='low'`. Select device by matching the node name via `sd.query_devices()`. |
| `pw-cat`/`pw-play --target translator_tts_sink -` stdin pipe | Great for a quick prototype; subprocess adds a small buffer. Note `--target` matches `object.serial`, so resolve it dynamically. |
| `pyaudio` | Works via same compat layer; more boilerplate than sounddevice. |
| `pipewire-python` bindings | Immature; fine for graph queries, not the hot audio path. |
| **GStreamer `appsrc ! audioconvert ! pipewiresink target-object=…`** | Powerful (built-in resampling, clocking) but the `pipewiresink` **audio** path is known-flaky (Collabora's Arun Raghavan: "don't try to share a stream from pipewiresink to pipewiresrc unless you are looking for trouble"; `pulsesrc`/`pulsesink` remain recommended for audio). Only if you already live in GStreamer. |
| JACK bindings | Overkill unless you run a pro-audio graph. |

Use the ALSA/pulse compat with sounddevice; it gives you numpy-native writes and automatic resample.

**4. Echo / feedback control (the hard part).**
- **Don't re-translate your own voice or the RU TTS you hear.** Your mic-direction ASR takes input **only** from the USB mic node, never from any monitor. The RU TTS you hear goes to headphones only — never to a mic-visible node. Use headphones (not speakers) to physically eliminate the RU-TTS→mic path; then no AEC is needed on the mic side.
- **Don't let the EN TTS you inject get re-captured as "remote" audio.** Your remote-capture tap reads the app's *playback* stream (what the remote party sends you), which does **not** contain your injected mic — so the loop is naturally broken. Verify the tap source is the app's `Stream/Output/Audio`, not a full desktop monitor.
- **Interpreter-style ducking:** route the original remote EN to your headphones at ~15–25% gain and the RU TTS at 100%, so you get lip-sync context without competing voices. A `pw-loopback` with controllable volume, or per-stream `wpctl set-volume`, handles this. Duck the original automatically while RU TTS is playing (VAD-gated gain). If you ever must use speakers, add WirePlumber's `module-echo-cancel` on the mic path.

**5. Two-stream timing & simultaneous policy.**
- Keep your Silero/WebRTC VAD gating. For the interpreter, prefer **server-side endpointing where available** (Deepgram `endpointing`, Gemini/Realtime built-in) plus a **stability policy**: only forward **committed** tokens. LocalAgreement-2 emits the longest common prefix of two consecutive hypotheses as confirmed; AlignAtt commits up to the most-attended source frame (the best-performing 2025 policy per UFAL). This prevents the TTS from speaking a word a later revision deletes.
- Chunk sizes: 20 ms capture frames, 200–500 ms ASR partial cadence. Trigger MT+TTS on **sentence/clause boundaries** (VAD pause >~0.5 s or committed clause) rather than every partial, so TTS utterances are stable and don't stutter.
- Because RU↔EN word order differs, do **not** stream sub-sentence into TTS unless you accept re-synth artifacts; clause-level chunking is the sweet spot for EN↔RU.

### End-to-end latency budget (per direction, central Europe → Frankfurt)

| Stage | Cascade (cloud, EU-pinned) | Gemini Live S2S |
|---|---|---|
| Capture + VAD frame | 20–60 ms | 20–60 ms |
| Endpoint / commit wait | 300–800 ms (policy-dependent) | model-internal |
| Network RTT (central Europe→Frankfurt) | ~30 ms each hop | ~30 ms |
| ASR partial→final | 150–450 ms | — |
| MT (Flash-Lite / Translation LLM) | 100–300 ms | — |
| TTS TTFB | 75 ms (Eleven Flash) – ~300 ms (Chirp 3 HD) | — |
| Playback buffer | 20–40 ms | 20–40 ms |
| **Total glass-to-glass** | **~0.9–2.0 s** (practical ~1.3–1.8 s) | **~0.8–1.5 s**, but agentic turn-taking may add |

Sub-second is only realistic if you commit aggressively (short wait) and use ElevenLabs Flash; expect **2–3 s** with conservative endpointing or Chirp 3 HD.

### PART 4 — Python implementation plan

**Architecture:** one `asyncio` event loop; two symmetric `DirectionPipeline` objects (A: remote-EN→RU-to-headphones; B: mic-RU→EN-to-virtmic). Each pipeline = `asyncio.Queue` chain: `capture → vad → asr → commit_policy → mt → tts → audio_out`. Blocking codecs (faster-whisper, resampling) run in `run_in_executor` / worker threads. Backpressure: bounded queues; if TTS falls behind, drop stale partials (RealtimeSTT's `allowed_latency_limit` pattern). This slots directly onto your existing AppTap/pw-link tapping + pluggable STT backends: add an `MT` stage, a `TTS` stage, and a second `AudioSink` targeting `translator_tts_sink`.

**Libraries (current):** `google-genai` (Gemini Live), `google-cloud-speech` (Chirp 3 STT v2), `google-cloud-texttospeech` (Chirp 3 HD streaming), `deepgram-sdk`, `faster-whisper`, `silero-vad`, `sounddevice`, `numpy`, `webrtcvad`; optionally `RealtimeSTT`/`RealtimeTTS` and `whisperlivekit` for the offline path.

**(1) Gemini Live bidirectional session (single-box fast path):**
```python
import asyncio
from google import genai
from google.genai import types

client = genai.Client()  # ADC or API key; use vertexai=True + location="europe-west3" if desired
MODEL = "gemini-2.5-flash-native-audio-preview"  # or a gemini-3.x flash live model

INTERP = ("You are a simultaneous interpreter. Translate English speech you hear into "
          "spoken Russian. Do NOT answer, comment, or add words. Output only the translation. "
          "Preserve tone. Begin translating as soon as a clause is complete.")

async def gemini_interpret(pcm_in_q: asyncio.Queue, pcm_out_q: asyncio.Queue):
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=INTERP,
        session_resumption=types.SessionResumptionConfig(handle=None),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow()),
    )
    async with client.aio.live.connect(model=MODEL, config=cfg) as session:
        async def sender():
            while True:
                chunk = await pcm_in_q.get()               # 16k mono PCM bytes
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))
        async def receiver():
            async for msg in session.receive():
                if msg.data:                                 # 24k PCM out
                    await pcm_out_q.put(msg.data)
                if msg.go_away:                              # ~10-min warning: reconnect w/ handle
                    return
        await asyncio.gather(sender(), receiver())
```
Wrap this in a reconnect loop that stores the `SessionResumptionUpdate` handle and re-`connect`s on `go_away` to survive the ~10-minute connection cap.

**(2) Writing decoded TTS PCM into the virtual mic sink (sounddevice):**
```python
import sounddevice as sd, numpy as np

def find_sink(name="translator_tts_sink"):
    for i, d in enumerate(sd.query_devices()):
        if name in d["name"] and d["max_output_channels"] > 0:
            return i
    raise RuntimeError(f"{name} not found")

class VirtMicWriter:
    def __init__(self, rate=48000):
        self.stream = sd.OutputStream(device=find_sink(), samplerate=rate,
                                      channels=1, dtype="float32",
                                      blocksize=480, latency="low")
        self.stream.start()
    def write(self, pcm_f32: np.ndarray):        # resample TTS output to 48k upstream
        self.stream.write(pcm_f32)
```

**(3) Incremental-translation / stability (LocalAgreement-2):**
```python
class LocalAgreement:
    def __init__(self): self.prev = []; self.committed = ""
    def update(self, hypo_tokens: list[str]) -> str:
        lcp, n = [], min(len(self.prev), len(hypo_tokens))
        for i in range(n):
            if self.prev[i] == hypo_tokens[i]: lcp.append(hypo_tokens[i])
            else: break
        self.prev = hypo_tokens
        already = len(self.committed.split()) if self.committed else 0
        new = lcp[already:]
        if new:
            self.committed = " ".join(lcp)
            return " ".join(new)     # feed only newly-committed words to MT+TTS
        return ""
```
Only newly committed text goes to MT→TTS, so the synthetic voice never revises.

**(4) Handling the remote party interrupting (barge-in):** if new remote speech arrives while RU TTS for the previous utterance is still playing, flush the TTS output queue and duck/stop current playback, then start the new utterance — mirrors Realtime/Live barge-in but under your control on the cascade.

**Deployment / GPU:** Full-offline (faster-whisper large-v3-turbo + a RU-capable TTS) needs a ~8–12 GB GPU for turbo INT8/FP16 plus headroom for TTS; XTTS-v2 adds ~4 GB and ~600 ms TTFA. **A hybrid (offline faster-whisper ASR + cloud MT via Gemini Flash-Lite + cloud TTS via ElevenLabs Flash) is the pragmatic sweet spot**: no per-minute ASR cost, best RU TTS, low latency, and privacy for the audio that stays local until the (text-only) MT call.

### PART 5 — Recommendation

**Build this MVP in a week (hybrid cascade):**
1. **ASR**: reuse your existing backends. Use **Deepgram Nova-3 multilingual** (handles EN and RU with code-switch, ~450 ms, ~$0.0077–0.013/min) as the streaming default; keep **faster-whisper large-v3-turbo** as the offline/free fallback. Do **not** use Whisper translate for EN→RU.
2. **Commit policy**: LocalAgreement-2 on partials (borrow from WhisperLiveKit) → clause-level chunks.
3. **MT**: **Gemini 2.5/3.x Flash-Lite** or **Google Translation LLM** ($10+$10/1M chars), pinned to europe-west3; low latency, good EN↔RU, promptable for register/formality.
4. **TTS**: **ElevenLabs Flash v2.5** (Russian + English, ~75 ms TTFB, $0.05/1K chars) as primary; **Google Chirp 3 HD** (`ru-RU-Chirp3-HD-*`, `eu` endpoint, ~$30/1M chars) as alternate; **Piper** offline fallback.
5. **Audio**: `pw-loopback` `Audio/Source` virtual mic (config above) + `sounddevice` writer; headphones mandatory; interpreter-style ducking of the original at ~20%.

**Expected**: ~1.3–2.0 s per direction glass-to-glass; cost roughly **$0.02–0.05 per minute per active direction** (Nova-3 ASR + Flash-Lite MT + Eleven Flash TTS), i.e. a one-hour bilingual call ≈ $2–5. **Quality ceiling for EN↔RU**: solid conversational gist, correct for most business/social speech; degrades on overlapping speakers, heavy jargon, names (mitigate with keyterm prompting/glossaries), and fast idiomatic exchanges. Half-duplex interpreting is inherently awkward — you'll develop a "speak, pause, let it interpret" cadence.

**When to just use the platform's built-in**: if the call is on **Google Meet** (and you/counterpart have AI Pro/Ultra) or **Teams** with a non-Russian pair, the built-in voice translation will beat your DIY latency and voice-matching. For Russian on Teams (unsupported) or on any messenger without built-in translation (Telegram, Discord, Slack huddles, Signal), your PipeWire virtual-mic app is the answer.

**Second path to prototype in parallel**: a **Gemini Live** single-box interpreter (code above) — lowest engineering effort, native audio, but wrestle with the ~10-min reconnects and agentic turn-taking; and evaluate **Azure `gpt-realtime-translate`**, which is purpose-built for continuous translation.

## Recommendations
1. **Week 1**: stand up the virtual mic + `sounddevice` writer; verify Zoom/Chrome see it and that no echo loop exists (headphones). Ship a one-direction (EN→RU to headphones) cascade using your existing Deepgram/faster-whisper ASR + Flash-Lite MT + ElevenLabs Flash TTS.
2. **Week 2**: add the mic→EN→virtmic direction; add LocalAgreement-2 commit policy and clause chunking; add interpreter ducking and barge-in flushing.
3. **Benchmark thresholds that change the plan**: if measured glass-to-glass >2.5 s, switch MT to Flash-Lite (from Translation LLM), shorten endpoint wait, and pin TTS to Eleven Flash (drop Chirp 3 HD). If per-minute cost matters more than latency, go offline ASR + Piper TTS. If you need voice preservation, evaluate SeamlessExpressive (non-commercial), DeepL's shipped voice-to-voice, or Google Meet's built-in.
4. **Pin all cloud endpoints to europe-west3 (Frankfurt)** for ~30 ms RTT; use the `eu` multi-region for Chirp 3 HD TTS.

## Caveats
- **Whisper `translate` is English-only output** — a common trap for the EN→RU direction; it works for RU→EN only.
- **Deepgram Aura-2 TTS has no Russian** — do not pick it for the RU output voice.
- **Gemini Live / OpenAI Realtime session limits** (audio-only ~15 min context, ~10 min per WebSocket) require session-resumption plumbing for real calls.
- **Seamless / XTTS / F5 / Fish licenses are non-commercial**; fine for personal use, not a product. Piper's active fork is GPL-3.0 (copyleft).
- Specific TTS dollar figures ($30/1M Chirp 3 HD, $16/1M Neural2, etc.) are corroborated across multiple 2026 third-party trackers but Google's live pricing table renders dynamically and should be reconfirmed at build time; **no official numeric TTFB exists for Chirp 3 HD streaming** (Google describes it only as "low-latency").
- Latency figures are city-to-city WonderNetwork ping estimates and vendor-stated TTFBs; measure your real path with gcping / the GCP Network Intelligence Performance Dashboard and end-to-end instrumentation before committing.
- Platform built-ins evolve monthly (Zoom/Teams/Meet language lists especially) — reverify Russian voice support before relying on any. Google's Meet "70+ languages / 2,000+ pairs" figure comes from a 2025 announcement (Implicator.ai); the Meet support doc's enumerated list is narrower, so confirm EN↔RU voice translation is live in your account before a real call.