# Experiment 6 — what do Chirp's interim results actually look like?

Run on 2026-09-22, before `LocalAgreementSegmenter` was written, because the
whole feature rests on assumptions about interim results that nothing in this
repository had ever observed. `FinalsOnlySegmenter` discards interims, so they
have been enabled and unexamined since v1.

**Question.** On a long pauseless utterance: does recognition finalise on its
own, how often do interims arrive, do they carry punctuation, and does an
already-emitted prefix ever get revised?

**Why it matters.** LocalAgreement-2 commits the longest common prefix of two
consecutive interim hypotheses. If interims arrive rarely it buys nothing; if
prefixes are revised it speaks words a later revision deletes, and the
synthesised voice cannot take a word back. The spec
(`2026-09-17-sidetap-design.md:72`) rejected LA-2 for v1 pending exactly this
measurement, and named the transcript's latency column as the evidence — but
sidetap has never been run against a real call, so that column is empty.

**Method.** Chirp 3 HD synthesised the source audio at 16 kHz, in pieces
because the API rejects a single sentence this long ("This request contains
sentences that are too long"), each piece trimmed of leading and trailing
silence and concatenated — so what reaches recognition is continuous speech
with no pause the VAD could gate and no gap the recogniser could endpoint on.

That audio was then streamed into the real streaming recognition path **at
real time**, in 100 ms blocks through the same `SilenceGate` and 2 s keepalive
`RecognitionWorker` uses. Pacing is not a detail: feeding the stream as fast
as the loop can push it would make both the interim cadence and the
endpointing decisions unrepresentative. Every result was logged on arrival
with its wall time, `result_end_offset`, `is_final` and text.

Three sources: a 34.8 s pauseless monologue, a 7.9 s utterance, and four short
turn-taking sentences separated by 1.2 s of silence. The monologue was run
four times. No `gcloud auth` was run; the probe used the project's existing
ADC, and `sidetap doctor` reported all checks passing before and after.

## Result

**1. Interims arrive every 5.0 s of *sent* audio, and the cadence is
deterministic.** Across four monologue runs the `result_end_offset` values
were identical to the tenth of a second every time — 5.0, 6.04, 11.12, 16.12,
21.12, 26.12, 31.12, 34.7. The clock that drives them is audio position, not wall time, and because the
silence gate drops non-speech before sending, that means one interim per 5 s
of *speech*.

**2. Recognition lag is 1.04 s rising to 1.99 s** from `result_end_offset` to
the result arriving, drifting upward over the length of one stream. Wall-clock
arrival varies by a few hundred milliseconds between runs; the offsets do not.
Every figure below is from the committed capture.

**3. The monologue stall is real, and larger than the code comments assume.**
34.8 s of continuous speech produced a final at 7.43 s covering the first 6 s,
then **29.3 s with no final at all**, then one final at 36.69 s carrying 28.7 s
of audio as a single unit. Content spoken at second 6 is not heard until second
37.

The first final is itself an artifact worth naming: it fired on a 400 ms
residual pause left by the concatenation. Genuine pauseless speech does not
finalise — it just keeps extending.

**4. Short utterances produce no interims whatsoever.** All four turn-taking
sentences (2-4 s of audio each) came back as finals with not one interim
between them. The 7.9 s utterance produced exactly one interim, and one is not
enough to agree with.

This has two consequences, and the second is not about LA-2 at all:

- **LocalAgreement-2 cannot help short sentences**, because there is nothing
  to compare. The 0.5–1 s that `segment.py`'s docstring and the spec both
  quote as its payoff describes a case Chirp does not serve. What LA-2 can
  address is the monologue, and only the monologue.
- **The TUI's live interim display never fires in ordinary conversation.**
  `2026-09-17-sidetap-design.md:317-320` keeps interims enabled on the grounds
  that "they feed the TUI", and that full-replacement routing removes the
  user's "they are talking right now" signal so the interim line is what gives
  it back. For normal turn-taking there are no interims to show. Out of scope
  here; recorded because nothing else would have caught it.

**5. Interims are additive, and the one revision landed on the final.** Word
prefixes against the previous hypothesis:

| transition | common word prefix | revised tail |
|---|---|---|
| 12.16 → 17.29 | 14 w | 0 |
| 17.29 → 22.45 | 28 w | 0 |
| 22.45 → 27.58 | 39 w | 0 |
| 27.58 → 32.69 | 51 w | 0 |
| 32.69 → 36.69 (final) | 51 w | **13 w** |

The final inserted a word ("а") thirteen words from the end of text that had
appeared in one interim already. **This is the measured justification for the
"-2"**: requiring two agreements commits 51 words and leaves the revised tail
uncommitted. Requiring one would have spoken text the final then contradicted.

**6. Three things change between interims and must not be compared.** All
three were observed:

| | seen as |
|---|---|
| capitalisation | `Что` → `что` |
| punctuation | `сверхурочно.` → `сверхурочно,` |
| trailing partial word | a word present in one hypothesis, extended in the next |

The capitalisation case is the one that decides the implementation: comparing
raw text would find a common prefix of **zero characters** between the 12.16 s
and 17.29 s hypotheses, and commit nothing for the entire monologue. The
comparison key must be lowercased and stripped of punctuation; only the
surface form is translated.

**7. Interims do carry punctuation**, so clause boundaries are available to
cut on — but per 6 they are revisable, so punctuation may be used to choose a
cut point and never as part of the agreement key.

## Consequence

1. LocalAgreement-2 is worth building, for the monologue case alone. On this
   sample it converts "up to 31 s late" into "steadily ~6.7 s behind".
2. **It is self-limiting with no constant to tune.** Below roughly 11 s of
   continuous speech there are not two interims to agree, so the segmenter
   emits nothing early and behaviour is identical to `FinalsOnlySegmenter`.
   No length threshold is needed, and none should be added.
3. Commit cadence is ~5 s, so a committed unit is a substantial clause. The
   transcript, the metrics pane and `cost.py` all absorb this without change.
4. The agreement count is fixed at 2 by measurement, not by the name.
5. `segment.py`'s "roughly 0.5-1 s" and the spec's "measure first" are both
   superseded by this document.

## Raw capture is committed, unlike Experiments 4 and 5

`tests/fixtures/chirp_interims.json` holds every result from all three
sources. Those experiments recorded one-line facts that anyone could re-derive
for free; this one is a timed sequence of hypotheses that costs a live
streaming call to reproduce, and it is the only evidence available for whether
a prefix is safe to commit. It is real API output, in the same spirit as
`pw_dump_real.json`: a hand-written interim sequence would share exactly the
assumptions it is meant to test.

The probe script itself was a throwaway, as in Experiments 4 and 5.

## Not answered

The audio is synthetic. Chirp 3 HD speech has no disfluencies, no overlapping
speakers, no background noise, and steadier prosody than a person — all of
which plausibly affect both endpointing and how often a prefix gets revised.
What this establishes is the *structure*: the 5 s cadence, the absence of
interims on short utterances, and that revisions land on the final rather than
between interims. Whether a real speaker revises more often belongs in
`docs/manual-smoke.md`, which remains the only place the result can be judged.

Nor does it measure the far end of the pipeline: whether clause-by-clause
translation of a monologue reads worse than translating it whole, which is the
cost the spec named and the one thing no offline probe can settle.
