# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

sidetap is a real-time two-way voice interpreter for any call on Linux —
Zoom, Viber, Telegram, Discord, Slack huddles — built by tapping **PipeWire**
instead of integrating with each platform's API. It runs a cascaded pipeline
twice, once per direction: Speech-to-Text v2 (`chirp_2`, streaming) → Cloud
Translation v3 → Text-to-Speech (Chirp 3 HD, streaming synthesis). Routing is
**full replacement** — each party hears only the translation, never the
original and the translation at once — so pipeline health is not a diagnostic
detail, it is the only thing standing between the conversation and silence.

sidetap started from `meetscribe` (a call transcriber, same author): the
input side — PipeWire graph parsing, the additive per-application tap,
`pw-record` framing, VAD gating, stream rotation, Chirp streaming
recognition — was **ported**, copied and adapted with its tests, not
imported. The two repositories share no runtime dependency.

## Repository layout — read this first

`sidetap/` is the application; see **Architecture** below for the module
table.

`tests/` holds 555 tests that run with no audio hardware, no network and no
credentials — every subprocess, socket and clock the package touches sits
behind a `Protocol` in `ports.py`, with a real implementation in
`adapters.py` and a fake in `tests/conftest.py`.

`docs/superpowers/specs/` holds the design this package implements, and
`docs/superpowers/plans/` the implementation plan it was built from.
`docs/experiments/` records the measurements that plan's decisions rest on —
prefer a number from there over a claim from memory. `docs/manual-smoke.md`
is a checklist for what the automated tests structurally cannot verify: that
ducking actually silences the original, that a messenger enumerates the
virtual mic, that the remote party hears anything intelligible. Run it by
hand before trusting a change to capture, routing, playout or the duck.

`docs/initial_research/` is reference material, not the codebase: the
research spike that preceded this package, kept to record what it found.

`tests/fixtures/pw_dump_real.json` is a **real `pw-dump`, scrubbed** — every
other fixture in `tests/fixtures/` was hand-written from the same assumptions
`graph.py` was written from, so they cannot catch a wrong assumption; they
share it. This one can.

## Commands

**This project uses uv, not pip.** Never call `pip install` or activate
`.venv` by hand; `uv run` does both. `uv.lock` is committed and `uv sync` is
what installs from it. Run every command below from the repo root.

```bash
uv sync                          # install from uv.lock

uv add <pkg>                     # edits pyproject.toml + uv.lock together
uv lock --upgrade                # refresh the lockfile

# verify the PipeWire toolchain BEFORE debugging anything in Python
pw-cli --version                   # needs >= 0.3.60
pw-dump | head                     # graph as JSON
wpctl status                       # sinks/sources, incl. sidetap's own nodes

uv run pytest -q                                   # 555 tests, no audio/network/creds needed
uv run sidetap devices                              # run this MID-CALL, not before
uv run sidetap doctor                               # environment checks
uv run sidetap doctor --install                     # write the virtual-mic config (once)
uv run sidetap doctor --repair                      # replay the journal after a crash
uv run sidetap run --app zoom --their-lang ru-RU --my-lang en-US
uv run python -m sidetap --help
```

Do not run `gcloud auth ...` or anything else that changes ambient
credentials in a development environment attached to a live GCP project —
recover the *checks* with `sidetap doctor`, not by re-authenticating.

Dependencies are plain, not grouped per engine: `google-cloud-speech`,
`google-cloud-translate`, `google-cloud-texttospeech` and
`webrtcvad-wheels` are ordinary dependencies in `pyproject.toml`, because
Google Cloud is the only engine implemented. All of them are still imported
lazily — see **Invariants**.

Credentials: `GOOGLE_APPLICATION_CREDENTIALS` (service account key path) or
`gcloud auth application-default login`, plus `GOOGLE_CLOUD_PROJECT` (or
`--project`) — asked for separately from ADC's own project on purpose, since
ADC's default is whatever `gcloud` was last pointed at, which is routinely
not the project with these three APIs enabled.

## Architecture

### Signal flow

Both directions below are the same `DirectionPipeline`, instantiated twice.
They differ only in source node, destination sink, source language, target
language and voice; the channel fixes the source language, so nothing is
language-detected.

```
=== DIRECTION "in"  (them -> you) ===============================================

 messenger   --additive pw-link-->  recorder -> gate -> ASR -> segmenter
 Stream/Output       (tap.py)                          (their-lang) |
      |                                                             v
      |                                                   translate L1->L2
      |                                                             |
      v                                                             v
 sidetap_duck --[vol 0% while speaking]--> headphones <---------- playout
                                                          (lag-capped queue)

=== DIRECTION "out"  (you -> them) ===============================================

 USB mic -> recorder -> gate -> ASR -> segmenter -> translate L2->L1 -> TTS
                                (my-lang)                                |
                                                                          v
 messenger input <-- sidetap_virtmic <-- loopback <-- sidetap_tts_sink <- playout
  (your real mic is never linked here,     [permanent, from pipewire.conf.d]
   except while bypassed)
```

`L1` is `--their-lang`, `L2` is `--my-lang`.

### Concurrency: threads, not asyncio

Google's streaming SDKs (`StreamingRecognize`, `StreamingSynthesize`) are
synchronous generator APIs, and the ported capture code is
subprocess-and-thread shaped, so this stays threaded rather than forced into
one asyncio loop. Per direction: a capture thread (in `capture.py`, one per
track), an ASR worker thread, a translate-and-synthesise worker thread
(`DirectionPipeline.consume`), and a playout thread, joined by
`queue.Queue`s. Two more run session-wide: the routing watcher
(`Router.run`) and the capture health poller.

Textual owns the main thread's event loop and **the pipeline never calls into
it** — worker threads write to a lock-guarded `Metrics` snapshot
(`metrics.py`) that `tui.py` polls at 10 Hz. That one-way dependency is what
makes `--no-tui` and the headless test suite the same code path rather than a
second implementation.

### Modules

| Module | Role |
|---|---|
| `types.py` | value types and audio constants; imports only stdlib |
| `ports.py` | every `Protocol` — one real implementation, one fake, each |
| `graph.py` | parses `pw-dump` text into a `PwGraph`; pure, never spawns |
| `recorder.py` | builds `pw-record` argv, frames stdout into fixed blocks |
| `tap.py` | `AppTap` — links a matching application's ports, re-scans every 2 s |
| `capture.py` | owns recorders, queues and capture threads |
| `vad.py` | `SilenceGate` — drops silence, keeps a tail so utterances finalise |
| `rotation.py` | `StreamClock`, `AudioTimeline` — offsets across rotated streams |
| `asr.py` | `chirp_2` streaming recognition, `RecognitionWorker` |
| `segment.py` | the `Segmenter` seam; finals-only, or LocalAgreement-2 prefixes |
| `translate.py` | Cloud Translation v3 adapter, NMT fallback |
| `tts.py` | Chirp 3 HD streaming synthesis adapter |
| `playout.py` | lag-capped queue of growable utterances, `DuckControl`, PCM writer |
| `routing.py` | duck loopback lifecycle, re-route, journal, restore |
| `pipeline.py` | `DirectionPipeline` — wires one direction's stages end to end |
| `metrics.py` | per-stage latency, queue depth, stage health, cost — the TUI's only input |
| `cost.py` | `Rates` — STT/MT/TTS pricing as a config value, not a hardcoded number |
| `doctor.py` | `sidetap doctor` — environment checks, writes the virtual-mic config |
| `transcript.py` | bilingual `.jsonl` (append, flushed per unit) + `.md` (written at close) |
| `tui.py` | Textual dashboard, hotkeys |
| `run.py` | `Session` — builds every stage, owns startup/shutdown, signal handling |
| `adapters.py` | the real ports — **every subprocess on the audio path starts here** (`recorder.py` and `doctor.py` are the two deliberate exceptions, neither on the audio path) |
| `cli.py` / `__main__.py` | argparse, `doctor`/`devices`/`run` dispatch |

### Invariants

Each of these says what breaks if it is violated, because "don't do X" reads
as a style preference and this is not one.

- **Audio contract.** Capture side: s16 / 16 kHz / mono, 100 ms blocks
  (`BLOCK_BYTES` = 3200) — `pw-record --rate/--channels/--format` does the
  resampling. Playout side: s16 / 24 kHz / mono — Chirp 3 HD's own streaming
  output rate; `pw-cat` resamples to whatever the destination sink wants.
  Neither direction does sample-rate conversion in Python, which is why the
  capture and playout paths carry no numpy. Adding resampling in Python
  duplicates what PipeWire already does, and gets it wrong at the boundary
  where the two rates meet.
- **Identify PipeWire nodes by `object.serial` — except `wpctl`, which
  resolves against `object.id`.** PipeWire recycles `object.id` on every
  restart of a stream, and a restarted stream can inherit its dead
  predecessor's id within one poll interval; `object.serial` is durable, which
  is why the routing journal and `Router._routed` key on it. `wpctl` is the
  one place that must NOT use serial: `WpctlVolumeControl.set_volume` takes
  `object.id` because that is the only number `wpctl set-volume` accepts, and
  `Router.duck_id`/`Router.duck_serial` are tracked as two separate fields for
  exactly this reason. Conflate them and the duck silently never closes — the
  original plays under every translation for the rest of the call, with
  nothing on screen explaining why (see `docs/experiments/01-tap-volume.md`).
- **Google's SDKs are imported lazily**, inside the function bodies that need
  them (`asr.py`, `tts.py`, `doctor.py`'s API checks; `translate.py`'s
  `google.cloud.translate` client — its `google.api_core.exceptions` import
  stays at module level, since it needs no network or credentials), not at
  module level. `webrtcvad` the same way (`vad.py:webrtc_detector`), with a
  fallback to a no-op gate if it is missing. This is what lets 555 tests
  import the package and run with no credentials configured at all — a
  top-level `from google.cloud import X` would make every test that merely
  imports the module require live credentials to collect.
- **The keepalive lives in `RecognitionWorker` (`asr.py`), not in
  `SilenceGate` (`vad.py`).** An unlinked PipeWire capture node delivers ZERO
  BYTES, not silence — the tapped application closed, a tab quit — and
  `SilenceGate` only ever sees blocks that actually arrived, so it structurally
  cannot notice that failure. `KEEPALIVE_S = 2.0`: whenever 2 s pass with
  nothing sent, `RecognitionWorker` sends one `SILENCE_BLOCK` itself, covering
  both "nothing worth sending" and "nothing arriving at all" with one timer.
  A gate-level keepalive looks equivalent under test and silently fails the
  second case, which is the one that ends a call's recognition mid-way with a
  409 "stream timed out" and nothing forwarding it.
- **Audio time is not elapsed time.** The silence gate drops blocks before
  they are sent, so Chirp's `result_end_offset` is a position in the audio
  actually sent, not real time. `AudioTimeline` (`rotation.py`) maps a sent
  position back to when it was captured by recording each sent block's real
  timestamp; `StreamClock`, which only adds a flat offset, must never be
  substituted for it when mapping a recognition result onto the session
  timeline (see the `SessionTime` Protocol's docstring in `asr.py`). Get this
  wrong and every timestamp is understated by however much silence was gated
  — and because the two directions gate different amounts, the bilingual
  transcript's chronological order scrambles.
- **The duck defaults open and fails open.** `DuckControl.close()`
  (`playout.py`) only flips its internal flag on a *successful* `wpctl` call,
  so a failed call leaves the duck's own bookkeeping consistent with reality
  rather than desyncing it; `Playout.run()`'s `finally` calls `duck.open()`
  unconditionally on shutdown or if the tick loop ever raises. This is
  deliberate: a duck stuck **closed** silences the person you are on a call
  with and leaves them talking to nobody, which is worse than sidetap not
  working at all. A duck stuck open at worst leaves the original and the
  translation both audible — audibly wrong, not silently cruel.
- **Journal before you touch the graph.** `Router` (`routing.py`) writes
  every planned link/unlink to `~/.local/state/sidetap/routing-journal.json`
  — atomically, via a temp file plus `os.replace()` — *before* calling the
  linker/unlinker, on every call that routes something. A `kill -9` between
  the journal write and the actual `pw-link`/`pw-unlink` is recoverable by
  `sidetap doctor --repair`, which replays the journal. Do this in the other
  order and a crash in that window leaves the graph rewired with nothing on
  disk to repair it from.

### Non-obvious mechanics worth knowing before you touch these

- **The tap is additive**, exactly as in meetscribe: `pw-link` adds a second
  link from the application's existing output ports into sidetap's capture
  node; the application's link to your speakers is routing.py's concern, not
  the tap's, and stays untouched until `Router.engage()` re-routes it through
  the duck.
- **`Router.engage()` reuses one snapshot rather than re-reading the graph**
  after spawning the duck loopback, because `spawn_writer()` returns as soon
  as the process forks with no guarantee its nodes are registered yet. A
  fresh read here could race the loopback either way; reusing the snapshot
  means `engage()` simply finds no duck yet and the next `poll_once()` (at
  most `POLL_INTERVAL_S` later) finishes the job.
- **Bypass's real-mic link is not journalled and does not use a fresh
  snapshot to unlink.** `Session._link_real_mic` replays exactly what it
  linked rather than recomputing from a new snapshot, because the default
  source can change mid-call (a headset gets plugged in) and recomputing
  would unlink the wrong pair while leaving the real link live. That link
  outlives the process if bypass is left on through `kill -9`, and
  `doctor --repair` cannot find it — `Session.shutdown()` tears it down
  itself for exactly this reason, before anything else.
- **`Router.restore()` always terminates the loopback, even with an empty
  journal.** The duck is created by `engage()`, not by routing a stream, so
  it exists even when no application stream was ever routed through it —
  starting sidetap before the call and quitting before it begins does exactly
  that. Returning early on an empty journal used to skip this and orphan the
  `pw-loopback` process.
- **Routing only pairs ports by index**, which is correct for mono and
  stereo only: `ports_of()` sorts by port *name*, and for 5.1 that
  alphabetizes to FC, FL, FR, LFE, SL, SR — not positional order — so a
  5.1 sink would cross channels. Scoped to stereo/mono for v1; see
  `routing.py`'s `_route_locked` for the full comment.
- **`SILENCE_TAIL_BLOCKS = 5` and `aggressiveness = 2`** (`vad.py`) were
  inherited from meetscribe, a transcriber with no latency budget, and have
  not been remeasured against Chirp or against sidetap's two very different
  input signals (a raw mic vs. audio already processed by the far end). See
  `docs/manual-smoke.md`'s "Tuning constants that were inherited, not
  measured" before treating either as settled.
- **Translation LLM is the default `--mt-model`, with a sticky NMT
  fallback.** `GoogleTranslator` downgrades to `general/nmt` on error and
  stays there for the rest of the session — `Metrics.set_mt_model` exists so
  that stays visible, since the next successful NMT call would otherwise turn
  the TUI's `mt` marker green again with no sign the model actually changed.
- **Mute and bypass both suppress the OUT playout, so neither may be derived
  from `Playout.suppressed`.** `Session` keeps `_muted_out` as its own flag
  and `_apply_suppression_locked` derives OUT from `bypassed or _muted_out`
  (IN from bypass alone). Read the playout instead and the two states are
  indistinguishable once either is on: `m` under bypass becomes
  `not True`, which un-suppresses OUT and puts translated speech over the
  unmediated call bypass exists to step out of, and leaving bypass clears a
  mute the user never lifted. The guard on an actual change matters too -
  `set_suppressed` flushes, and flush cuts the utterance in progress short.
- **`set_suppressed` flushes on BOTH edges.** Nothing upstream knows a
  playout is suppressed: `pipeline._speak` keeps synthesising and keeps
  calling `begin()`, so the queue refills the whole time. `begin()`'s trim
  bounds that at `LAG_CAP_S` rather than preventing it, so flushing only on
  the way in would replay up to 20 s of a conversation that has already moved
  on the moment you come back.
- **The dashboard's toggle state is polled, never pushed.** `_paint_toggles`
  re-applies the `-engaged` class to the `Footer`'s keys on every 10 Hz tick
  from the `Metrics` snapshot, because `Footer` rebuilds its `FooterKey`
  children whenever screen bindings change and would drop a class set once.
  It also means the key shows what the pipeline actually did, not what the UI
  asked for - the reason `action_bypass` no longer keeps a local mirror.
- **Cost is billed on audio actually sent, not on call duration.**
  `RecognitionWorker.blocks()` reports both real speech and keepalive silence
  through `on_audio_sent`, because Google bills them identically — counting
  only real speech would understate a quiet call left running unattended,
  which is the one a user is most likely to forget about.
- **Playout starts an utterance before it has been fully synthesised.**
  `DirectionPipeline._speak` appends chunks to a `playout.Utterance` as they
  arrive rather than joining the generator, which is worth 271 ms on a short
  sentence and 2109 ms on a long one. Three consequences that are easy to
  break: an utterance that has *started* and then run dry holds the duck
  **closed**, because opening it would let a burst of the untranslated
  original through a mid-sentence gap — bounded by `STARVE_LIMIT_TICKS` so a
  dead producer cannot silence the remote party for the rest of the call; the
  lag cap never drops an utterance that is still arriving, since its duration
  is unknown and it is always the newest thing queued; and every `begin()`
  must be paired with a `finish()` on every exit path, or an orphaned open
  utterance sits at the queue head where the trim loop breaks, switching the
  lag cap off for that direction — bounded by the same `STARVE_LIMIT_TICKS`,
  since a head stuck unstartable for that long gets force-closed in place
  regardless of what left it open.
- **Committing early is bounded by Chirp, not by a constant.** Interims arrive
  once per 5 s of sent audio and short utterances produce none at all
  (`docs/experiments/06-interim-cadence.md`), so `LocalAgreementSegmenter`
  needs ~11 s of continuous speech before two hypotheses can agree and is
  inert below that. There is deliberately no length threshold in the code: a
  constant here would be a second, worse copy of a bound the API already
  imposes, and it would drift the moment Google changes the cadence.
- **The comparison key is lowercased and stripped of edge punctuation.** Both
  were measured changing between two interims of one utterance - `Что` to
  `что`, `сверхурочно.` to `сверхурочно,`. Compare surfaces instead and the
  longest common prefix on the real capture is **zero characters**, so nothing
  commits early and the feature silently does nothing at all. Punctuation is
  stripped only at a token's edges, because stripping it throughout would key
  `1.2` and `12` the same and commit a number the recogniser had not settled
  on. It picks the cut point; it never decides agreement.
- **`MIN_ANCHOR = 8` (`segment.py`) is a safety floor, not a knob for
  repeats.** `_overlap`'s third arm matches a tail of what was already spoken
  ANYWHERE in the candidate and drops everything in front of it - what catches
  Chirp re-windowing back past an utterance's first commit, which re-spoke 64
  words on a real call. Lower the floor to catch shorter re-windows and an
  ordinary phrase that recurs in new speech anchors instead, silently dropping
  the new words before it; coincidental runs on that call reached 6 words. A
  few repeated words from a re-window shorter than 8 are the intended cost.
- **`Playout.expect_continuation` is the only thing keeping the duck shut
  between committed clauses, and it has its own deadline.** `tick()` opens the
  duck whenever the queue drains and nothing is starved, and "starved" means an
  utterance already part-way through - which the gap between two clauses is
  not. Without the hold, IN leaks a burst of the untranslated original into
  that gap roughly every 5 s of a monologue. The hold counts on `_hold_ticks`,
  **not** the `_starved_ticks` the other two cases share: sharing let a hold
  spend an incoming clause's start budget and truncate it, measured at 10 ticks
  instead of 100. `_hold_ticks` counts consecutive ticks with the duck shut and
  no chunk written, and is cleared only by a written chunk or `flush()` - not
  by arming, which would make the bound a lease a caller could renew forever -
  before this fix the duck never reopened for the full 100 000-tick (33
  minute) run a probe simulated, arming every 50 ticks with nothing ever
  queued. Worst case across every adversarial pattern tried after the fix is
  99 ticks, 1.98 s. `pipeline._speak` arms it only after playout has accepted
  audio, so a direction whose translator is down cannot re-arm it.

## Things that bite at runtime

- **No two of the three services share a region.** `chirp_2` does not exist
  in `europe-west3` (400 "does not exist in this location"), so `--region`
  defaults to `europe-west4`; Cloud Translation accepts only `global` or
  `us-central1`; and TTS has no Frankfurt single-region, so `--tts-region`
  uses the `eu` multi-region. Three services, three different answers — which
  is why `--region`, `--mt-region` and `--tts-region` are three separate flags
  rather than one shared `--region`, and collapsing them back into one is a
  regression, not a simplification. The tempting mistake is Frankfurt, because
  it is nearest and `long` works there for `en-US` — and then returns 400 for
  `ru-RU`, so it fails only once audio is already flowing. See `asr.py`'s
  module docstring, which records the measured grid, and
  `docs/experiments/03-translation-llm.md`.
- **Headphones matter for the same reason they do in meetscribe:** without
  them your own speakers re-enter your microphone and get recognised as your
  own speech.
- **Wayland is irrelevant here** — audio capture needs no portal permission;
  that is a video-capture concern.
- **Never run `gcloud auth ...` against this checkout casually** — it is
  attached to a live GCP project, and re-authenticating has broken the
  project's ambient credentials before. Use `sidetap doctor` to check state,
  not to fix it by re-logging in.
