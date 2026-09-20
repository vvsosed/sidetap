# Experiment 4 — does Chirp 3 HD streaming synthesis work as designed?

Not one of the three planned experiments. Added on 2026-09-18 once the
Text-to-Speech API was enabled, because Task 17's entire design rests on
`StreamingSynthesize` behaving a particular way and nothing had verified it.

**Question.** Do the voice names hardcoded in Task 25's `DEFAULT_VOICES` table
actually exist, does `StreamingSynthesize` work with Chirp 3 HD, does it
deliver audio incrementally, and what is the real time-to-first-audio?

**Why it matters.** The voice table was written from research, not from the
API. An invalid voice name is a fatal `InvalidArgument` on the first utterance
of every session. And the spec budgets TTS time-to-first-byte at ~300 ms
without ever having measured it — that figure is a quarter of the whole
glass-to-glass target.

**Method.** List voices from both the `eu` and `global` endpoints and check
every name in the table. Then run real streaming syntheses, timing the gap
from request to first audio chunk, discarding one warm-up call.

**Result.** Project `<project-id>`, from central Europe, `eu` endpoint.

All eight planned voices exist, on both `eu` and `global` (2066 voices total,
1568 of them Chirp 3 HD):

`en-US-Chirp3-HD-Charon`, `en-GB-Chirp3-HD-Charon`, `ru-RU-Chirp3-HD-Kore`,
`uk-UA-Chirp3-HD-Kore`, `de-DE-Chirp3-HD-Kore`, `es-ES-Chirp3-HD-Kore`,
`fr-FR-Chirp3-HD-Kore`, `pl-PL-Chirp3-HD-Kore`.

Streaming works and is genuinely incremental — 6.2 s of Russian audio arrived
as 27 separate chunks, which is what lets playout start speaking before
synthesis finishes.

Time-to-first-audio, five warm runs each after one discarded warm-up:

| voice | min | median | max |
|---|---|---|---|
| `ru-RU-Chirp3-HD-Kore` | 143 ms | **267 ms** | 331 ms |
| `en-US-Chirp3-HD-Charon` | 147 ms | **186 ms** | 283 ms |

**Cold start is the outlier: the very first call of a session measured 543 ms**
against a 267 ms warm median — roughly 280 ms of connection setup paid once.

**Consequence.**

1. The `DEFAULT_VOICES` table in Task 25 is correct as written. No change.
2. The spec's ~300 ms TTS budget holds *warm*: 186-267 ms median. Recorded.
3. **Warm the TTS connection during `Session.setup()`.** Otherwise the first
   utterance of every call pays ~280 ms extra, on top of the ~195 ms
   Translation LLM already costs — and the first utterance is the one where a
   user is deciding whether the tool works at all. Cheap to fix: one throwaway
   synthesis at startup, while the graph is being rewired anyway.

---

## Addendum, 2026-09-20 — chunk shape, and whether early playout can underrun

The original run measured time-to-first-audio and counted chunks. It never
measured chunk *sizes* or the gaps between them, which is what decides whether
starting playback on the first chunk risks a gap in the middle of a sentence.

**Method.** Three Russian utterances (2.2 s, 6.9 s, 16.6 s of audio), three
runs each, one warm-up discarded, `ru-RU-Chirp3-HD-Kore` on the `eu` endpoint.
Recorded every chunk's arrival time and byte count, then simulated playing
from the first chunk to find the smallest the buffer ever gets.

**Result.**

| utterance | audio | chunks | full synthesis | first chunk | ratio |
|---|---|---|---|---|---|
| short | 2200 ms | 10 | 465 ms | 194 ms | 4.7x realtime |
| medium | 6880 ms | 29 | 1052 ms | 208 ms | 6.5x realtime |
| long | 16560 ms | 70 | 2339 ms | 230 ms | 7.1x realtime |

Time-to-first-audio across all nine runs: 182-337 ms, consistent with the
186-267 ms medians above.

The shape is strikingly regular:

- the **first chunk is always exactly 200 ms of audio**
- every chunk after it is **240 ms of audio, arriving every ~30 ms**
- longer text synthesises *faster* relative to real time, not slower

**The buffer never gets tighter than the first chunk.** Simulating playback
from the moment chunk 1 arrives, the minimum buffer margin was **200 ms in all
nine runs** — identical to the first chunk's own duration, because production
outruns consumption from that point on and the margin only grows.

**Consequence.**

1. **Early playout cannot underrun on a healthy connection.** Pacing is not a
   risk; only a network stall is. This removes the main objection to
   exploiting the streaming generator.
2. **The win scales with sentence length** — 271 ms short, 844 ms medium,
   2109 ms long — so it is largest exactly where the current
   `b"".join(...)` hurts most.
3. **A 400 ms start threshold costs ~30 ms** and doubles the stall margin to
   440 ms, because chunk 2 arrives ~30 ms after chunk 1. Adopted in the
   streaming-playout design.
4. **The lag cap does not need to become an estimate.** At 4.7-7.1x realtime,
   counting only arrived bytes understates backlog by a fraction of one
   utterance and self-corrects within about a second. `tts.py`'s docstring
   claim that early playout forces an estimated backlog is wrong, and is
   corrected as part of that work.
