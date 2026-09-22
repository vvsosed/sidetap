# LocalAgreement-2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start speaking a long utterance part-way through instead of waiting
for a final that may be 29 seconds away, by committing the longest word prefix
two consecutive Chirp interim hypotheses agree on.

**Architecture:** A new `LocalAgreementSegmenter` behind the existing
one-method `Segmenter` protocol emits one `Unit` per committed clause. Each
clause stays its own playout `Utterance`, so the lag cap keeps working; a
bounded flag on `Playout` keeps the duck closed across the gap between
clauses, reusing the existing `STARVE_LIMIT_TICKS` so it always fails open.

**Tech Stack:** Python 3.13, `uv`, pytest. No new dependencies. The design is
`docs/superpowers/specs/2026-09-22-local-agreement-2-design.md`; the
measurements it rests on are `docs/experiments/06-interim-cadence.md` and the
committed capture `tests/fixtures/chirp_interims.json`.

**Before starting:** read the spec. Read `docs/experiments/06-interim-cadence.md`
findings 5 and 6 — they are the reason the comparison key exists and the
reason the agreement count is 2. Every command runs from the repo root and
uses `uv run`; never `pip install`, never activate `.venv` by hand.

**Branch:** work continues on `local-agreement-2`, which already holds the
spec, the experiment and the fixture.

---

### Task 1: `Unit.continues`

The bit that tells playout more of this speech run is coming. It rides on the
value type so the `Segmenter` protocol stays one method.

**Files:**
- Modify: `sidetap/types.py:86-110` (the `Unit` dataclass)
- Test: `tests/test_types.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_types.py`:

```python
def test_a_unit_does_not_continue_by_default():
    """FinalsOnlySegmenter emits whole utterances, so nothing follows them.

    The flag is opt-in for exactly that reason: a segmenter that does not know
    about speech runs cannot accidentally hold the duck closed.
    """
    from sidetap.types import AsrResult, Direction, Unit

    result = AsrResult(
        direction=Direction.IN, text="hi", is_final=True, t_start=1.0, t_end=2.0
    )
    assert Unit.from_result(result).continues is False
    assert Unit(
        direction=Direction.IN, text="hi", t_start=1.0, t_end=2.0, continues=True
    ).continues is True
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_types.py::test_a_unit_does_not_continue_by_default -q`
Expected: FAIL — `TypeError: Unit.__init__() got an unexpected keyword argument 'continues'`

- [ ] **Step 3: Add the field**

In `sidetap/types.py`, inside `class Unit`, after `t_end: float`:

```python
    # More of this speech run is on its way: a committed clause that is not
    # the end of its utterance. Playout uses it to keep the duck closed across
    # the gap before the next clause, rather than reading an empty queue as
    # "the translation is over" and letting the original through mid-sentence.
    # False for FinalsOnlySegmenter, which only ever emits whole utterances.
    continues: bool = False
```

- [ ] **Step 4: Run it and watch it pass**

Run: `uv run pytest tests/test_types.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sidetap/types.py tests/test_types.py
git commit -F - <<'EOF'
Let a Unit say more of its speech run is coming

The Segmenter protocol stays one method: the bit rides on the value type
rather than widening the seam.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 2: Tokenising, and the key that is compared

Experiment 6 finding 6: capitalisation and punctuation change between
interims. A character-level comparison finds a common prefix of **zero** for
the real capture and would commit nothing at all.

**Files:**
- Modify: `sidetap/segment.py`
- Test: `tests/test_segment.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_segment.py`:

```python
from sidetap.segment import _agreed, _tokens


def test_the_key_ignores_case_and_punctuation():
    """Both were observed changing between two interims of one utterance.

    docs/experiments/06-interim-cadence.md finding 6: "Что" became "что", and
    "сверхурочно." became "сверхурочно,". Comparing surfaces would read either
    as a disagreement and commit nothing for the whole monologue.
    """
    assert [k for _, k in _tokens("Что сверхурочно.")] == ["что", "сверхурочно"]


def test_the_surface_form_keeps_case_and_punctuation():
    """It is what gets translated, so it must stay intact."""
    assert [s for s, _ in _tokens("Что сверхурочно.")] == ["Что", "сверхурочно."]


def test_agreement_counts_words_not_characters():
    a = _tokens("Что у нас есть")
    b = _tokens("что у нас было")
    assert _agreed(a, b) == 3


def test_a_word_extended_in_the_next_hypothesis_ends_the_agreement():
    """A truncated trailing word needs no special case: its key differs."""
    assert _agreed(_tokens("без поним"), _tokens("без понимания")) == 1
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_segment.py -q`
Expected: FAIL — `ImportError: cannot import name '_agreed' from 'sidetap.segment'`

- [ ] **Step 3: Implement**

In `sidetap/segment.py`, after the imports:

```python
def _tokens(text: str) -> list[tuple[str, str]]:
    """Split into (surface, key) pairs. Only the key is ever compared.

    The surface keeps case and punctuation, because it is what reaches the
    translator. The key drops both, because Experiment 6 observed both
    changing between two hypotheses whose words were otherwise identical -
    "Что" to "что", "сверхурочно." to "сверхурочно,". Comparing surfaces finds
    a common prefix of zero characters on the real capture.

    A token whose surface is punctuation alone keeps an empty key. It still
    occupies a position, so a dash present in one hypothesis and absent from
    the next ends the agreement there rather than silently shifting it - which
    errs toward committing less.
    """
    return [
        (surface, "".join(ch for ch in surface.lower() if ch.isalnum()))
        for surface in text.split()
    ]


def _agreed(previous: list[tuple[str, str]], current: list[tuple[str, str]]) -> int:
    """How many leading words two hypotheses agree on, by key."""
    n = 0
    for (_, previous_key), (_, current_key) in zip(previous, current):
        if previous_key != current_key:
            break
        n += 1
    return n
```

- [ ] **Step 4: Run them and watch them pass**

Run: `uv run pytest tests/test_segment.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sidetap/segment.py tests/test_segment.py
git commit -F - <<'EOF'
Compare interim hypotheses by word key, not by text

Experiment 6 observed capitalisation and punctuation changing between two
hypotheses whose words were identical. A character comparison finds a
common prefix of zero on the real capture and would commit nothing.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 3: `LocalAgreementSegmenter` — commit what two interims agree on

**Files:**
- Modify: `sidetap/segment.py`
- Test: `tests/test_segment.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_segment.py`:

```python
from sidetap.segment import LocalAgreementSegmenter


def _interim(text, t_end, direction=Direction.IN):
    return AsrResult(
        direction=direction, text=text, is_final=False,
        t_start=t_end, t_end=t_end,
    )


def test_one_interim_commits_nothing():
    """There is nothing to agree with.

    This is what makes the feature self-limiting: Experiment 6 measured one
    interim per 5 s of speech, so two agreeing hypotheses need ~11 s of
    continuous speech. Below that the segmenter is FinalsOnlySegmenter, with
    no threshold constant anywhere.
    """
    segmenter = LocalAgreementSegmenter()
    assert segmenter.feed(_interim("что у нас есть", 5.0)) == []


def test_two_agreeing_interims_commit_the_agreed_prefix():
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    units = segmenter.feed(_interim("что у нас есть, несколько важных", 10.0))
    assert [u.text for u in units] == ["что у нас есть,"]


def test_a_committed_prefix_is_not_committed_again():
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    segmenter.feed(_interim("что у нас есть, несколько важных задач,", 10.0))
    units = segmenter.feed(
        _interim("что у нас есть, несколько важных задач, которые нужно", 15.0)
    )
    assert [u.text for u in units] == ["несколько важных задач,"]


def test_a_committed_prefix_says_more_is_coming():
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    units = segmenter.feed(_interim("что у нас есть, несколько", 10.0))
    assert [u.continues for u in units] == [True]


def test_a_disagreement_commits_nothing():
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть", 5.0))
    assert segmenter.feed(_interim("это совсем другое", 10.0)) == []


def test_the_two_directions_do_not_share_state():
    """Load-bearing now, unlike the same test for FinalsOnlySegmenter.

    One instance fed by both directions would interleave two conversations and
    commit a prefix of neither.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    assert segmenter.feed(
        _interim("this is english,", 10.0, direction=Direction.OUT)
    ) == []
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_segment.py -q`
Expected: FAIL — `ImportError: cannot import name 'LocalAgreementSegmenter'`

- [ ] **Step 3: Implement the class (interim path only for now)**

Append to `sidetap/segment.py`:

```python
class LocalAgreementSegmenter:
    """Commit the longest word prefix two consecutive interims agree on.

    **One instance per direction, never shared.** Unlike FinalsOnlySegmenter
    this really does hold state, and one instance fed by both directions would
    interleave two conversations and commit a prefix of neither.

    Two agreements, not configurable, and the count is measured rather than
    taken from the name: the final in Experiment 6 inserted a word thirteen
    from the end of text that had already appeared in one interim. Two
    agreements leave that tail uncommitted; one would have spoken text the
    final then contradicted, and a synthesised voice cannot take a word back.
    """

    def __init__(self):
        self._previous: list[tuple[str, str]] = []
        self._previous_t_end = 0.0
        # The tokens already emitted for the utterance in progress. Kept as
        # tokens rather than a count so a final can be checked against them.
        self._committed: list[tuple[str, str]] = []
        self._span_start = 0.0

    def feed(self, result: AsrResult) -> list[Unit]:
        if result.is_final:
            return self._finalise(result)
        return self._interim(result)

    def _interim(self, result: AsrResult) -> list[Unit]:
        previous, previous_t_end = self._previous, self._previous_t_end
        current = _tokens(result.text)
        self._previous, self._previous_t_end = current, result.t_end

        agreed = _agreed(previous, current)
        if agreed <= len(self._committed):
            return []
        growth = current[len(self._committed) : agreed]

        # t_end is the OLDER hypothesis's audio position, because that is the
        # point through which this text is confirmed - not where the speaker
        # has since got to. The transcript's latency column is the spec's
        # stated evidence for this whole change, so overstating freshness here
        # would corrupt the one number it is judged by.
        unit = self._emit(result.direction, growth, previous_t_end, continues=True)
        self._committed = self._committed + growth
        return [unit]

    def _emit(self, direction, tokens, t_end, *, continues):
        unit = Unit(
            direction=direction,
            text=" ".join(surface for surface, _ in tokens),
            t_start=self._span_start,
            t_end=t_end,
            continues=continues,
        )
        self._span_start = t_end
        return unit
```

Also add `_finalise` as a stub so the class imports — Task 5 fills it in:

```python
    def _finalise(self, result: AsrResult) -> list[Unit]:
        return []
```

- [ ] **Step 4: Run them and watch them pass**

Run: `uv run pytest tests/test_segment.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sidetap/segment.py tests/test_segment.py
git commit -F - <<'EOF'
Commit the prefix two consecutive interims agree on

The interim path only; finals still commit nothing, which the next commit
fixes. Two agreements is measured, not nominal - see Experiment 6.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 4: Cut the committed growth at a clause boundary

**Files:**
- Modify: `sidetap/segment.py`
- Test: `tests/test_segment.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_segment.py`:

```python
def test_the_commit_is_cut_at_the_last_clause_boundary():
    """A fragment reaching the translator mid-clause is the cost the v1 spec
    named. Interims do carry punctuation, so there is usually a cut available.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("мы рискуем снова оказаться в ситуации, когда", 5.0))
    units = segmenter.feed(
        _interim("мы рискуем снова оказаться в ситуации, когда команда", 10.0)
    )
    assert [u.text for u in units] == ["мы рискуем снова оказаться в ситуации,"]


def test_text_held_back_by_the_cut_is_committed_later():
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("в ситуации, когда", 5.0))
    segmenter.feed(_interim("в ситуации, когда команда работает", 10.0))
    units = segmenter.feed(
        _interim("в ситуации, когда команда работает сверхурочно, а", 15.0)
    )
    assert [u.text for u in units] == ["когда команда работает"]


def test_growth_with_no_boundary_at_all_is_committed_whole():
    """Holding it back would reproduce the stall this change exists to remove.

    Five seconds of speech with no punctuation is exactly the case where
    waiting hurts, so the rule fails toward speaking.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("мы рискуем снова оказаться", 5.0))
    units = segmenter.feed(_interim("мы рискуем снова оказаться в ситуации", 10.0))
    assert [u.text for u in units] == ["мы рискуем снова оказаться"]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_segment.py -q`
Expected: FAIL — `test_the_commit_is_cut_at_the_last_clause_boundary` asserts
`["мы рискуем снова оказаться в ситуации, когда"]` != the expected cut.

- [ ] **Step 3: Implement**

In `sidetap/segment.py`, beside `_agreed`:

```python
# What ends a clause. Interims carry punctuation (Experiment 6 finding 7), so
# a boundary is almost always available inside five seconds of speech.
_BOUNDARY = ",.;:!?…"


def _cut(tokens: list[tuple[str, str]]) -> int:
    """How many of these tokens to commit now.

    Everything up to and including the last clause boundary, so a fragment
    does not reach the translator mid-clause - the word-order cost the v1 spec
    named as LocalAgreement-2's main downside.

    With no boundary anywhere, commit the lot. Holding it back would reproduce
    the stall this exists to remove: five seconds of speech with no punctuation
    is precisely where waiting hurts. One rule, no constant, and it fails
    toward speaking.
    """
    for i in range(len(tokens) - 1, -1, -1):
        if tokens[i][0][-1:] in _BOUNDARY:
            return i + 1
    return len(tokens)
```

Then in `_interim`, replace

```python
        growth = current[len(self._committed) : agreed]
```

with

```python
        growth = current[len(self._committed) : agreed]
        growth = growth[: _cut(growth)]
        if not growth:
            return []
```

- [ ] **Step 4: Run them and watch them pass**

Run: `uv run pytest tests/test_segment.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sidetap/segment.py tests/test_segment.py
git commit -F - <<'EOF'
Cut a commit at the last clause boundary inside it

With no boundary anywhere, commit the lot rather than hold: five seconds of
speech with no punctuation is exactly where waiting hurts.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 5: The final flushes the remainder and resets

**Files:**
- Modify: `sidetap/segment.py`
- Test: `tests/test_segment.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_segment.py`:

```python
def _final_result(text, t_end, direction=Direction.IN):
    return AsrResult(
        direction=direction, text=text, is_final=True, t_start=t_end, t_end=t_end,
    )


def test_a_final_emits_only_what_was_not_committed():
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    segmenter.feed(_interim("что у нас есть, несколько задач", 10.0))
    units = segmenter.feed(_final_result("что у нас есть, несколько задач.", 15.0))
    assert [u.text for u in units] == ["несколько задач."]
    assert [u.continues for u in units] == [False]


def test_a_final_is_not_cut_at_a_boundary():
    """Nothing is left to wait for, so nothing is held back."""
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("в ситуации,", 5.0))
    segmenter.feed(_interim("в ситуации, когда команда", 10.0))
    units = segmenter.feed(_final_result("в ситуации, когда команда работает", 15.0))
    assert [u.text for u in units] == ["когда команда работает"]


def test_a_final_that_adds_nothing_emits_nothing():
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    segmenter.feed(_interim("что у нас есть, несколько задач", 10.0))
    segmenter.feed(_interim("что у нас есть, несколько задач,", 15.0))
    assert segmenter.feed(_final_result("что у нас есть, несколько задач,", 20.0)) == []


def test_a_final_resets_the_state_for_the_next_utterance():
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    segmenter.feed(_interim("что у нас есть, задач", 10.0))
    segmenter.feed(_final_result("что у нас есть, задач.", 15.0))
    # A new utterance: one interim again commits nothing.
    assert segmenter.feed(_interim("совсем другое,", 20.0)) == []


def test_a_whitespace_only_final_emits_nothing():
    segmenter = LocalAgreementSegmenter()
    assert segmenter.feed(_final_result("   ", 5.0)) == []


def test_spans_are_contiguous_and_end_where_the_text_was_confirmed():
    """t_end is the OLDER hypothesis's position, not the newer one's.

    Reading the newer one would report a committed clause as a second old when
    it is six seconds old.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    first = segmenter.feed(_interim("что у нас есть, задач больше", 10.0))[0]
    second = segmenter.feed(_final_result("что у нас есть, задач больше.", 15.0))[0]
    assert (first.t_start, first.t_end) == (0.0, 5.0)
    assert (second.t_start, second.t_end) == (5.0, 15.0)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_segment.py -q`
Expected: FAIL — `_finalise` returns `[]`, so
`test_a_final_emits_only_what_was_not_committed` gets `[]`.

- [ ] **Step 3: Replace the `_finalise` stub**

First add a logger at the top of `sidetap/segment.py`, after the existing
imports:

```python
import logging

log = logging.getLogger(__name__)
```

Then replace the whole `_finalise` stub with:

```python
    def _finalise(self, result: AsrResult) -> list[Unit]:
        tokens = _tokens(result.text)
        committed = self._committed

        self._previous = []
        self._previous_t_end = result.t_end
        self._committed = []

        remainder = tokens[len(committed) :]
        if not remainder:
            # Everything this final carried had already been committed, or it
            # carried nothing at all. The span still advances, so the next
            # utterance's first commit does not claim to start back here.
            self._span_start = result.t_end
            return []

        if _agreed(tokens, committed) < len(committed):
            # A final may revise text already committed - Experiment 6 saw one
            # insert a word thirteen from the end. Nothing can be un-spoken,
            # so emit from where committing stopped rather than from where the
            # revision starts: losing a word the listener will not hear beats
            # repeating a clause they already heard.
            log.debug(
                "%s: final revised committed text; emitting from the commit point",
                result.direction.value,
            )

        return [self._emit(result.direction, remainder, result.t_end, continues=False)]
```

- [ ] **Step 4: Run them and watch them pass**

Run: `uv run pytest tests/test_segment.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sidetap/segment.py tests/test_segment.py
git commit -F - <<'EOF'
Flush the uncommitted remainder when a final arrives

A final may revise text already committed, and nothing can be un-spoken, so
emit from the commit point rather than the divergence: losing a word beats
repeating a clause.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 6: Replay the real capture — the load-bearing test

Every test so far was hand-written from the same assumptions the code was, so
they share them. This one cannot: `tests/fixtures/chirp_interims.json` is real
API output, committed for the reason `pw_dump_real.json` is.

**Files:**
- Test: `tests/test_segment.py`
- Read: `tests/fixtures/chirp_interims.json` (already committed)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_segment.py`:

```python
import json
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "chirp_interims.json"


def _capture(name):
    for probe in json.loads(FIXTURE.read_text()):
        if probe["name"] == name:
            return probe["results"]
    raise AssertionError(f"no {name!r} in the capture")


def _replay(name):
    segmenter = LocalAgreementSegmenter()
    units = []
    for row in _capture(name):
        result = AsrResult(
            direction=Direction.IN, text=row["text"], is_final=row["final"],
            t_start=row["end_offset"], t_end=row["end_offset"],
        )
        units.extend(segmenter.feed(result))
    return units


def test_the_real_monologue_is_committed_in_pieces_instead_of_one_block():
    """Today's behaviour on this capture is two units, 29.3 s apart.

    Six is pinned rather than bounded: this capture is fixed, so the number is
    a fact about it, and a range would hide a segmenter that started
    committing twice as often or half as often.
    """
    units = _replay("monologue")
    assert len(units) == 6
    assert [u.continues for u in units] == [False, True, True, True, True, False]


def test_the_real_capture_commits_nothing_the_final_contradicted():
    """The measured justification for the "-2".

    The final inserted "а" into "сверхурочно, результат", text that had already
    appeared in one interim. One agreement would have spoken it. Two must not.
    """
    spoken = " ".join(u.text for u in _replay("monologue"))
    assert "сверхурочно, результат" not in spoken


def test_the_real_capture_survives_the_case_change():
    """"Что" became "что" between two hypotheses whose words were identical.

    A surface comparison finds a common prefix of zero here and commits
    nothing for the entire monologue - so this is the test that fails if the
    key ever stops being lowercased.
    """
    units = _replay("monologue")
    assert "несколько важных задач" in " ".join(u.text for u in units)


def test_the_real_capture_loses_no_words():
    """Committing early must not drop or duplicate text."""
    import re

    final_text = [r["text"] for r in _capture("monologue") if r["final"]]
    expected = re.findall(r"\w+", " ".join(final_text).lower())
    got = re.findall(r"\w+", " ".join(u.text for u in _replay("monologue")).lower())
    assert got == expected


def test_the_real_short_turns_commit_nothing_early():
    """They produce no interims at all, so LocalAgreement-2 cannot engage.

    This is what makes ordinary conversation byte-identical to today.
    """
    units = _replay("turns")
    assert all(u.continues is False for u in units)
    assert len(units) == 4


def test_the_real_medium_utterance_commits_nothing_early():
    """7.9 s produces one interim - not enough to agree with."""
    units = _replay("medium")
    assert [u.continues for u in units] == [False]
```

- [ ] **Step 2: Run them**

The algorithm in Tasks 2-5 was prototyped against this fixture before the plan
was written, so the expected output is known rather than hoped for. The
monologue must come out as exactly these six units, spans included:

| span | continues | text (truncated) |
|---|---|---|
| 0.0 → 6.04 | False | `Я хочу рассказать, как мы планируем запустить…` |
| 6.04 → 11.12 | True | `что у нас есть несколько важных задач, которые…` |
| 11.12 → 16.12 | True | `Я думаю, что нам стоит начать с анализа текущей ситуации,` |
| 16.12 → 21.12 | True | `затем перейти к обсуждению бюджета и только после…` |
| 21.12 → 26.12 | True | `поскольку без понимания реальных затрат мы рискуем…` |
| 26.12 → 34.7 | False | `а результат никого не устраивает. И мне кажется,…` |

Note where the fifth unit stops and the sixth begins: `сверхурочно,` was
committed, `а результат` was not, and the final supplied it. That is the "-2"
rule doing its job on real data.

Run: `uv run pytest tests/test_segment.py -q -k real`
Expected: PASS. If `test_the_real_capture_loses_no_words` fails, the commit
arithmetic is wrong — fix it before going on; it is the test that proves the
segmenter is lossless, and no later task can substitute for it.

- [ ] **Step 3: Commit**

```bash
git add tests/test_segment.py
git commit -F - <<'EOF'
Replay the committed Chirp capture through the segmenter

Every other segmenter test was hand-written from the assumptions the code
was written from, so they share them. This one is real API output: it pins
the case change, the punctuation revision, and that the word the final
inserted was never committed early.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 7: The bounded duck hold

The one change outside the seam. Without it, IN leaks a burst of untranslated
original into every gap between committed clauses — roughly once every five
seconds through a monologue — because `tick()` reads an empty queue as "the
translation is over".

**Files:**
- Modify: `sidetap/playout.py` — `Playout.__init__`, `flush`,
  `_advance_locked`, plus a new `expect_continuation`
- Test: `tests/test_playout.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_playout.py`:

```python
def test_the_duck_holds_across_a_gap_when_more_of_the_run_is_coming():
    """The gap between two committed clauses is not the end of the sentence.

    Opening here would let a burst of the untranslated original through the
    middle of what the listener hears as one sentence.
    """
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    playout.submit(_translated(0.04))  # 2 chunks
    playout.expect_continuation(True)
    assert playout.tick() is True
    assert playout.tick() is True
    assert duck.is_open is False

    for _ in range(10):  # 200 ms of gap before the next clause
        assert playout.tick() is False
    assert duck.is_open is False


def test_the_duck_holds_while_the_next_clause_is_still_buffering():
    """The gap is not always an empty queue.

    The next clause is usually queued already and sitting under
    START_BUFFER_S, which reaches a different branch of _advance_locked.
    """
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    playout.submit(_translated(0.04))
    playout.expect_continuation(True)
    playout.tick()
    playout.tick()

    nxt = playout.begin(_unit(), "next")
    playout.append(nxt, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.1 / 2))  # 100 ms
    for _ in range(5):
        assert playout.tick() is False
    assert duck.is_open is False


def test_the_duck_opens_when_the_run_ends():
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    playout.submit(_translated(0.04))
    playout.expect_continuation(True)
    playout.tick()
    playout.tick()
    assert duck.is_open is False

    playout.expect_continuation(False)
    playout.tick()
    assert duck.is_open is True


def test_a_continuation_that_never_arrives_still_opens_the_duck():
    """The same bound as every other reason the duck stays shut.

    A duck stuck closed silences the person you are on a call with and leaves
    them talking to nobody, which CLAUDE.md names as worse than sidetap not
    working at all. Exactly STARVE_LIMIT_TICKS, because an off-by-one either
    way is a real bug here.
    """
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    playout.submit(_translated(0.04))
    playout.expect_continuation(True)
    playout.tick()
    playout.tick()
    assert duck.is_open is False

    for _ in range(STARVE_LIMIT_TICKS):
        playout.tick()
    assert duck.is_open is True


def test_flushing_clears_the_continuation_hold():
    """Bypass engaging and the drop-backlog hotkey both funnel through flush.

    Neither should leave the duck shut waiting for audio that was just thrown
    away.
    """
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    playout.submit(_translated(0.04))
    playout.expect_continuation(True)
    playout.tick()
    assert duck.is_open is False

    playout.flush()
    playout.tick()
    assert duck.is_open is True


def test_leaving_suppression_does_not_resume_a_stale_hold():
    """set_suppressed flushes on both edges, so the hold goes with the queue.

    The run it belonged to is minutes old by the time bypass is released.
    """
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    playout.submit(_translated(0.04))
    playout.expect_continuation(True)
    playout.tick()

    playout.set_suppressed(True)
    playout.set_suppressed(False)
    playout.tick()
    assert duck.is_open is True
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_playout.py -q -k "continuation or hold"`
Expected: FAIL — `AttributeError: 'Playout' object has no attribute 'expect_continuation'`

- [ ] **Step 3: Implement**

In `Playout.__init__`, after `self._starved_ticks = 0`:

```python
        self._continuation = False
```

Add the method, directly after `set_suppressed`:

```python
    def expect_continuation(self, value: bool) -> None:
        """More of the speech run in progress is on its way.

        Set while a segmenter commits one utterance clause by clause. The
        queue drains between clauses, and tick() would otherwise read an empty
        queue as "the translation is over" and open the duck - letting a burst
        of the untranslated original through the middle of what the listener
        hears as one sentence, roughly every five seconds of a monologue
        (docs/experiments/06-interim-cadence.md).

        Bounded by the same STARVE_LIMIT_TICKS as every other reason the duck
        stays shut, so a producer that dies mid-run cannot hold it closed for
        the rest of the call. That bound is not optional: a duck stuck closed
        silences the person you are talking to, which this module treats as
        worse than not working.
        """
        with self._lock:
            self._continuation = value
```

In `flush()`, immediately before `return count`:

```python
            # The run this belonged to is gone with the queue. Leaving it set
            # would hold the duck shut waiting for audio that was just thrown
            # away - and set_suppressed() flushes on both edges, so this is
            # also what stops a hold surviving a whole bypass.
            self._continuation = False
```

In `_advance_locked`, in the `elif self._queue:` branch, insert immediately
before `self._starved_ticks += 1`:

```python
            # A hold means the listener is mid-run and this is the gap before
            # the next clause, not the quiet before the first one. The duck
            # stays shut across it, under the same bound as everything else.
            starved = self._continuation
```

and replace the final `else:` branch — currently just
`self._starved_ticks = 0` — with:

```python
        else:
            if self._continuation:
                # Nothing queued at all, but the producer says more of this
                # run is coming. Hold the duck shut across the gap, bounded
                # exactly as above so it always fails open.
                starved = True
                self._starved_ticks += 1
                if self._starved_ticks >= STARVE_LIMIT_TICKS:
                    log.warning(
                        "%s playout: expected continuation never arrived in "
                        "%.1fs; reopening the duck",
                        self.direction.value,
                        STARVE_LIMIT_TICKS * CHUNK_MS / 1000,
                    )
                    self._continuation = False
                    self._starved_ticks = 0
                    starved = False
            else:
                self._starved_ticks = 0
```

- [ ] **Step 4: Run the whole playout suite**

Run: `uv run pytest tests/test_playout.py -q`
Expected: PASS, including every pre-existing test. If
`test_an_utterance_that_has_not_started_leaves_the_duck_open` now fails, the
`starved = self._continuation` line was written as an unconditional `True`.

- [ ] **Step 5: Commit**

```bash
git add sidetap/playout.py tests/test_playout.py
git commit -F - <<'EOF'
Hold the duck shut across the gap between committed clauses

Without it, committing a monologue clause by clause lets a burst of the
untranslated original through roughly every five seconds. Bounded by the
same STARVE_LIMIT_TICKS as every other reason the duck stays shut, so it
always fails open.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 8: Wire the pipeline — per-unit latency, and arming the hold

**Files:**
- Modify: `sidetap/pipeline.py` — `handle` (around `:118-133`) and `_speak`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pipeline.py`:

```python
from sidetap.types import Unit


class _OneUnitSegmenter:
    """Emits a fixed Unit on every interim, nothing on a final."""

    def __init__(self, unit):
        self.unit = unit

    def feed(self, result):
        return [] if result.is_final else [self.unit]


class _NothingSegmenter:
    def feed(self, result):
        return []


def test_the_asr_latency_is_measured_from_the_unit_not_the_result():
    """A committed prefix is confirmed through the OLDER hypothesis.

    Its content is seconds older than the result that triggered it. Reading
    result.t_end reports a six-second-old clause as one second old, in the
    exact column the spec names as this change's evidence.
    """
    records = []
    unit = Unit(direction=Direction.IN, text="привет", t_start=10.0, t_end=14.0)
    pipeline = _pipeline(
        segmenter=_OneUnitSegmenter(unit),
        clock=FakeClock(20.0),
        on_record=records.append,
    )
    pipeline.handle(_interim("прив"))
    assert records[0].latency.asr_ms == 6000.0


def test_a_continuing_clause_holds_the_duck_across_the_gap():
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)
    unit = Unit(
        direction=Direction.IN, text="привет", t_start=0.0, t_end=1.0, continues=True
    )
    pipeline = _pipeline(segmenter=_OneUnitSegmenter(unit), playout=playout)

    pipeline.handle(_interim("прив"))
    for _ in range(3):  # FakeSynthesizer yields 60 ms for this text
        playout.tick()
    for _ in range(5):
        assert playout.tick() is False
    assert duck.is_open is False


def test_a_failed_clause_does_not_arm_the_duck_hold():
    """Otherwise a direction whose translator is down re-arms every five
    seconds and holds the duck shut for the whole call, with silence behind
    it - the stuck-closed failure, reached by a different road.
    """
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)
    unit = Unit(
        direction=Direction.IN, text="привет", t_start=0.0, t_end=1.0, continues=True
    )
    pipeline = _pipeline(
        segmenter=_OneUnitSegmenter(unit),
        synthesizer=FakeSynthesizer(error=RuntimeError("boom")),
        playout=playout,
    )

    pipeline.handle(_interim("прив"))
    playout.tick()
    assert duck.is_open is True


def test_a_final_ends_the_duck_hold_even_when_it_commits_nothing():
    """The last interim often already covered everything the final carries.

    Without clearing here, the hold would sit until the starvation bound
    instead of ending with the sentence.
    """
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)
    playout.expect_continuation(True)
    pipeline = _pipeline(segmenter=_NothingSegmenter(), playout=playout)

    pipeline.handle(_final())
    playout.tick()
    assert duck.is_open is True
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_pipeline.py -q -k "asr_latency or duck_hold or holds_the_duck"`
Expected: FAIL — `test_the_asr_latency_is_measured_from_the_unit_not_the_result`
gets `19000.0` (measured from the result), and the hold tests find the duck in
the wrong state.

- [ ] **Step 3: Implement**

In `sidetap/pipeline.py`, `handle()`, replace

```python
        arrived = self._clock.monotonic() - self._session_t0
        # Rounded to a tenth of a ms: these are wall-clock subtractions, so
        # raw floats carry binary rounding noise (10.2 - 10.0 == 0.19999...
        # not 0.2) that a millisecond-scale metric has no business showing.
        asr_ms = round(max(0.0, (arrived - result.t_end) * 1000), 1)

        for unit in self._segmenter.feed(result):
            if self._dead_air is not None:
                self._dead_air.heard_speech()
            self._speak(unit, asr_ms)
```

with

```python
        arrived = self._clock.monotonic() - self._session_t0

        for unit in self._segmenter.feed(result):
            if self._dead_air is not None:
                self._dead_air.heard_speech()
            # Per unit, not per result. A committed prefix is confirmed only
            # through the OLDER hypothesis's audio position, so its content is
            # seconds older than the result that triggered it - reading
            # result.t_end here reports a clause as ~1 s old when it is ~6 s
            # old, and the transcript's latency column is the stated evidence
            # for the whole commit-early decision. Identical under
            # FinalsOnlySegmenter, where unit.t_end IS result.t_end.
            #
            # Rounded to a tenth of a ms: these are wall-clock subtractions,
            # so raw floats carry binary rounding noise (10.2 - 10.0 ==
            # 0.19999... not 0.2) that a millisecond-scale metric has no
            # business showing.
            asr_ms = round(max(0.0, (arrived - unit.t_end) * 1000), 1)
            self._speak(unit, asr_ms)

        if result.is_final:
            # Ends the duck hold with the sentence, including when the final
            # committed nothing new because the last interim already covered
            # it. Without this the hold would sit until the starvation bound.
            self._playout.expect_continuation(False)
```

In `_speak`, immediately after the

```python
        if first_ms is None:
            # Nothing playout accepted, so nothing was heard: no latency, no
            # record, and dead-air stays armed.
            return
```

guard, insert:

```python
        if unit.continues:
            # Armed only here, on the path where playout actually accepted
            # audio. Arming it on a failure path would let a direction whose
            # translator is down re-arm every five seconds and hold the duck
            # shut for the whole call with silence behind it.
            self._playout.expect_continuation(True)
```

- [ ] **Step 4: Run the pipeline suite**

Run: `uv run pytest tests/test_pipeline.py tests/test_transcript.py -q`
Expected: PASS. Every pre-existing latency assertion must still hold —
`FinalsOnlySegmenter` sets `unit.t_end` from `result.t_end`, so the number
does not move on the old path.

- [ ] **Step 5: Commit**

```bash
git add sidetap/pipeline.py tests/test_pipeline.py
git commit -F - <<'EOF'
Measure ASR latency per unit, and arm the duck hold

A committed prefix is confirmed through the older hypothesis, so its
content is seconds older than the result that produced it. Reading the
result would understate it in the one column this decision is judged by.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 9: The flag, and choosing the segmenter

**Files:**
- Modify: `sidetap/cli.py:184-201` (the "output" group)
- Modify: `sidetap/run.py:20` (import) and `sidetap/run.py:278` (construction)
- Test: `tests/test_cli.py`, `tests/test_run.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_early_commit_is_on_by_default():
    args = build_parser().parse_args(_RUN)
    assert args.no_early_commit is False


def test_early_commit_can_be_turned_off():
    args = build_parser().parse_args(_RUN + ["--no-early-commit"])
    assert args.no_early_commit is True
```

Append to `tests/test_run.py`. These use the file's existing
`_session(tmp_path, routing_graph, **kwargs)` helper (`tests/test_run.py:140`),
whose keyword arguments are forwarded into `_args`, and the `routing_graph`
fixture the surrounding Session tests already take:

```python
def test_the_default_segmenter_commits_early(tmp_path, routing_graph):
    from sidetap.segment import LocalAgreementSegmenter

    session = _session(tmp_path, routing_graph)
    session.setup()
    try:
        for direction in Direction:
            assert isinstance(
                session.pipelines[direction]._segmenter, LocalAgreementSegmenter
            )
    finally:
        session.shutdown()


def test_no_early_commit_selects_the_finals_only_segmenter(tmp_path, routing_graph):
    from sidetap.segment import FinalsOnlySegmenter

    session = _session(tmp_path, routing_graph, no_early_commit=True)
    session.setup()
    try:
        for direction in Direction:
            assert isinstance(
                session.pipelines[direction]._segmenter, FinalsOnlySegmenter
            )
    finally:
        session.shutdown()


def test_the_two_directions_get_their_own_segmenter(tmp_path, routing_graph):
    """LocalAgreementSegmenter holds per-utterance state.

    One instance shared between directions would interleave two conversations
    and commit a prefix of neither - which is why segment.py tells you not to
    hoist it out of the loop.
    """
    session = _session(tmp_path, routing_graph)
    session.setup()
    try:
        assert (
            session.pipelines[Direction.IN]._segmenter
            is not session.pipelines[Direction.OUT]._segmenter
        )
    finally:
        session.shutdown()
```

Also add `no_early_commit=False` to the `base` dict in `tests/test_run.py`'s
`_args` helper (`tests/test_run.py:21-31`), beside `voice_out_gender=None`.

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_cli.py tests/test_run.py -q -k "early_commit or segmenter"`
Expected: FAIL — `AttributeError: 'Namespace' object has no attribute 'no_early_commit'`

- [ ] **Step 3: Implement**

In `sidetap/cli.py`, in the `out` group after the `--lag-cap` argument:

```python
    out.add_argument(
        "--no-early-commit",
        action="store_true",
        help="wait for a complete utterance before translating, instead of "
        "committing a stable prefix part-way through. Early committing only "
        "engages after roughly 11 s of continuous speech, so this changes "
        "nothing for ordinary conversation.",
    )
```

In `sidetap/run.py:20`, widen the import:

```python
from .segment import FinalsOnlySegmenter, LocalAgreementSegmenter
```

and in the `for direction, config in configs.items():` loop, replace
`segmenter=FinalsOnlySegmenter(),` with:

```python
                # Constructed inside the loop, one per direction.
                # LocalAgreementSegmenter holds per-utterance state, so a
                # single instance fed by both directions would interleave two
                # conversations and commit a prefix of neither. segment.py
                # says so too; this is the line it is talking about.
                segmenter=(
                    FinalsOnlySegmenter()
                    if getattr(args, "no_early_commit", False)
                    else LocalAgreementSegmenter()
                ),
```

The `getattr` shim mirrors `_rate` and `_gender` (`run.py:104-109`), so a
hand-built `argparse.Namespace` without the attribute still works.

- [ ] **Step 4: Run them and watch them pass**

Run: `uv run pytest tests/test_cli.py tests/test_run.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sidetap/cli.py sidetap/run.py tests/test_cli.py tests/test_run.py
git commit -F - <<'EOF'
Commit stable prefixes early by default, with --no-early-commit to stop

On by default because the feature does nothing below ~11 s of continuous
speech: ordinary conversation is unchanged, and the flag exists so the two
can be compared on a real call.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 10: Documentation

Four documents make claims this change falsifies. Leaving any of them is how
the next reader ends up trusting a number that moved.

**Files:**
- Modify: `sidetap/segment.py` (module docstring)
- Modify: `README.md:291-300`
- Modify: `CLAUDE.md:27`, `CLAUDE.md:66`, `CLAUDE.md:198`, the module table, and
  the **Non-obvious mechanics** list
- Modify: `docs/manual-smoke.md`

- [ ] **Step 1: Rewrite `segment.py`'s module docstring**

The existing one says LocalAgreement-2 "buys roughly 0.5-1 s" and that "the
spec's decision was to measure first". Both are now wrong. Replace the whole
docstring with:

```python
"""The seam between recognition and translation.

A Segmenter turns AsrResults into Units - the things worth paying to translate
and speak. Two ship: FinalsOnlySegmenter waits for a complete utterance,
LocalAgreementSegmenter commits a stable prefix part-way through.

What LocalAgreement-2 buys here is NOT the "roughly 0.5-1 s on a normal
sentence" the research describes, and an earlier version of this docstring
claimed. Chirp emits no interim results at all for short turn-taking
utterances (docs/experiments/06-interim-cadence.md), so there is nothing for a
prefix comparison to work on and ordinary conversation is untouched. What it
buys is the monologue: 34.8 s of continuous speech measured as one final after
29.3 s of silence, which this turns into a commit roughly every 5 s.

It is self-limiting with no constant to tune. Interims arrive once per 5 s of
sent audio, so two agreeing hypotheses need ~11 s of continuous speech; below
that both classes behave identically. Do not add a length threshold - it would
be a second, worse copy of a bound the API already imposes.
"""
```

- [ ] **Step 2: Fix the README**

`README.md:296-300` currently ends the "Felt latency" bullet with:

> `segment.py`'s `FinalsOnlySegmenter` is the placeholder seam for
> LocalAgreement-2, which would cut the wait itself; that seam exists and is
> unused.

Replace those three lines with:

```markdown
  `segment.py`'s `LocalAgreementSegmenter` now cuts that wait, but only where
  Chirp makes it possible: short utterances produce no interim results at all,
  so turn-taking is unchanged and the gain is confined to monologues, where a
  measured 29.3 s wait becomes a commit roughly every 5 s
  (`docs/experiments/06-interim-cadence.md`). `--no-early-commit` restores the
  old behaviour.
```

- [ ] **Step 3: Fix CLAUDE.md**

Three test counts at `:27`, `:66` and `:198` — set all three to whatever
`uv run pytest -q` actually reports, not to a guess.

In the module table, `segment.py`'s row currently reads
`the `Segmenter` seam; `FinalsOnlySegmenter` for v1`. Replace with:

```markdown
| `segment.py` | the `Segmenter` seam; finals-only, or LocalAgreement-2 prefixes |
```

Add to **Non-obvious mechanics worth knowing before you touch these**:

```markdown
- **Committing early is bounded by Chirp, not by a constant.** Interims arrive
  once per 5 s of sent audio and short utterances produce none at all
  (`docs/experiments/06-interim-cadence.md`), so `LocalAgreementSegmenter`
  needs ~11 s of continuous speech before two hypotheses can agree and is
  inert below that. There is deliberately no length threshold in the code: a
  constant here would be a second, worse copy of a bound the API already
  imposes, and it would drift the moment Google changes the cadence.
- **The comparison key is lowercased and stripped of punctuation.** Both were
  measured changing between two interims of one utterance - `Что` to `что`,
  `сверхурочно.` to `сверхурочно,`. Compare surfaces instead and the longest
  common prefix on the real capture is **zero characters**, so nothing commits
  early and the feature silently does nothing at all. Punctuation still picks
  the cut point; it just never decides agreement.
- **`Playout.expect_continuation` is the only thing keeping the duck shut
  between committed clauses.** `tick()` opens the duck whenever the queue
  drains and nothing is starved, and "starved" means an utterance already
  part-way through - which the gap between two clauses is not. Without the
  hold, IN leaks a burst of the untranslated original into that gap roughly
  every 5 s of a monologue. It shares `STARVE_LIMIT_TICKS` with the two
  starvation cases so it always fails open, and `pipeline._speak` arms it only
  after playout has accepted audio: arming on a failure path would let a
  direction whose translator is down re-arm every 5 s and hold the duck shut
  for the rest of the call.
```

- [ ] **Step 4: Add the manual-smoke checks**

The automated suite cannot hear anything. Add a new section to
`docs/manual-smoke.md`, after `## Playout buffering`:

```markdown
## Committing early (`LocalAgreementSegmenter`)

Everything below needs someone willing to talk for half a minute without
pausing. Nothing in the test suite can reach any of it.

- [ ] **A monologue starts being spoken part-way through.** Have the other
      party talk continuously for 30 s. You should start hearing the
      translation after roughly 6-7 s and keep hearing it in pieces, not
      hear nothing for half a minute and then a wall of speech. Compare
      against the same thing under `--no-early-commit`.
- [ ] **The duck does not flap between clauses.** This is the check that
      decides whether the feature ships on by default. Listen for the
      original bleeding through *inside* the monologue, in the gap between
      one committed clause and the next. It should not be audible at all.
      If it is, `Playout.expect_continuation` is not being armed.
- [ ] **Ordinary conversation is unchanged.** Normal turn-taking produces no
      interim results, so nothing should sound or read differently from
      before. If short sentences start arriving in fragments, the segmenter
      is committing on one hypothesis instead of two.
- [ ] **The transcript reads as clauses, not as a jumble.** A monologue
      should render as several rows whose text joins back into the whole
      utterance, with no word repeated and none missing. Word order across a
      clause boundary is the quality cost the spec named, and this is the
      only place it can be judged.
- [ ] **The end of a monologue releases the duck promptly.** When the speaker
      stops, the original should become audible again within a second or so -
      not after the 2 s starvation bound, which would mean the final never
      cleared the hold.
```

- [ ] **Step 5: Commit**

```bash
git add sidetap/segment.py README.md CLAUDE.md docs/manual-smoke.md
git commit -F - <<'EOF'
Correct the docs that this change falsifies

segment.py and the README both quoted "0.5-1 s on a normal sentence" for
LocalAgreement-2. Chirp emits no interims for short utterances at all, so
that describes a case it cannot serve; the gain is the monologue.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 11: Prove the tests can fail

Reading cannot establish that a test is load-bearing; only breaking the code
can. Four mutations, each with the test that must catch it. A mutation that
survives means the test is decorative and has to be fixed before this is done.

**Files:** none permanently — every edit here is reverted.

- [ ] **Step 1: Full suite, green baseline**

Run: `uv run pytest -q`
Expected: PASS. Record the count; it goes into CLAUDE.md in Task 10 if it was
not already correct.

- [ ] **Step 2: Mutation 1 — the comparison key stops folding case**

In `sidetap/segment.py`, change `_tokens` from `surface.lower()` to `surface`.

Run: `uv run pytest tests/test_segment.py -q`
Expected: FAIL — `test_the_key_ignores_case_and_punctuation` and
`test_the_real_capture_survives_the_case_change`. **If the second one passes,
the fixture replay is not actually exercising the case change and must be
fixed.** Revert with `git checkout sidetap/segment.py`.

- [ ] **Step 3: Mutation 2 — one agreement instead of two**

In `_interim`, replace

```python
        agreed = _agreed(previous, current)
```

with

```python
        agreed = len(current)
```

which commits the newest hypothesis outright.

Run: `uv run pytest tests/test_segment.py -q`
Expected: FAIL — `test_the_real_capture_commits_nothing_the_final_contradicted`
and `test_one_interim_commits_nothing`. Revert with
`git checkout sidetap/segment.py`.

- [ ] **Step 4: Mutation 3 — the duck hold does nothing**

In `sidetap/playout.py`, make the body of `expect_continuation` `pass`.

Run: `uv run pytest tests/test_playout.py tests/test_pipeline.py -q`
Expected: FAIL — `test_the_duck_holds_across_a_gap_when_more_of_the_run_is_coming`,
`test_the_duck_holds_while_the_next_clause_is_still_buffering`, and
`test_a_continuing_clause_holds_the_duck_across_the_gap`. Revert with
`git checkout sidetap/playout.py`.

- [ ] **Step 5: Mutation 4 — the hold is unbounded**

In `_advance_locked`'s continuation branch, delete the
`if self._starved_ticks >= STARVE_LIMIT_TICKS:` block.

Run: `uv run pytest tests/test_playout.py -q`
Expected: FAIL — `test_a_continuation_that_never_arrives_still_opens_the_duck`.
This is the most important of the four: it is the only thing standing between
this feature and a duck stuck closed for the rest of a call. Revert with
`git checkout sidetap/playout.py`.

- [ ] **Step 6: Mutation 5 — latency read from the result again**

In `sidetap/pipeline.py`, change `arrived - unit.t_end` back to
`arrived - result.t_end`.

Run: `uv run pytest tests/test_pipeline.py -q`
Expected: FAIL —
`test_the_asr_latency_is_measured_from_the_unit_not_the_result`. Revert with
`git checkout sidetap/pipeline.py`.

- [ ] **Step 7: Confirm the tree is clean and green**

```bash
git status --short          # must be empty
uv run pytest -q            # must pass
```

- [ ] **Step 8: Secret-scan the whole branch before it goes anywhere**

The repository is public.

```bash
git diff main...HEAD | grep -niE "speechtotext|prod-|/home/|@|AIza|private_key" || echo clean
```

Expected: `clean`, or only the Co-Authored-By trailer's noreply address.

- [ ] **Step 9: Run the manual smoke checklist**

`docs/manual-smoke.md`, at minimum the new **Committing early** section and
the existing **The duck** section. This is the only place the feature can
actually be judged — everything above proves it does what it was written to
do, not that it sounds right.

Report honestly which boxes were ticked and which were not. It has never been
run; if it is not run now either, say so rather than implying otherwise.

---

## Notes for whoever executes this

**The one thing that is genuinely risky** is the duck hold. Everything else is
contained: the segmenter sits behind a protocol that already exists, and
`--no-early-commit` turns it off. The hold reaches into `_advance_locked`,
which is the function CLAUDE.md's most emphatic invariant is about. If a
choice comes up between holding the duck a little too long and releasing it a
little too early, release it — a duck stuck closed silences the person on the
other end of the call.

**Do not run `gcloud auth`.** The checkout is attached to a live GCP project
and re-authenticating has broken its ambient credentials before. Nothing in
this plan needs credentials: the fixture is committed and the whole suite runs
with no network.

**This project uses uv.** Never `pip install`, never activate `.venv` by hand.
