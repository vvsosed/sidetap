# LocalAgreement-2 — stop the monologue stall

Status: design, approved 2026-09-22.

## The problem

Nothing downstream of recognition starts until Chirp declares a result final,
and on continuous speech Chirp does not declare one. Measured
(`docs/experiments/06-interim-cadence.md`): 34.8 s of pauseless Russian
produced a final at 7.43 s covering the first 6 s, then **29.3 s of nothing**,
then one final at 36.69 s carrying 28.7 s of audio as a single unit.

Because routing is full replacement, those 29.3 s are not "the translation is
late" — they are the listener hearing the duck open onto an untranslated voice
they cannot understand, for half a minute, with every pipeline indicator
green.

`segment.py` has always named LocalAgreement-2 as the alternative, and
`pipeline.py:120-125` already routes every interim into the segmenter so that
such an implementation could exist. This is that implementation.

## What this is not

It is **not** a general latency win. `segment.py`'s docstring and the v1 spec
both quote "roughly 0.5-1 s" on a normal utterance. That number describes a
case Chirp cannot serve: short turn-taking sentences produce **no interim
results at all** (Experiment 6, finding 4), so there is nothing for a prefix
comparison to work on. Ordinary conversation is unaffected by this change,
byte for byte.

It is also **not** a backlog fix. Each committed clause remains its own queue
entry, so the lag cap behaves exactly as it does today.

## What the measurement changed

Three findings from Experiment 6 shape the design rather than merely support
it:

1. **Interims arrive every 5.0 s of sent audio, deterministically** — four
   runs produced identical `result_end_offset` values. The commit cadence is
   therefore ~5 s and is not tunable from this side.
2. **The feature is self-limiting for free.** Two agreeing interims require
   roughly 11 s of continuous speech. Below that the segmenter emits nothing
   early and falls back to finals. **No length threshold is needed, and none
   should be added** — a constant here would be a second, worse copy of a
   bound the API already imposes.
3. **Capitalisation and punctuation change between interims.** `Что` became
   `что` between two hypotheses whose word prefixes were otherwise identical.
   A character-level comparison would find a common prefix of zero and commit
   nothing for the whole monologue.

## Design

### `LocalAgreementSegmenter` (`segment.py`)

Behind the existing `Segmenter` protocol — `feed(result) -> list[Unit]`,
unchanged. One instance per direction, which `FinalsOnlySegmenter`'s docstring
already forbids hoisting out of the loop; that warning stops being
hypothetical here.

State: the previous interim's tokens, and how many words of the utterance in
progress have already been committed.

On an **interim**: take the longest common key prefix against the previous
interim, and commit whatever it adds beyond what has already been sent. On a
**final**: match the final's leading tokens against what was committed, emit
the remainder in full, and reset. The punctuation rule below does not apply to
a final — there is nothing left to wait for, so nothing is held back.

Two agreements, fixed. Not configurable, and justified by measurement rather
than by the name: the final in Experiment 6 inserted a word thirteen from the
end of text that had already appeared in one interim. Two agreements leave
that tail uncommitted. One would have spoken text the final then contradicted,
and a synthesised voice cannot take a word back.

### The comparison key

Each token is a pair: the **surface** form, which keeps case and punctuation
and is what gets translated, and the **key**, which is lowercased and stripped
of punctuation and is the only thing compared.

This is forced by the data. Every revision observed between interims was
capitalisation, punctuation, or a trailing partial word — never a word
substitution. A partial word needs no special handling: an extended word has a
different key, so the prefix simply stops before it.

### Where to cut

Cut the committed growth at the **last punctuation mark inside it**. Interims
do carry punctuation, so a boundary is almost always available, and cutting
there is what keeps a fragment from reaching the translator mid-clause — the
word-order cost the v1 spec named as LA-2's main downside.

If the growth contains no punctuation at all, **commit it whole rather than
hold it back.** Holding would reproduce the stall this change exists to
remove: five seconds of speech with no boundary is precisely the case where
waiting hurts. One rule, no constant, and it fails toward speaking.

### Spans, and the one edit they force

A committed prefix's `t_end` is the **older** interim's `t_end` — the audio
position through which the text is confirmed — and its `t_start` is the
previous commit's end. Chirp rejects `enable_word_time_offsets` in streaming
mode, so nothing finer is available.

Chaining applies **only within an utterance that was actually committed in
pieces.** A final whose utterance committed nothing early — every turn of
ordinary conversation, since Chirp emits no interims for short utterances —
must carry exactly the span `FinalsOnlySegmenter` would have given it,
`t_start == t_end == result.t_end`. Chaining unconditionally moves every
row's start back by one utterance, and `transcript.render_markdown` sorts on
`t_start`, so an interleaved two-way transcript comes out reordered — on the
default path, for every call.

That forces one change in `pipeline.py`. `asr_ms` is computed once from
`result.t_end` before the unit loop. For a committed prefix that reports the
current interim's arrival lag (~1.0 s) when the content is really ~6 s old,
which would corrupt the transcript's latency column — the column the v1 spec
names as the evidence for this very decision. It moves inside the loop and
reads `unit.t_end`. For `FinalsOnlySegmenter` the two are the same value, so
nothing changes on the old path.

### The bounded duck hold (`playout.py`)

The two directions fail in opposite ways, and only one of them needs help.

On **IN** the translation is shorter than its Russian source, so playout
drains before the next clause is committed and the queue empties. `tick()`
opens the duck whenever the queue drains and nothing is *starved*, and
"starved" today means an utterance already part-way through. So between
committed clauses the duck would open and leak the untranslated original —
roughly once every five seconds, right through a monologue. On **OUT** the
translation is longer, the backlog grows instead, and there is no duck at all
(`run.py:262-266`).

`Unit` gains `continues: bool = False`, set on every committed prefix that is
not the end of its utterance. The `Segmenter` protocol stays one method: the
extra bit rides on the value type rather than widening the seam.

`Playout.expect_continuation(bool)` sets a flag that makes the two
"nothing playable" branches of `_advance_locked` report `starved`, so the duck
closes instead of opening. It reuses the existing `_starved_ticks` /
`STARVE_LIMIT_TICKS` counter, so **the hold can never exceed 2 s** and fails
open — the property CLAUDE.md is least willing to trade, because a duck stuck
closed silences the person you are talking to. It is also cleared by the
final's unit, by `flush()`, and by `set_suppressed(True)`.

**Armed only when playout accepted a chunk.** If translation or synthesis
fails on a clause, the hold is not re-armed. Arming it unconditionally would
let a direction whose translator is failing re-arm every five seconds and hold
the duck shut for the whole call with silence behind it, which is the failure
this module exists to avoid.

### Wiring and the flag

`--no-early-commit`, `store_true`, in the **output** argument group beside
`--lag-cap` — both shape what the listener hears, where the cloud group is
regions and models. Read through a `getattr` shim in `run.py`, as `_rate` and
`_gender` already are, so hand-built `Namespace`s keep working.

On by default. The feature does nothing below ~11 s of continuous speech, so
the risk it carries is confined to the case it was asked for; the flag exists
so the two can be compared on a real call, which is the only place the duck
behaviour can be judged.

## What needs no change

`metrics.py`, `transcript.py` and `cost.py` all absorb ~5 s commits as they
stand, and two of them improve. A 30 s monologue becomes about six transcript
rows carrying real latency figures instead of one row reporting 31 s, and the
dashboard advances every five seconds instead of once at the end. `cost.py`
bills per character, so committing in pieces bills the same total — more
requests, identical characters. `DeadAirWatch` fires more often with unchanged
semantics.

## Testing

The load-bearing fixture is `tests/fixtures/chirp_interims.json`: every result
from Experiment 6, real API output. The argument is the one CLAUDE.md makes
for `pw_dump_real.json` — a hand-written interim sequence would share exactly
the assumptions it is meant to test. Replayed through the segmenter it pins
that the `Что`/`что` case change does not break agreement, that the
`сверхурочно.`/`сверхурочно,` punctuation change does not break agreement, and
that the final's inserted word was never committed early.

- `test_segment.py` — one interim commits nothing (the 7.9 s case); a final
  flushes the remainder; the two directions do not share state, which stops
  being trivially true the moment this class has instance state.
- `test_playout.py` — the duck stays closed across a gap while a continuation
  is expected; opens when the run ends; **opens at `STARVE_LIMIT_TICKS` when
  the continuation never arrives**; `flush()` and `set_suppressed(True)` clear
  the hold.
- `test_pipeline.py` — `asr_ms` comes from the unit, not the result; the hold
  is not armed when nothing was accepted.
- `test_cli.py` / `test_run.py` — the flag parses, is absent by default so
  early commit is on, and selects the segmenter, one instance per direction.

Four mutations must each break a named test before this is called done: drop
`.lower()` from the key, change the agreement count from 2 to 1, make
`expect_continuation` a no-op, remove the starvation bound from the hold.

`docs/manual-smoke.md` gains the checks no test can make: that a monologue
actually starts being spoken part-way through, and that the duck does not
audibly flap between committed clauses.

## Out of scope

**Short utterances produce no interims**, so the TUI's live interim line never
fires in ordinary conversation. `2026-09-17-sidetap-design.md:317-320` keeps
interims enabled partly on the grounds that they give back the "they are
talking right now" signal that full-replacement routing removes. They do not.
Recorded in Experiment 6; a separate change.

The v1 spec's *Rejected* paragraph (`2026-09-17-sidetap-design.md:72`) stays
as written. This document supersedes it, the way Experiment 2 gained an
addendum rather than a rewrite.

Splitting a single long *final* at clause boundaries, AlignAtt, and any
attempt to reduce the ~1 s recognition lag itself all remain future work.
