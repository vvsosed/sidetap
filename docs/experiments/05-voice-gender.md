# Experiment 5 — is there a male and a female voice for every language in the table?

Run on 2026-09-21, before `--voice-in-gender` / `--voice-out-gender` were
written, for the same reason Experiment 4 exists: the table was about to gain
eight voice names that had never been sent to the API.

**Question.** Does every language in `run.py`'s voice table have both a male
and a female Chirp 3 HD voice, and does the API agree with Google's published
gender for each one?

**Why it matters.** The table shipped one voice per language — Charon for
English, Kore for everything else. Selecting by gender doubles it to sixteen
names, and the eight new ones (`ru-RU-Chirp3-HD-Charon`,
`de-DE-Chirp3-HD-Charon`, …) had never been used. An invalid voice name is a
fatal `InvalidArgument` on the first utterance of the call, which is the worst
possible moment to find out. Experiment 4 verified the eight names then in
use; it could not verify these.

Taking the gender mapping from Google's documentation was the alternative, and
that page is not reliable on its own: its own summary line says "Total: 30
voices (15 male, 15 female)" while the table above it lists 16 male and 14
female names. The API reports `ssml_gender` per voice, so there was no need to
trust prose.

**Method.** `ListVoices` against both the `eu` and `global` endpoints — the
same read-only call `doctor.py` already makes, free, and touching no
credentials. Filter to `Chirp3-HD`, group each of the eight locales by the
`ssml_gender` the API returns, and check Charon and Kore specifically. No
`gcloud auth` was run.

Then, separately, **synthesise with all sixteen**. Experiment 4 treated "the
name is listed" and "streaming synthesis accepts it" as two questions, and so
does this: a `ListVoices` entry is a catalogue row, not a guarantee that
`StreamingSynthesize` will take it. One short phrase per voice in that voice's
own language, discarding a warm-up call.

**Result.** Both endpoints returned identical answers: 2066 voices, 1568 of
them Chirp 3 HD. All sixteen names exist, and every one reports the gender
expected — Charon `MALE`, Kore `FEMALE`, in all eight locales.

Two findings beyond the question asked:

**1. The API's gender labels match Google's per-voice table exactly.** Zero
disagreements across the full 30-voice set. It is only the documentation's
*summary* that is wrong; the real split is **16 male / 14 female**.

**2. `ru-RU` is the only locale of 53 with fewer than 30 Chirp 3 HD voices.**
It has eight:

| | |
|---|---|
| male | Charon, Fenrir, Orus, Puck |
| female | Aoede, Kore, Leda, Zephyr |

Every other locale — including `uk-UA`, `pl-PL` and the rest of the table —
carries the full 30. This matters more than it looks: `ru-RU` is this
project's canonical example language, used in nearly every command line in the
README and the specs. Anything that widens the voice table beyond one pair per
language has to be checked against `ru-RU` first, because it is the one place
the published catalogue overstates what is available.

**Synthesis.** All sixteen produced audio — 232-379 ms warm, in line with
Experiment 4's 186-267 ms warm median for the two voices it measured — and
male and female produced *different* audio in every language, which is what
rules out the API silently falling back to one voice for both.

**Consequence.**

1. Charon and Kore are the pair for every language, because they are the pair
   that exists in every language. Recorded in the table's comment.
2. The gender flags ship as designed. No table entry needed a substitute.
3. `GENDERS = ("male", "female")` in `tts.py` is the whole set — no Chirp 3 HD
   voice reports `NEUTRAL` or `UNSPECIFIED`, so a third choice would be a
   value the service cannot satisfy.
4. `DEFAULT_VOICES` was renamed `VOICES` in this change; Experiment 4 refers
   to it under the old name.

**Not answered.** Whether the two voices are equally intelligible over a call,
and whether time-to-first-audio differs between them — Experiment 4 measured
Kore at 267 ms and Charon at 186 ms median, but in different languages, so the
gap is not attributable to the voice. Nothing in this change depends on it.

The listing script was a throwaway, as in Experiment 4; only the result is
recorded here.
