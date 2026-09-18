# sidetap — real-time two-way voice interpretation on PipeWire

Status: design, approved 2026-09-17.

## What this is

A Linux desktop interpreter for calls on any messenger — Zoom, Viber, Telegram,
Discord, Slack huddles. The remote party speaks their language and you hear
yours; you speak yours and they hear theirs. Neither side installs anything or
changes platform.

It works by tapping PipeWire rather than integrating with any platform's API,
which is what makes "any messenger" possible. The remote party's audio is
captured from the application's own playback stream; the translated speech
destined for them is injected through a virtual microphone the messenger
selects as an ordinary input device.

This is a cascaded pipeline — streaming ASR → machine translation → streaming
TTS — running twice, once per direction, entirely on Google Cloud: Chirp 3
(Speech-to-Text v2), Cloud Translation v3, and Chirp 3: HD (Text-to-Speech).

Python, managed with `uv`. Linux and PipeWire only.

## Relationship to meetscribe

`sidetap` is a standalone repository with its own `pyproject.toml` and
`uv.lock`. It is not a fork of, and does not depend on, `meetscribe`.

meetscribe already solves sidetap's entire input side, and solves it with
tests: PipeWire graph parsing, the additive per-application tap, `pw-record`
framing, VAD gating, stream rotation, and Chirp 3 streaming recognition. Those
modules are **ported** — copied and adapted, with their tests — rather than
imported. meetscribe's internals were never designed as a public API, and
coupling a live interpreter's release cadence to a transcriber's would serve
neither.

The structural conventions are carried over deliberately, because they are why
meetscribe is testable: every subprocess, socket and clock sits behind a
`Protocol` in `ports.py`, with one real implementation and one fake, and the
suite runs with no audio hardware, no network and no credentials.

## Decisions

These were settled during design. Each records the alternative rejected,
because the rejections carry information the choice alone does not.

### Routing: full replacement

You hear the translation only. The remote party's original voice is ducked to
silence while the translation is speaking and restored in the gaps. They hear
the synthetic voice only; your real microphone is never linked into the call.

*Rejected:* an interpreter-style overlay keeping the originals audible at ~20%
underneath. It preserves lip-sync and "are they still talking" cues, but two
voices competing at a 1.5–3 s offset is a heavier cognitive load than the cues
are worth. A symmetric overlay was rejected more firmly still: your undertone
would reach the remote party roughly two seconds *before* the words it belongs
to.

*Consequence, carried through the rest of this design:* neither party can hear
the other unaided, so pipeline health is not a diagnostic detail — it is the
only thing standing between a conversation and silence. See **Failure
handling**.

### Commit policy: finals only, behind a swappable seam

v1 waits for Chirp 3's `is_final`, translates the complete utterance, and
speaks it. The stage that turns recognition results into translatable units is
a `Segmenter` Protocol, and the pipeline records per-stage latency for every
utterance.

*Rejected:* clause-level LocalAgreement-2 over interim results from day one. It
buys roughly 0.5–1 s, at the cost of more TTS calls on shorter strings and
awkward output wherever word order diverges mid-clause — which for EN↔RU is
often. The research is explicit that this should be measured before it is
built, and the seam plus the latency column in the transcript is what makes
measuring it possible. `LocalAgreementSegmenter` slots in behind the same
Protocol with no other change.

### Backpressure: in-order queue with a lag cap

Utterances are spoken in order and nothing is skipped under normal load. When
the un-spoken backlog exceeds `LAG_CAP_S` (default 12 s), the oldest pending
utterances are dropped — with a visible count in the TUI and a gap marker in
the transcript.

This is not a rare path. Russian renders longer than English for the same
content, so during a monologue the synthetic voice drifts progressively further
behind rather than holding a fixed lag; without a cap it recovers only in
pauses.

*Rejected:* an unbounded queue (a two-minute monologue leaves the translation
answering a question the conversation has moved past) and newest-wins
interruption (lowest lag, but truncates sentences mid-word and loses the most
content).

### Graph ownership: permanent virtual mic, runtime-owned duck path

The virtual microphone is a permanent `~/.config/pipewire/pipewire.conf.d/`
loopback, installed once. The duck path is created per session and restored on
exit.

The virtual mic must be permanent for a reason that is easy to miss: a
runtime-created device has a different identity every session, so Zoom and
Viber lose the saved selection and fall back to your real microphone. The
failure is silent and the symptom is the remote party hearing your
untranslated voice — exactly what the tool exists to prevent.

*Rejected:* creating everything at runtime (the device-identity problem above),
and ducking by turning down the application's own stream volume while touching
nothing else (simpler, but it bets that `pw-link` hands us pre-volume audio —
if it does not, every translation silences the recogniser that has to hear the
next sentence). See **Experiment 1**.

### Interface: Textual TUI, with a headless fallback

A full dashboard, one pane per direction, plus hotkeys. `--no-tui` produces the
same information as plain console logging and is the CI, SSH and debugging
path.

*Rejected:* console-only. Under full replacement you are trusting the pipeline
blind, and the per-stage instrumentation the segmenter seam produces needs
somewhere to be seen.

## Architecture

### Signal flow

```
╔═══ DIRECTION "in"  (them → you) ══════════════════════════════════════════════╗
║                                                                               ║
║  messenger    ──additive pw-link──►  recorder ─► gate ─► ASR ─► segmenter     ║
║  Stream/Output         (tap.py)                          (L1)      │          ║
║       │                                                            ▼          ║
║       │                                                  translate L1→L2      ║
║       │                                                            │          ║
║       ▼                                                            ▼          ║
║  sidetap_duck ──[vol 0% while speaking]──► headphones ◄──────── playout       ║
║                                                          (lag-capped queue)   ║
╚═══════════════════════════════════════════════════════════════════════════════╝

╔═══ DIRECTION "out"  (you → them) ═════════════════════════════════════════════╗
║                                                                               ║
║  USB mic ─► recorder ─► gate ─► ASR ─► segmenter ─► translate L2→L1 ─► TTS    ║
║                                 (L2)                                   │      ║
║                                                                        ▼      ║
║  messenger input ◄── sidetap_virtmic ◄── loopback ◄── sidetap_tts_sink ◄ playout
║   (your real mic is never linked here)      [permanent, from pipewire.conf.d] ║
╚═══════════════════════════════════════════════════════════════════════════════╝
```

Both boxes are the same `DirectionPipeline`. They differ only in source node,
destination sink, source language, target language and voice.

`L1` is the remote party's language (`--their-lang`), `L2` is yours
(`--my-lang`). The channel determines the source language, so nothing is
detected and there is never ambiguity about which way to translate.

### Concurrency: threads, not a single asyncio loop

The initial research proposed one asyncio event loop. This design rejects that.
The ported capture code is subprocess-and-thread shaped, and Google's streaming
SDKs (`StreamingRecognize`, `StreamingSynthesize`) are synchronous generator
APIs. Forcing them into asyncio means executor-wrapping the entire hot path and
rewriting proven code for no benefit.

Per direction: a capture thread, an ASR worker thread, a translate-and-
synthesise worker thread, and a playout thread, joined by bounded
`queue.Queue`s.

Textual owns the main thread's event loop. **The pipeline never calls into the
UI.** It writes to a lock-guarded `Metrics` snapshot which the TUI polls at
10 Hz. This is what makes `--no-tui` and headless tests genuinely work rather
than nominally: the core has no idea a UI exists.

### Modules

Ported from meetscribe, with their tests:

| Module | Role |
|---|---|
| `types.py` | value types and audio constants; imports only stdlib |
| `ports.py` | every Protocol |
| `graph.py` | parses `pw-dump` text into a `PwGraph`; pure, never spawns |
| `recorder.py` | builds `pw-record` argv, frames stdout into fixed blocks |
| `tap.py` | `AppTap` — links a matching application's ports, re-scans every 2 s |
| `capture.py` | owns recorders, queues and capture threads |
| `vad.py` | `SilenceGate` — drops silence, keeps a tail so utterances finalise |
| `rotation.py` | `StreamClock`, `AudioTimeline` |
| `asr.py` | Chirp 3 streaming recognition (was `google.py`) |
| `adapters.py` | the real ports — **the only module that starts a subprocess** |
| `transcript.py` | now bilingual |

New:

| Module | Role |
|---|---|
| `routing.py` | duck loopback lifecycle, re-route, journal, restore |
| `doctor.py` | `sidetap doctor` — environment checks, emits the config file |
| `segment.py` | the `Segmenter` seam; `FinalsOnlySegmenter` for v1 |
| `translate.py` | Cloud Translation v3 adapter |
| `tts.py` | Chirp 3: HD streaming synthesis adapter |
| `playout.py` | lag-capped queue, duck control, PCM writer |
| `pipeline.py` | `DirectionPipeline` — wires one direction's stages |
| `metrics.py` | per-stage latency, queue depth, stage health snapshot |
| `tui.py` | Textual dashboard |
| `cli.py` / `__main__.py` | argparse, wiring, signals, shutdown |

### Ports

Ported: `GraphSource`, `ManagedProcess`, `ProcessLauncher`, `Linker`, `Clock`.

New:

- `Unlinker` — breaks a link by port pair. Needed by the duck re-route and by
  the bypass hotkey.
- `VolumeControl` — `set(node_serial, fraction)`. Wraps `wpctl set-volume`.
  This is how playout silences the original, so it needs a fake like anything
  else.
- `LoopbackFactory` — creates and destroys the duck loopback.
- `Recognizer` — `stream(pcm: Iterator[bytes]) -> Iterator[AsrResult]` (meetscribe's
  `SpeechSession`, renamed).
- `Segmenter` — `feed(result: AsrResult) -> list[Unit]`.
- `Translator` — `translate(text: str, src: str, tgt: str) -> str`.
- `Synthesizer` — `synthesize(text: str, voice: str) -> Iterator[bytes]`.
- `AudioSink` — `write(pcm: bytes)`, `close()`. The long-lived `pw-cat` writer.

### Types

`AudioChunk` is carried over unchanged. Added:

- `Direction` — `IN` (them → you) or `OUT` (you → them).
- `AsrResult` — one recognition result: text, `is_final`, timestamps, confidence.
- `Unit` — a translatable unit emitted by the segmenter: direction, text,
  timestamps. With `FinalsOnlySegmenter` this is one per final `AsrResult`.
- `Translated` — a `Unit` plus target text and MT latency.
- `Record` — one transcript row: direction, source text, target text,
  timestamps, per-stage latency breakdown, dropped flag.

Constants: capture stays 16 kHz / s16 / mono in 100 ms blocks
(`BLOCK_BYTES` 3200), matching meetscribe. TTS output is 24 kHz LINEAR16.
`LAG_CAP_S` defaults to 12, `DEAD_AIR_S` to 6.

## The PipeWire graph

### Virtual microphone (permanent)

Installed once at `~/.config/pipewire/pipewire.conf.d/90-sidetap-mic.conf`, a
`libpipewire-module-loopback` presenting `sidetap_tts_sink` (an `Audio/Sink`
that sidetap writes into) and `sidetap_virtmic` (an `Audio/Source` the
messenger sees as a microphone, with a stable human-readable
`node.description`, which is the string Chrome and Electron display).

`sidetap doctor` writes this file if it is absent and tells the user to
`systemctl --user restart pipewire pipewire-pulse`.

Chrome and Electron apply their own AEC/AGC/noise suppression to microphone
input by default. The virtual mic carries clean synthetic speech, so that
processing can only gate or duck it. `doctor` warns about this and points at
the relevant setting.

### Duck path (per session)

At startup, `routing.py`:

1. creates the duck loopback (`sidetap_duck`), playing into the default sink;
2. unlinks the application's stream from the default sink;
3. links the application's stream into `sidetap_duck`;
4. links the application's stream additively into `sidetap_capture` for ASR.

Step 4 is additive and independent of step 3, which is the whole point: playout
sets the duck node's volume to 0 while speaking, and the recogniser keeps
hearing the remote party at full gain throughout.

### Restoration is a correctness requirement

Step 2 means a crashed process leaves the user with no call audio at all.
`routing.py` journals every change it makes to a state file before making it.
SIGINT and SIGTERM restore. Startup performs an idempotent repair if it finds a
journal from a session that died, and `sidetap doctor` can force the same
repair.

## Pipeline stages

### Capture and VAD

Ported unchanged. `pw-record` is given `--rate/--channels/--format` so PipeWire
does all resampling and downmixing; there is no numpy in the capture path and
none should be added. `SilenceGate` drops silence to cut API cost but lets
`SILENCE_TAIL_BLOCKS` through after speech so utterances finalise.

Audio time is not elapsed time: the gate drops blocks before they are sent, so
recognition offsets count only audio actually delivered, and `AudioTimeline`
maps them back onto real capture times.

### ASR — Chirp 3, Speech-to-Text v2 streaming

Ported nearly intact, including the parts that were learned the hard way:

- **Stream rotation at 240 s.** Google caps `StreamingRecognize` at five
  minutes; without proactive rotation, recognition silently stops mid-call.
  `StreamClock.offset` carries forward so timestamps stay continuous.
- **A 2 s keepalive silence block**, sent from the layer where audio actually
  stops rather than from the gate. Two unrelated things stop the request
  stream — the gate dropping a quiet stretch, and blocks ceasing entirely
  because the tapped application's node went away — and PipeWire does not drive
  a stream with no input, so `pw-record` emits nothing at all rather than
  silence. One timer at the right layer covers both.
- **The retryable/fatal split.** `Unauthenticated`, `PermissionDenied`,
  `InvalidArgument` and `NotFound` are configuration errors that will stay
  wrong; everything else backs off and retries.
- **No word timestamps.** Chirp 3 rejects `enable_word_time_offsets` in
  streaming mode with a fatal `InvalidArgument`.

Changed for sidetap:

- **One language code per direction**, not meetscribe's list.
- **Interim results stay enabled** even though `FinalsOnlySegmenter` discards
  them. They feed the TUI, and that matters more than it sounds: full-
  replacement routing removes your "they are talking right now" signal, and
  live interims on screen are what give it back. LocalAgreement-2 will need
  them later regardless.
- Phrase hints (`--phrase`) are carried over; the research names proper nouns
  as the main quality failure.

### Segmenter

`FinalsOnlySegmenter` emits one `Unit` per final `AsrResult` and discards
interims. That is the entire v1 implementation. Its value is the boundary, not
its contents.

### MT — Cloud Translation v3

One `translateText` call per `Unit`, no batching — batching would add lag for
no benefit at this granularity.

The preferred model is Translation LLM rather than NMT: roughly cost-parity
($10 in + $10 out vs $20 per 1M characters) and better on conversational
register. **Translation LLM's region and language-pair coverage is narrower
than NMT's, and Experiment 3 measured what that costs.** It covers EN↔RU and
EN↔UK, but runs about **195 ms slower** than NMT (238–333 ms against
134–178 ms) — overrunning this spec's own MT budget and eating roughly 13% of
the glass-to-glass target.

It stays the default anyway, per the rule committed before the measurement was
taken: the quality gap is real on idiom, where NMT flattens "не успеваю" ("I
won't manage it in time") into "I don't have time". The model remains a
configuration value with automatic NMT fallback on error, so `--mt-model
general/nmt` buys back ~195 ms whenever latency matters more than register.

Glossaries are out of scope for v1, and are the first thing to add if proper
nouns are mangled in practice.

### TTS — Chirp 3: HD streaming

`StreamingSynthesize`, so playout can begin before the whole utterance is
synthesised. One fixed voice per direction, set by `--voice-in` (the voice you
hear, in `--my-lang`) and `--voice-out` (the voice they hear, in
`--their-lang`). No voice cloning: the research puts it at ~600 ms additional
TTFA for a v1 that does not need it.

### Playout

One long-lived `pw-cat --playback --target=<serial> --rate=24000 --channels=1
--format=s16 -` per direction, fed raw PCM on stdin. PipeWire resamples, which
preserves the "no resampling in Python" invariant and keeps `adapters.py` the
only module that spawns anything.

**Between utterances playout writes silence rather than stopping** — the mirror
of the ASR keepalive. This avoids underrun ambiguity and gives playout exact
knowledge of when it is emitting speech, which is what drives the duck.

Duck control lives here, on the `IN` direction: volume 0 on `sidetap_duck`
while speech is being written, restored when the queue empties.

The lag cap lives here too. Queue depth is measured in seconds of un-spoken
audio; over `LAG_CAP_S` the oldest pending `Translated` items are dropped, the
dropped count is published to `Metrics`, and each dropped unit is written to
the transcript with its `dropped` flag set.

Known limit: dropping the backlog cannot unplay bytes already in the pipe, so
roughly 100–200 ms still emerges after a flush.

## Failure handling

**The two directions fail asymmetrically, and the design leans on that.**

If `IN` dies you hear the remote party's raw, untranslated voice — the duck
only closes while TTS is actively playing, so a dead pipeline leaves it open.
That is a free fail-safe and it is load-bearing; nothing may change the duck to
a default-closed design without replacing it.

If `OUT` dies, they hear silence and neither party knows. You keep talking into
a void. So `OUT` gets a **dead-air detector**: if the microphone VAD reports
speech for more than `DEAD_AIR_S` while nothing has reached the virtual mic,
the TUI pane goes red **and an earcon plays in your headphones** — during a
call you are looking at the other person, not at a dashboard.

Other cases:

- **Fatal errors** (auth, permissions, malformed config, missing API) end the
  session immediately with a clear message, and restore the graph.
- **Network trouble** backs off and retries per the ported policy.
- **Dropped audio is never silent — on both queues, which is not automatic.**
  There are two bounded queues and they fail for different reasons. The
  *playout* queue drops under the lag cap, and those drops are counted into
  `Metrics` and written to the transcript with a `dropped` flag. The *capture*
  queue (`DroppingQueue`) overflows during a network outage, when the
  recogniser stops draining it — and that is precisely meetscribe's documented
  bug, where dropped blocks were logged but left no marker in the output, so a
  lost stretch simply read as nobody talking.

  The playout fix does not cover it: they are different queues, and meetscribe
  had no playout side at all. So the capture queue's drop counters are polled
  into `Metrics` explicitly and shown per direction in the TUI. Without that,
  sidetap reproduces the bug it claims to have fixed, on the exact side it was
  reported against.

## Interface

### Commands

```
sidetap doctor     environment check; writes the virtual-mic config if absent
sidetap devices    sinks, sources, and applications currently playing audio
sidetap run        the interpreter
```

`devices` is ported from meetscribe, including its most important property:
an application does not appear in the PipeWire graph until it actually starts
a stream, so it must be run mid-call. Zoom creates its stream when the meeting
starts, not when the app launches.

`doctor` checks PipeWire ≥ 0.3.60; the presence of `pw-link`, `pw-record`,
`pw-cat`, `pw-loopback` and `wpctl`; the virtual mic's existence; credentials
(`GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT`); and reachability of
all three APIs. One of three APIs not being enabled should fail in a second at
setup, not two minutes into a call. It also performs the graph repair described
under **Restoration**.

A representative run:

```
sidetap run --app zoom --their-lang ru-RU --my-lang en-US \
            --voice-in en-US-Chirp3-HD-Charon \
            --voice-out ru-RU-Chirp3-HD-Kore \
            --phrase "Volodymyr" --phrase "sidetap"
```

Region defaults: **three separate settings, not one.** `europe-west3` for STT,
`global` for Translation, and the `eu` multi-region for TTS. Experiment 3
established that Cloud Translation does not exist in `europe-west3` at all — it
rejects the location outright ("Must be 'us-central1' or 'global'") — while
Speech-to-Text accepts it. Frankfurt is likewise not available as a TTS
single-region. `us-central1` measured marginally faster than `global` for
Translation and is one flag away.

### TUI

One pane per direction showing source language, live interim text dimmed,
committed final text bright, the translation beneath it, queue depth, current
lag in seconds, and health indicators for ASR, MT and TTS. A footer carries
session time, running cost estimate, dropped-utterance count, and the hotkeys.

Hotkeys: **bypass**, **mute outbound**, **drop backlog**, **quit**.

**Bypass** is the escape hatch full replacement otherwise removes, and it does
three things together, not one: restores the duck to full gain, links your real
microphone into `sidetap_tts_sink`, and **suppresses playout on both
directions**. All three are required — without the third, translated speech
would talk over the unmediated conversation it was meant to step out of.
Recognition keeps running while bypassed, so the transcript stays continuous
and toggling back does not restart a stream. Toggling off reverses all three.

Hotkeys are TUI-only; `--no-tui` has none. That mode exists for CI, SSH and
debugging, and keeping it input-free keeps its surface small.

### Transcript

Bilingual JSONL, appended and flushed per finalised utterance so an unclean
exit keeps everything, plus Markdown rendered at close. Each record carries
direction, source text, target text, timestamps, the per-stage latency
breakdown, and the dropped flag.

That latency column is the evidence for deciding whether LocalAgreement-2 is
worth building.

## Testing

Mirrors meetscribe. Every Protocol has exactly one real implementation and one
fake in `tests/conftest.py`, and the suite runs with no audio hardware, no
network and no credentials.

Two additions:

- `docs/manual-smoke.md` — a checklist of what tests structurally cannot
  verify: that ducking actually silences the original without starving the
  recogniser, that Zoom and Viber enumerate and select the virtual mic, that
  the remote party hears intelligible speech, measured glass-to-glass latency,
  and a 30-minute call crossing several stream rotations.
- A scrubbed real `pw-dump` fixture, as in meetscribe. Hand-written fixtures
  are written from the same assumptions as the parser, so they share its bugs
  and cannot catch a wrong assumption; a real dump can. It must be scrubbed of
  username, hostname, machine-id, pids and device serials, with a test
  guarding the scrub.

## Experiments to run before implementing

These are measurements, not open design questions. The design below is settled
either way; the results determine how much of it can be simplified.

**Experiment 1 — is the additive tap pre- or post-volume?** With an application
playing and a meetscribe-style additive tap in place, set the application's
stream volume to 0 with `wpctl set-volume` and measure RMS arriving at the
capture node.

The duck-node design specified above is correct regardless, and is what gets
built. But if the tap proves pre-volume, `routing.py` collapses to "set the
application's stream volume" — no unlink, no re-route, no journal, no crash
repair. That is a large enough simplification to be worth one measurement, and
it is recorded as a follow-up rather than a branch in this spec.

**Experiment 2 — does `pw-cat --playback` from a stdin pipe hold up?** Verify
that continuously written silence produces no buffer growth or drift across
30 minutes, and measure the residual audio that emerges after a flush.

**Experiment 3 — Translation LLM coverage.** Confirm whether the model is
available for the target language pair, and measure its latency against NMT on
representative utterances. **Answered: `europe-west3` is not a valid
Translation location at all**, both models work for EN↔RU and EN↔UK from
`global`/`us-central1`, and Translation LLM costs ~195 ms over NMT.

## Latency and cost

Per direction, finals-only. STT in Frankfurt; Translation in `global` or
`us-central1`, which is why its share is larger than originally budgeted:

| Stage | Budget |
|---|---|
| capture + VAD frame | 20–60 ms |
| endpoint / final wait | 300–800 ms |
| ASR final | 150–450 ms |
| MT | 240–330 ms measured (Translation LLM); 135–180 ms (NMT) |
| TTS TTFB | ~300 ms |
| playout buffer | ~40 ms |
| **total, after they stop speaking** | **1.1–2.0 s** (Translation LLM); **0.9–1.8 s** (NMT) |

Cost lands near $0.02–0.05 per active direction-minute — roughly $2–5 for an
hour of bilingual conversation. VAD gating is what keeps it there. The
research notes that per-character prices should be reconfirmed at build time.

## Non-goals for v1

**Surround sound.** Port pairing is by index over a name-sorted list, which is
correct for stereo and mono only — a 5.1 or 7.1 default sink alphabetizes to
FC, FL, FR, LFE, SL, SR rather than channel order, and would cross channels
silently. Supporting it means carrying `audio.channel` through `PwPort`, which
`graph.py` parses past today.

Voice cloning or preservation. LocalAgreement-2 (the seam only). Glossaries.
Language auto-detection. More than two participants, or diarization. Offline
or local models. The Gemini Live single-box path. Any OS other than Linux with
PipeWire.

## Done looks like

- Measured glass-to-glass under 2.5 s per direction on a real call.
- Zoom or Viber selects the virtual mic, and the remote party hears
  intelligible translated speech.
- A 30-minute call completes with no rotation gaps and no graph corruption.
- Ctrl-C restores the graph completely, and a `kill -9` is repaired at next
  startup.
