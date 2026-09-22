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

State: the previous interim's tokens, and the tokens of the utterance in
progress that have actually been spoken. *(Superseded in part - the second
was a word COUNT until a real call falsified it; see "The count was the bug"
at the end of this document.)*

On an **interim**: take the longest common key prefix against the previous
interim, and commit whatever it adds beyond what has already been sent. On a
**final**: match the final's leading tokens against what was spoken, emit the
remainder in full, and reset. The punctuation rule below does not apply to
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

## Addendum, 2026-09-22 — As built

A whole-branch review checked this document against the shipped code and
found four places where what shipped is not what was designed, plus one
mechanism this document never mentions. Left here rather than folded into the
body, the way Experiment 2 gained an addendum rather than a rewrite.

**The hold does not reuse `_starved_ticks`.** "The bounded duck hold" above
says `expect_continuation` "reuses the existing `_starved_ticks` /
`STARVE_LIMIT_TICKS` counter". It ships with its own counter, `_hold_ticks`
(`playout.py`). Sharing was tried and reverted: it let a hold in progress
spend an incoming clause's own start budget, so a clause that would otherwise
have played arrived only to be truncated by a deadline that had nothing to do
with it - measured at 10 ticks instead of 100. `_hold_ticks` counts
consecutive shut-and-silent ticks on its own, cleared only by a written chunk
or `flush()`, so a starving `_current` or a stuck queue head keeps its own
full `_starved_ticks` budget regardless of whether a hold is also armed.

**The key strips punctuation only at token edges, not throughout.** "The
comparison key" above says the key is "lowercased and stripped of
punctuation". `segment.py`'s `_tokens` lowercases fully but strips punctuation
only from a token's edges (`_EDGE_PUNCTUATION`), because stripping it
throughout would key `"1.2"` and `"12"` to the same string and let two numbers
a factor of ten apart compare as agreed - committed, translated and spoken,
with no way to take a spoken number back. Both revisions Experiment 6 actually
observed were at a token's edge, which is what edge-only stripping was
measured to be sufficient for.

**The cut point is a fixed boundary set, not "the last punctuation mark".**
"Where to cut" above says to cut "at the last punctuation mark inside it". The
shipped `_BOUNDARY = ",.;:!?…"` (`segment.py`) deliberately excludes the em
dash: Russian uses it both as a clause separator and to stand in for a missing
copula ("Москва - столица"), and nothing in the text distinguishes the two, so
treating it as a boundary risks cutting a subject from its predicate. `_cut`
also tests only a token's *last character* against that set, not "contains
punctuation" more generally - a closing quote or bracket with nothing after it
is missed and costs a later commit, not a wrong one.

**`asr_ms`'s wall-clock read stays outside the unit loop; only the
`t_end` it is measured against moves in.** "What needs no change" and the
paragraph above it describe this as `asr_ms` moving inside the unit loop
wholesale. The implementation is right and this document is wrong: only the
`unit.t_end` read moves inside the loop; `arrived = self._clock.monotonic() -
self._session_t0` (`pipeline.py`) is still read once per *result*, before the
loop, and shared by every unit that result commits. `pipeline.py`'s own
comment says why - moving the clock read inside the loop would fold `_speak`'s
translate-and-synthesise time into the second and later units' `asr_ms` on a
multi-unit result, which is not what that column measures.

**`transcript.py` did not absorb this for free - it had a real bug, now
fixed.** "What needs no change" claims `metrics.py`, `transcript.py` and
`cost.py` "all absorb ~5 s commits as they stand". True for the first and
third; false for `transcript.py`, and this is the claim that hid a real
regression. `_emit` (`segment.py`) originally took `t_start` from the running
span watermark unconditionally, so a final whose utterance committed *nothing*
early - every turn of ordinary conversation - still chained its `t_start` from
the *previous* utterance's end instead of carrying `t_start == t_end ==
result.t_end` the way `FinalsOnlySegmenter` does. Every row's start moved back
by one utterance, and `transcript.render_markdown` sorts on `t_start`, so an
interleaved two-way transcript came out reordered on the default path, for
every call. Fixed in commit `17ab049`: `_emit` now takes `t_start` from its
caller, and `_finalise` chains from the previous piece's span only when the
utterance was actually committed in pieces; a final that committed nothing
early carries a whole-utterance span, span included, exactly what
`FinalsOnlySegmenter` would have produced. A parametrised replay of the real
`turns` and `medium` captures against both segmenters
(`test_a_capture_with_no_early_commits_is_identical_to_finals_only`,
`test_segment.py`) now pins the equivalence, spans included, so this cannot
regress silently again.

**The count was the bug: `_committed` is gone, replaced by `_spoken`.**
Everything in the paragraph below, and the "how many words have been
committed" in the design above, was falsified by the first real call ever run
against this feature. Chirp does not only extend a hypothesis - it re-windows
it mid-utterance, reporting the same sentence from a later word on, which the
offline experiment never saw because its interims were purely additive.
Against a count that shift is unrecoverable: the zero-agreement rule below
read a re-window as a dead stream, discarded the commit, and the final of that
same utterance then found nothing committed and re-spoke everything. Roughly
15% of a five-minute call was verbatim repeats of speech up to a minute old,
in three bursts of 50 to 110 words, with the session log reading "discarding
69 / 55 / 14 / 83 committed word(s)".

The fix replaces both concepts with one. `_spoken` holds the tokens this
utterance has actually emitted, and every candidate - an interim's growth or a
final's text - is aligned against it by CONTENT before anything is emitted:
the largest k for which the last k spoken keys equal the candidate's first k,
emit `candidate[k:]`. A stream restart is then just `k == 0`, which emits the
new utterance whole - the behaviour the discards existed to produce - so both
discard rules are deleted rather than ported. `_overlap` carries one more arm,
for a hypothesis that SHRANK: a candidate wholly contained in what was spoken
adds nothing and emits nothing, which is what `agreed <= len(committed)` used
to do positionally. The window is capped at `_SPOKEN_LIMIT = 400` tokens,
trimmed from the front, because the search is quadratic and an utterance on a
stream that never finalises is unbounded; 400 is nearly six times the largest
overlap ever measured (69). Cost, stated because it is real: an edit INSIDE
the spoken text - a word substituted, not appended - breaks the alignment to
zero and the final goes out whole, where the count rule emitted only the tail.
That is logged at `warning` and is the one shape where the old rule did
better; the shape that actually happens on a real call is the re-window, and
it is the opposite way round.

**Superseded, and kept for the reasoning: recovery from a stream restart
mid-utterance.**
`RecognitionWorker` (`asr.py`) rebuilds its streaming connection every
`MAX_STREAM_SECONDS` and after any non-fatal error, and neither path
guarantees a final for the utterance in progress - so a commit made before the
break can still be sitting in `_committed` when the *next* utterance's
hypotheses arrive, with nothing in the `AsrResult` values themselves to say a
restart happened (timestamps stay monotonic across a rotation by
construction). `_finalise` and `_interim` (`segment.py`) both now detect this
the only way available - zero word agreement between the stale commit and the
new utterance's tokens - and discard the stale commit rather than slicing by
its length, logging at `warning` because this is speech potentially lost, not
a tail being revised. In `_finalise` the check runs *before* the remainder is
sliced, deliberately: the worst case (a final shorter than, or sharing nothing
with, the stale commit) has no remainder to emit, so checking after would
leave the loudest available sign that committing early went wrong completely
unlogged. `_interim` gained the mirror of this check later (commit
`751576e`), fixing the case where the discard fired only on the final side and
the interim path below it was left re-slicing a new utterance's opening words
against the stale, already-invalidated commit. Neither behaviour is mentioned
above; both are load-bearing.

**Testing section correction.** "Four mutations must each break a named test"
lists changing the agreement count from 2 to 1 as one of them, implicitly
naming `test_the_real_capture_commits_nothing_the_final_contradicted` as the
test that catches it. It does not reliably: its own docstring states that
under a one-agreement mutation, the measured capture still never produces the
literal contradicted substring, for an unrelated reason (the mutated
segmenter locks in the earlier hypothesis's punctuation before the later one
arrives), so an assertion against that one string is not evidence the
agreement count is actually 2. The tests that do catch it are
`test_the_real_monologue_is_committed_in_pieces_instead_of_one_block` (unit
count changes from 6), `test_the_real_capture_loses_no_words` (the word list
stops matching), and `test_the_real_monologues_spans_match_the_measured_capture`
(the pinned spans move) - all in `test_segment.py`.
