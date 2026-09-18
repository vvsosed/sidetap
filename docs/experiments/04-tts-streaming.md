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
