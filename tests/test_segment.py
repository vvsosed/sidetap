import json
import logging
import re
from pathlib import Path

import pytest

from sidetap.segment import (
    _SPOKEN_LIMIT,
    MIN_ANCHOR,
    FinalsOnlySegmenter,
    LocalAgreementSegmenter,
    _agreed,
    _overlap,
    _tokens,
)
from sidetap.types import AsrResult, Direction


def _result(text="hello", is_final=True, t_start=1.0, t_end=2.0, direction=Direction.IN):
    return AsrResult(
        direction=direction,
        text=text,
        is_final=is_final,
        t_start=t_start,
        t_end=t_end,
    )


# --- FinalsOnlySegmenter -----------------------------------------------------


def test_a_final_becomes_exactly_one_unit():
    units = FinalsOnlySegmenter().feed(_result())
    assert len(units) == 1
    assert units[0].text == "hello"
    assert units[0].direction is Direction.IN


def test_the_unit_keeps_the_results_span():
    units = FinalsOnlySegmenter().feed(_result(t_start=5.0, t_end=7.5))
    assert (units[0].t_start, units[0].t_end) == (5.0, 7.5)


def test_interims_produce_nothing():
    assert FinalsOnlySegmenter().feed(_result(is_final=False)) == []


def test_a_final_with_only_whitespace_produces_nothing():
    # result_from_response already drops these, but the segmenter is the last
    # gate before a paid translation call.
    assert FinalsOnlySegmenter().feed(_result(text="   ")) == []


def test_a_discarded_interim_does_not_leak_into_the_next_final():
    segmenter = FinalsOnlySegmenter()
    segmenter.feed(_result(is_final=False, text="partial"))
    units = segmenter.feed(_result(text="complete"))
    assert [u.text for u in units] == ["complete"]


def test_directions_do_not_share_state():
    """Stateless today, so this passes trivially — and that is the point.

    It fails loudly the day someone adds instance state without also making
    the per-direction ownership explicit.
    """
    segmenter = FinalsOnlySegmenter()
    segmenter.feed(_result(direction=Direction.IN, is_final=False, text="ru"))
    units = segmenter.feed(_result(direction=Direction.OUT, text="en"))
    assert [(u.direction, u.text) for u in units] == [(Direction.OUT, "en")]


# --- the comparison key: case, punctuation, word agreement -------------------


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


def test_two_different_numbers_do_not_agree():
    """Stripping punctuation throughout would key "1.2" and "12" the same.

    Numbers are the one place a wrong commit is unrecoverable in a way the
    listener notices: an amount or a time gets spoken, and nothing can take it
    back. Only edge punctuation is dropped, so interior separators survive.
    """
    assert _agreed(_tokens("about 1.2 million"), _tokens("about 12 million")) == 1


def test_agreement_counts_words_not_characters():
    a = _tokens("Что у нас есть")
    b = _tokens("что у нас было")
    assert _agreed(a, b) == 3


def test_a_word_extended_in_the_next_hypothesis_ends_the_agreement():
    """A truncated trailing word needs no special case: its key differs."""
    assert _agreed(_tokens("без поним"), _tokens("без понимания")) == 1


def test_agreement_stops_at_the_first_mismatch():
    """A later match must not resurrect an earlier disagreement.

    Without the break, a hypothesis that diverges and then re-converges would
    report agreement on words that were never agreed - and those words get
    committed, translated and spoken, where nothing can take them back.
    """
    assert _agreed(_tokens("а б в г"), _tokens("а X в г")) == 1


# --- LocalAgreementSegmenter: committing on interims -------------------------


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


def test_spans_are_contiguous_across_two_commits():
    """The second commit starts where the first one ended.

    Without that, every unit of a monologue would claim to start at the
    beginning of the session, and the transcript would show a pile of
    overlapping spans instead of a sequence.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    first = segmenter.feed(_interim("что у нас есть, несколько важных задач,", 10.0))[0]
    second = segmenter.feed(
        _interim("что у нас есть, несколько важных задач, которые нужно", 15.0)
    )[0]
    assert (first.t_start, first.t_end) == (0.0, 5.0)
    assert (second.t_start, second.t_end) == (5.0, 10.0)


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


def test_the_cut_takes_the_last_boundary_not_the_first():
    """Scanning backwards is the whole point of the loop.

    Stopping at the first boundary would hold back clauses that were already
    agreed, one commit at a time - the stall this feature exists to remove,
    reintroduced a comma at a time.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("да, конечно, мы", 5.0))
    units = segmenter.feed(_interim("да, конечно, мы согласны", 10.0))
    assert [u.text for u in units] == ["да, конечно,"]


# --- LocalAgreementSegmenter: finals, revisions and stream restarts ----------


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


def test_one_interim_after_a_final_commits_nothing():
    """A new utterance starts from nothing agreed, like any other.

    This does NOT prove the spoken list was cleared - one interim cannot,
    because the previous-hypothesis reset alone forces the same answer. Nor
    does test_a_new_utterance_is_not_offset_by_the_last_one, despite feeding
    two - its new utterance shares no words with the last one, so the
    alignment comes back zero and emits the opening whole either way. What
    actually pins the clear is
    test_a_final_clears_the_spoken_text_even_when_the_next_utterance_opens_on_it,
    where the two utterances meet on a shared word.
    """
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


def test_a_final_that_revises_inside_the_spoken_text_is_emitted_whole(caplog):
    """The price of aligning by content, pinned rather than preferred.

    "есть," was spoken and the final replaces it with "было", so no suffix of
    what was spoken opens the final and no prefix of the final was spoken: the
    alignment is zero and the whole final goes out, with "что у нас" heard a
    second time. The count-based version emitted only "несколько задач." here,
    silently dropping the revised word instead.

    This is the one shape where the old rule did better, which is why the
    alignment reports it at warning. It is the mirror of the shape that
    actually happened on a real call - a hypothesis re-windowed onto a LATER
    part of the same utterance - where the count-based rule repeated 50 to 110
    words at a time and this one repeats nothing
    (test_a_final_after_a_re_windowed_interim_emits_only_the_new_tail).
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    segmenter.feed(_interim("что у нас есть, несколько задач", 10.0))
    with caplog.at_level(logging.DEBUG):
        units = segmenter.feed(_final_result("что у нас было несколько задач.", 15.0))
    assert [u.text for u in units] == ["что у нас было несколько задач."]
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_a_new_utterance_is_not_offset_by_the_last_one():
    """Two interims after the final, not one - one interim proves nothing.

    This is not the clear-in-_finalise regression test it looks like. The new
    utterance ("совсем другое...") shares no word with the text already spoken
    ("что у нас есть,..."), so the alignment comes back zero and emits the
    opening whole whether or not _finalise cleared anything - confirmed by
    mutation. What this test actually pins is narrower: that an unrelated new
    utterance is not offset, and one interim would hide nothing because the
    first interim of any utterance commits nothing anyway. See
    test_a_final_clears_the_spoken_text_even_when_the_next_utterance_opens_on_it
    for the case the alignment cannot cover, where the clear is load-bearing.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    segmenter.feed(_interim("что у нас есть, несколько задач", 10.0))
    segmenter.feed(_final_result("что у нас есть, несколько задач.", 15.0))

    segmenter.feed(_interim("совсем другое, но нужно", 20.0))
    units = segmenter.feed(_interim("совсем другое, но нужно сказать", 25.0))
    assert [u.text for u in units] == ["совсем другое,"]


def test_a_final_clears_the_spoken_text_even_when_the_next_utterance_opens_on_it():
    """`self._spoken = []` in _finalise, pinned where nothing covers for it.

    _spoken is per-utterance, and the word that ends one utterance is a
    perfectly ordinary word to open the next with - "всё" here. Carried over,
    that one word aligns, so the next utterance's final emits "было готово
    вчера." and the listener never hears its first word; worse, a non-zero
    alignment chains the span, dating a sentence spoken at 20 s to 15 s, and
    render_markdown sorts on t_start.

    Both halves are asserted for that reason. The text alone would also pass
    with the reset moved into the alignment instead of the state.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("у нас всё,", 5.0))
    segmenter.feed(_interim("у нас всё, ладно", 10.0))
    segmenter.feed(_final_result("у нас всё, ладно.", 15.0))

    units = segmenter.feed(_final_result("всё было готово вчера.", 20.0))
    assert [u.text for u in units] == ["всё было готово вчера."]
    assert (units[0].t_start, units[0].t_end) == (20.0, 20.0)


def test_a_final_that_emits_nothing_still_advances_the_span():
    """Otherwise the next utterance claims to start before this final.

    Spans are what the transcript's latency column is built from, and one that
    reaches back across a finished utterance reads as a clause that took the
    whole gap to arrive.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_final_result("   ", 5.0))
    segmenter.feed(_interim("совсем другое,", 10.0))
    unit = segmenter.feed(_interim("совсем другое, но нужно", 15.0))[0]
    assert unit.t_start == 5.0


def test_a_final_that_drops_spoken_text_still_says_so(caplog):
    """The worst disagreement is the one with nothing left to emit.

    A final carrying LESS than what was spoken aligns whole - every word of it
    was already heard - so there is no remainder and the emit path never runs.
    The log is what stops the loudest available sign that committing early
    went wrong from passing in complete silence.

    Debug rather than warning, because nothing reaches the listener twice:
    what this costs is the revision, which cannot be honoured anyway.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    segmenter.feed(_interim("что у нас есть, несколько задач", 10.0))
    with caplog.at_level(logging.DEBUG):
        units = segmenter.feed(_final_result("что у нас.", 15.0))
    assert units == []
    assert "revised" in caplog.text


def test_a_final_sharing_nothing_with_the_spoken_text_is_emitted_whole(caplog):
    """The stream restarted mid-utterance, and nothing told the segmenter.

    RecognitionWorker rebuilds its stream every MAX_STREAM_SECONDS and after
    any non-fatal error, and neither path guarantees a final - so text spoken
    before the break is still in _spoken when the first final of the NEXT
    utterance arrives. Slicing that final by a count cuts words off the front
    of a sentence nobody has heard: six spoken words turned "completely new
    sentence here." into nothing at all. Zero alignment emits it whole, which
    is why the two discard rules this replaced are gone rather than ported.

    Warning, not debug: a zero alignment is the one outcome that can still put
    a repeat in the listener's ear, and nothing in an AsrResult distinguishes
    a restart (harmless here) from a wholesale revision (not).
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("one two three four five six,", 5.0))
    segmenter.feed(_interim("one two three four five six, seven eight", 10.0))
    with caplog.at_level(logging.DEBUG):
        units = segmenter.feed(_final_result("completely new sentence here.", 240.0))
    assert [u.text for u in units] == ["completely new sentence here."]
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_spoken_text_does_not_shorten_a_final_it_shares_nothing_with():
    """The same break, with a final long enough that slicing would not be
    visible as a total loss - it would just quietly eat the first six words.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("one two three four five six,", 5.0))
    segmenter.feed(_interim("one two three four five six, seven eight", 10.0))
    units = segmenter.feed(
        _final_result("alpha beta gamma delta epsilon zeta eta theta iota.", 240.0)
    )
    assert [u.text for u in units] == [
        "alpha beta gamma delta epsilon zeta eta theta iota."
    ]


def test_a_zero_alignment_leaves_the_final_carrying_a_whole_utterances_span():
    """Nothing already spoken describes this utterance, its span least of all
    - chaining from it would date the sentence to before the break.

    This is the other half of requirement "chaining only within an utterance
    actually committed in pieces": an alignment of zero means this final
    continues nothing, so it carries result.t_start exactly as
    FinalsOnlySegmenter would have given it.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("one two three four five six,", 5.0))
    segmenter.feed(_interim("one two three four five six, seven eight", 10.0))
    unit = segmenter.feed(_final_result("completely new sentence here.", 240.0))[0]
    assert (unit.t_start, unit.t_end) == (240.0, 240.0)


def test_a_revision_past_the_spoken_text_emits_only_the_new_tail(caplog):
    """The revision Experiment 6 actually measured, which must stay silent.

    There the final's first 51 words matched what had been committed exactly
    and the inserted word landed past the commit point - "а" here. Everything
    spoken still aligns, so the final emits only its new tail and logs
    nothing at all: a signal that fires when nothing went wrong is not a
    signal, and this is the common case on a monologue.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    segmenter.feed(_interim("что у нас есть, несколько задач", 10.0))
    with caplog.at_level(logging.DEBUG):
        units = segmenter.feed(
            _final_result("что у нас есть, несколько а задач.", 15.0)
        )
    assert [u.text for u in units] == ["несколько а задач."]
    assert caplog.records == []


def test_an_interim_agreeing_only_on_spoken_text_emits_nothing(caplog):
    """A hypothesis that SHRANK, which the count rule got right by accident.

    A word inserted inside the spoken prefix drops the agreement between the
    two live hypotheses back to "a b" - text the listener already heard in
    full. By content that is nothing new, so nothing is emitted. The count
    version guarded this with `agreed <= len(committed)`; without the
    containment arm of `_overlap` the suffix search alone calls "a b"
    genuinely new, because it is not a SUFFIX of what was spoken, and speaks
    it a second time.

    Logged at debug, not warning: the alignment found the seam, so nothing
    reached the listener twice.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("a b c d,", 5.0))
    segmenter.feed(_interim("a b c d, e", 10.0))
    with caplog.at_level(logging.DEBUG):
        assert segmenter.feed(_interim("a b X c d, e f", 15.0)) == []
    assert [r.levelno for r in caplog.records] == [logging.DEBUG]
    assert "revised" in caplog.text


def test_a_restart_does_not_eat_the_next_utterances_early_commits(caplog):
    """The interim half of the stream-restart defect.

    Same break as the final-side tests, but the new utterance is long enough
    to commit early on its own. Slicing by the six words already spoken turned
    "alpha beta gamma, delta epsilon" into "eta, theta" - the opening words
    of a sentence nobody has heard, gone, on the default path.

    The log moved one round later than it used to sit. The first interim after
    the break agrees with its predecessor on nothing, so there is no candidate
    to align and nothing to report; the warning lands on the round that
    actually emits, where it describes text the listener is about to hear.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("one two three four five six,", 5.0))
    segmenter.feed(_interim("one two three four five six, seven eight", 10.0))
    with caplog.at_level(logging.DEBUG):
        # The stream restarted. A new utterance, sharing nothing.
        assert segmenter.feed(_interim("alpha beta gamma, delta", 240.0)) == []
    assert caplog.records == []

    with caplog.at_level(logging.DEBUG):
        units = segmenter.feed(_interim("alpha beta gamma, delta epsilon zeta", 245.0))
    assert [u.text for u in units] == ["alpha beta gamma,"]
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_a_zero_alignment_does_not_chain_the_next_span_across_the_break():
    """The watermark is as stale as the text it belongs to.

    Left alone it dates the new utterance's first clause to before the
    restart, and render_markdown sorts on t_start - so the bilingual
    transcript puts a sentence spoken after the break among the rows from
    before it.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("one two three four five six,", 5.0))
    segmenter.feed(_interim("one two three four five six, seven eight", 10.0))
    segmenter.feed(_interim("alpha beta gamma, delta", 240.0))
    unit = segmenter.feed(_interim("alpha beta gamma, delta epsilon zeta", 245.0))[0]
    assert (unit.t_start, unit.t_end) == (240.0, 240.0)


def test_the_final_after_a_restarted_interim_commit_is_not_sliced_twice():
    """The two halves must not compound.

    Once the interim side has committed the new utterance's opening, the final
    aligns on THAT - "alpha beta gamma," sitting at the end of _spoken, three
    words behind the stale six - and emits only what is new. Nothing is lost
    and nothing is repeated, without either path needing to know a restart
    happened.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("one two three four five six,", 5.0))
    segmenter.feed(_interim("one two three four five six, seven eight", 10.0))
    segmenter.feed(_interim("alpha beta gamma, delta", 240.0))
    segmenter.feed(_interim("alpha beta gamma, delta epsilon zeta", 245.0))
    units = segmenter.feed(
        _final_result("alpha beta gamma, delta epsilon zeta eta.", 250.0)
    )
    assert [u.text for u in units] == ["delta epsilon zeta eta."]


def test_an_ordinary_interim_logs_no_revision(caplog):
    """A signal that fires on every interim is not a signal."""
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    with caplog.at_level(logging.DEBUG):
        segmenter.feed(_interim("что у нас есть, несколько задач", 10.0))
        segmenter.feed(_interim("что у нас есть, несколько задач, которые", 15.0))
    assert "interim revised" not in caplog.text


def test_an_ordinary_final_logs_no_revision(caplog):
    """A signal that fires on every final is not a signal.

    The revision log exists to be the loudest thing in this module when
    committing early goes wrong. If it also fires when nothing went wrong,
    nobody reading the log will look at it twice.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    segmenter.feed(_interim("что у нас есть, несколько задач", 10.0))
    with caplog.at_level(logging.DEBUG):
        segmenter.feed(_final_result("что у нас есть, несколько задач.", 15.0))
    assert "revised" not in caplog.text


# --- LocalAgreementSegmenter: aligning against what was actually spoken ------


def test_a_final_after_a_re_windowed_interim_emits_only_the_new_tail():
    """The shape of the regression the first real call produced.

    Chirp re-windows its hypothesis mid-utterance: an interim arrives that
    shares no LEADING word with the text already spoken, because it starts
    part-way into the same sentence. The count-based version read that as a
    dead stream, threw the commit away, and the final of that same utterance
    then found nothing committed and re-spoke the lot. On a five-minute call
    against real lecture audio that was roughly 15% of everything the listener
    heard, in three bursts of 50 to 110 words, with the session log showing
    "discarding 69 / 55 / 14 / 83 committed word(s)".

    Aligning by content instead, the two largest events measured out of those
    transcripts come to:

        spoken 69 words, final 98 words -> overlap 69, emits 29 new
        spoken 66 words, final 80 words -> overlap 55, emits 25 new

    - the overlaps matching the discarded-word counts in the log exactly. The
    wording below is this test's own; the transcripts are someone else's
    content and are not in this repository.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("first clause here,", 5.0))
    segmenter.feed(_interim("first clause here, second clause here,", 10.0))
    segmenter.feed(_interim("first clause here, second clause here, third clause", 15.0))
    # The re-window: same utterance, but reported from its second clause on.
    assert segmenter.feed(_interim("second clause here, third clause follows", 20.0)) == []

    units = segmenter.feed(
        _final_result(
            "first clause here, second clause here, third clause follows.", 25.0
        )
    )
    assert [u.text for u in units] == ["third clause follows."]
    assert (units[0].t_start, units[0].t_end) == (10.0, 25.0)


def test_a_final_overlapping_only_the_tail_of_the_spoken_text_emits_the_rest():
    """The windowed case: the candidate starts inside what was spoken.

    Six words have been spoken, and the final opens on the LAST three of them
    rather than the first - there is no common prefix at all, which is what a
    leading-word comparison would have looked for. The overlap is a suffix of
    one against a prefix of the other, so the seam is found three words in and
    only what is past it is emitted.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("first clause here,", 5.0))
    segmenter.feed(_interim("first clause here, second clause here,", 10.0))
    segmenter.feed(_interim("first clause here, second clause here, third clause", 15.0))

    units = segmenter.feed(
        _final_result("second clause here, third clause follows.", 25.0)
    )
    assert [u.text for u in units] == ["third clause follows."]


def test_a_zero_alignment_emits_a_new_utterances_agreed_prefix_whole(caplog):
    """A genuinely new utterance, which is what the discards used to handle.

    Nothing of "alpha beta gamma" survives into the new utterance, so the
    alignment is zero and all six agreed words are emitted. A count-based
    slice would keep only the last three and drop "one two three" - the
    opening of a sentence nobody has heard - which is the silent loss the
    discard rules existed to prevent. One rule now covers both that case and
    the re-window above.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("alpha beta gamma", 5.0))
    segmenter.feed(_interim("alpha beta gamma delta", 10.0))

    segmenter.feed(_interim("one two three four five six", 240.0))
    with caplog.at_level(logging.DEBUG):
        units = segmenter.feed(_interim("one two three four five six seven", 245.0))
    assert [u.text for u in units] == ["one two three four five six"]
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_a_repeated_phrase_anchors_on_its_last_occurrence():
    """"and then, and then" - the anchor ambiguity ordinary speech creates.

    Two overlaps fit here: the whole spoken text against the final's first
    four words, and its trailing "and then" against the final's first two.
    Maximal takes the longer one and emits "he left."; the shorter anchor
    emits "and then he left." and speaks the phrase a third time.

    That is the direction to fail in. A wrong anchor that is too LONG drops
    words the listener never knows were missing; one that is too short speaks
    words they have already heard, and a synthesised voice cannot take a word
    back. The whole regression this alignment replaced was of the second kind.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("and then and then", 5.0))
    segmenter.feed(_interim("and then and then he", 10.0))

    units = segmenter.feed(_final_result("and then and then he left.", 15.0))
    assert [u.text for u in units] == ["he left."]


def test_the_spoken_window_is_capped_and_keeps_the_newest_words():
    """The cap bounds the quadratic search; the direction bounds the damage.

    An utterance that never finalises - a stream restarting under a monologue
    - would otherwise grow _spoken without limit, and the overlap search is
    quadratic in its length. Trimming the FRONT is what makes the cap safe:
    the alignment anchors on the END of what was spoken, so the oldest words
    are the only ones it can afford to lose. Trim the other end and the very
    words the next hypothesis is about to be matched against are the ones
    thrown away.
    """
    segmenter = LocalAgreementSegmenter()
    words = " ".join(f"w{i}" for i in range(1000))
    segmenter.feed(_interim(words, 5.0))
    units = segmenter.feed(_interim(words + " tail", 10.0))

    assert len(units[0].text.split()) == 1000
    assert len(segmenter._spoken) == _SPOKEN_LIMIT
    assert [key for _, key in segmenter._spoken[:1]] == ["w600"]
    assert [key for _, key in segmenter._spoken[-1:]] == ["w999"]


# --- LocalAgreementSegmenter: a re-window reaching back past what was spoken -

# The wording in this section is its own. The real call these shapes come from
# was someone else's lecture, and that transcript is not in this repository.

_RIVER = "the river rises every spring, so the farmers move their herds uphill,"


def _speak_the_river(segmenter):
    """Commit `_RIVER`'s twelve words in two pieces, spans (0, 5) and (5, 10).

    Ending on a boundary keeps the cut from holding any of it back, so what is
    in `_spoken` afterwards is exactly those twelve words.
    """
    segmenter.feed(_interim("the river rises every spring, so the farmers move", 5.0))
    first = segmenter.feed(_interim(_RIVER, 10.0))
    second = segmenter.feed(_interim(_RIVER + " before the", 15.0))
    assert [u.text for u in first + second] == [
        "the river rises every spring,",
        "so the farmers move their herds uphill,",
    ]


def test_a_final_that_prepends_words_before_the_spoken_run_emits_only_the_new_tail(
    caplog,
):
    """The event behind almost all the repeats left after content alignment.

    Chirp's final opened with words from BEFORE the utterance's first commit
    and then carried everything already spoken. It neither starts with a tail
    of what was spoken nor fits inside it, so the first two arms of `_overlap`
    both return 0 and, before the anchored arm, the whole final went out
    again. On the real call: 64 words spoken in four pieces, 3 prepended, a
    101-word final, and 34 of those words new - `_overlap` returned 0 and all
    101 were spoken, 64 of them a second time. The anchored arm returns 67
    there and emits the 34. Scaled down here: 12 spoken, 3 prepended, a
    22-word final, 7 new.

    The warning is asserted with its numbers because it is what the next real
    call will be judged by: the dropped count and the run length are the only
    way to tell a genuine re-window from a false anchor after the fact.
    """
    segmenter = LocalAgreementSegmenter()
    _speak_the_river(segmenter)

    with caplog.at_level(logging.DEBUG):
        units = segmenter.feed(
            _final_result(
                "as noted earlier. The river rises every spring, so the farmers "
                "move their herds uphill, before the water reaches the lower "
                "fields.",
                20.0,
            )
        )
    assert [u.text for u in units] == ["before the water reaches the lower fields."]
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "dropped 3 leading word(s) in front of a run of 12" in caplog.text


def test_an_anchored_final_chains_its_span_from_the_pieces_before_it():
    """Chaining is right here, not a leftover of the stream-restart case.

    What the final emits FOLLOWS the run already spoken, so it continues the
    pieces before it exactly as a final aligned on its first word would, and
    its span starts where the last piece ended. Under the zero alignment the
    real call's final went out whole with a zero-length span at its own end;
    anchored, its 34 new words chain from the last piece, nine seconds
    earlier, which is when they began to be spoken.
    """
    segmenter = LocalAgreementSegmenter()
    _speak_the_river(segmenter)

    unit = segmenter.feed(
        _final_result(
            "as noted earlier. "
            + _RIVER
            + " before the water reaches the lower fields.",
            20.0,
        )
    )[0]
    assert (unit.t_start, unit.t_end) == (10.0, 20.0)
    assert unit.continues is False


def test_an_anchored_interim_chains_its_span_and_the_final_after_it_does_too(caplog):
    """The interim half: only a ZERO alignment resets the span watermark.

    The anchored arm's k (15 here) counts the prepended words as well as the
    twelve spoken, so it is larger than `_spoken` - nothing may read that as
    anything but "skip this many". The interim emits only the growth past the
    run, chained from the last piece's end; the final after it re-anchors on
    the fifteen words now spoken and emits only its own tail.

    The dropped words never join `_spoken`, so the same re-window logs again
    at the final, with the same dropped count and a longer run. That is how
    one event reads in the log - the count repeating, not the line count.
    """
    prepended = "as noted earlier. " + _RIVER
    segmenter = LocalAgreementSegmenter()
    _speak_the_river(segmenter)

    # The hypothesis re-windows back past the first commit and stays there, so
    # it takes two interims before the re-windowed text agrees with itself.
    assert segmenter.feed(_interim(prepended + " before the water", 20.0)) == []
    interim = segmenter.feed(_interim(prepended + " before the water reaches", 25.0))
    assert [(u.text, u.t_start, u.t_end) for u in interim] == [
        ("before the water", 10.0, 20.0)
    ]

    with caplog.at_level(logging.DEBUG):
        final = segmenter.feed(
            _final_result(
                prepended + " before the water reaches the lower fields.", 30.0
            )
        )
    assert [(u.text, u.t_start, u.t_end) for u in final] == [
        ("reaches the lower fields.", 20.0, 30.0)
    ]
    assert "dropped 3 leading word(s) in front of a run of 15" in caplog.text


def test_a_short_phrase_recurring_in_new_speech_does_not_anchor(caplog):
    """Coincidental phrasing must never cost the new words in front of it.

    On the real call the speaker reused a six-word sentence frame one sentence
    later with a different verb - the longest coincidental run measured
    anywhere on it. Here the frame sits at the END of what was spoken, which
    is the only place the anchored arm can see it, and the new sentence
    reuses it after a stream restart left the old text in `_spoken`. Anchored
    on those six words, the arm would emit only "argue about it." and silently
    drop the ten words before it, every one of them new speech; below
    MIN_ANCHOR it does not, the alignment is zero, and the final goes out
    whole with the usual warning.

    Dropping is the worse failure of the two a threshold can make: a repeat is
    at least heard, a drop is only in the log.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("a name for a colour gives us all a way to", 5.0))
    committed = segmenter.feed(
        _interim("a name for a colour gives us all a way to talk about it", 10.0)
    )
    assert [u.text for u in committed] == ["a name for a colour gives us all a way to"]

    new_speech = "Knowing its name also gives us all a way to argue about it."
    with caplog.at_level(logging.DEBUG):
        units = segmenter.feed(_final_result(new_speech, 240.0))
    assert [u.text for u in units] == [new_speech]
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "shares no words" in caplog.text


def test_a_run_of_eight_spoken_words_anchors_and_seven_does_not():
    """The boundary, in literal lengths rather than MIN_ANCHOR arithmetic.

    Literal on purpose: the threshold is a measurement (see MIN_ANCHOR's
    comment), and a test that followed the constant would let it move
    without anyone re-reading why it is 8. Both candidates open on words that
    were never spoken, so only the anchored arm can align them at all.
    """
    spoken = _tokens("we walked along the canal as far as the old mill")

    # "the canal as far as the old mill": the last 8 spoken, after "to" where
    # the spoken text had "along".
    eight = _tokens("down to the canal as far as the old mill and back")
    assert _overlap(spoken, eight) == (10, 8)

    # "canal as far as the old mill": the last 7, after "a" instead of "the".
    seven = _tokens("down by a canal as far as the old mill and back")
    assert _overlap(spoken, seven) == (0, 0)

    # Last, so that moving the constant fails on the behaviour above first.
    assert MIN_ANCHOR == 8


def test_a_spoken_run_found_twice_anchors_on_the_later_copy():
    """Of two equally long copies of the run, the later one wins.

    Both copies are word for word the last eleven words the listener heard.
    Anchoring on the earlier copy emits the later one - eleven words spoken
    twice, the repeat the anchored arm exists to stop. The later copy costs
    the words between the two only if the speaker really did say the same run
    twice inside one hypothesis, which is the price the containment arm
    already pays for a verbatim repeat.
    """
    spoken = _tokens("we walked along the canal as far as the old mill")
    candidate = _tokens(
        "so we walked along the canal as far as the old mill "
        "and then we walked along the canal as far as the old mill again"
    )
    assert _overlap(spoken, candidate) == (25, 11)


def test_a_revision_inside_a_long_spoken_text_anchors_past_it(caplog):
    """The anchored arm narrows a cost the content alignment used to pay.

    A word substituted INSIDE the spoken text breaks both of the first two
    arms, and before this arm the whole final went out again
    (test_a_final_that_revises_inside_the_spoken_text_is_emitted_whole, which
    still holds there: four spoken words are below MIN_ANCHOR). With at least
    MIN_ANCHOR unchanged words after the substitution, the run after it
    anchors and only the new tail is emitted.

    The revision itself is not honoured - "rose" is dropped, the listener
    having heard "rises" - which is what the count-based version did too, and
    the only alternative is repeating the sentence.
    """
    segmenter = LocalAgreementSegmenter()
    _speak_the_river(segmenter)
    with caplog.at_level(logging.DEBUG):
        units = segmenter.feed(
            _final_result(
                "the river rose every spring, so the farmers move their herds "
                "uphill, before the water reaches the lower fields.",
                20.0,
            )
        )
    assert [u.text for u in units] == ["before the water reaches the lower fields."]
    assert "dropped 3 leading word(s) in front of a run of 9" in caplog.text


def test_an_anchor_aligning_fewer_words_than_were_spoken_is_still_reported_as_a_drop(
    caplog,
):
    """The anchored report must come before the "revised" one, not after it.

    Here the hypothesis both re-windowed forward past "the river" and revised
    the word before the run, so k (10) counts one dropped word plus a 9-word
    run and comes out SMALLER than the 12 spoken. Checked in the other order,
    that reads as "10 of 12 spoken word(s) still align" - nothing lost, at
    debug, which the session log does not record - when a word has in fact
    been thrown away.
    """
    segmenter = LocalAgreementSegmenter()
    _speak_the_river(segmenter)
    with caplog.at_level(logging.DEBUG):
        units = segmenter.feed(
            _final_result(
                "rising every spring, so the farmers move their herds uphill, "
                "before the water reaches the lower fields.",
                20.0,
            )
        )
    assert [u.text for u in units] == ["before the water reaches the lower fields."]
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "dropped 1 leading word(s) in front of a run of 9" in caplog.text


# --- replayed against the real Chirp capture (chirp_interims.json) -----------

FIXTURE = Path(__file__).parent / "fixtures" / "chirp_interims.json"


def _capture(name):
    for probe in json.loads(FIXTURE.read_text()):
        if probe["name"] == name:
            return probe["results"]
    raise AssertionError(f"no {name!r} in the capture")


def _replay(name, segmenter=None):
    segmenter = segmenter if segmenter is not None else LocalAgreementSegmenter()
    units = []
    for row in _capture(name):
        result = AsrResult(
            direction=Direction.IN, text=row["text"], is_final=row["final"],
            t_start=row["end_offset"], t_end=row["end_offset"],
        )
        units.extend(segmenter.feed(result))
    return units


@pytest.mark.parametrize("capture", ["turns", "medium"])
def test_a_capture_with_no_early_commits_is_identical_to_finals_only(capture):
    """Text AND spans, not just text. This is the "changes nothing for
    ordinary conversation" claim in --no-early-commit's help, README and the
    spec, checked instead of asserted.

    Spans are the half that broke: _emit used to take t_start from the running
    span watermark unconditionally, so a final that committed nothing early
    still chained from the PREVIOUS utterance's end. Every row's start time
    moved back by one utterance, and render_markdown sorts on t_start - so in
    an interleaved two-way call the bilingual transcript came out reordered,
    on the default path, for every call.

    Both captures qualify: `turns` produces no interims at all and `medium`
    produces one, and a commit needs two that agree.
    """
    expected = [
        (u.direction, u.text, u.t_start, u.t_end, u.continues)
        for u in _replay(capture, FinalsOnlySegmenter())
    ]
    got = [
        (u.direction, u.text, u.t_start, u.t_end, u.continues)
        for u in _replay(capture, LocalAgreementSegmenter())
    ]
    assert got == expected


def test_the_real_monologue_is_committed_in_pieces_instead_of_one_block():
    """FinalsOnlySegmenter produced two units on this capture, 29.3 s apart.
    That is the behaviour this segmenter replaced, not the current one.

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

    Weaker than it looks under mutation: dropping to one agreement still never
    produces this literal substring, but for an unrelated reason - the
    mutated segmenter locks in the earlier hypothesis's "сверхурочно." before
    the comma form ever arrives. The unit-count, word-list and span tests are
    what actually catch that mutation; this one should not be trusted alone.
    """
    spoken = " ".join(u.text for u in _replay("monologue"))
    assert "сверхурочно, результат" not in spoken


def test_the_real_capture_survives_the_case_change():
    """"Что" became "что" between two hypotheses whose words were identical.

    A surface comparison finds a common prefix of zero here and commits
    nothing for the entire monologue. But the phrase's mere presence in the
    output does not pin that: with `.lower()` removed, agreement on this pair
    stalls for exactly one round and "несколько важных задач" still turns up
    a round later, folded into a longer unit. What pins the one-round delay
    `.lower()` prevents is the exact text of the SECOND unit - lowering is
    what lets it close after just one more interim instead of two.
    """
    units = _replay("monologue")
    assert units[1].text == (
        "что у нас есть несколько важных задач, которые нужно решить как можно быстрее."
    )


def test_the_real_capture_loses_no_words():
    """Nothing is dropped or duplicated when no final revises what was spoken.

    Not the general invariant it looks like: a final that revises text already
    spoken cannot be honoured either way, and this capture never triggers one
    - the inserted "а" lands past the commit point, not inside it. What
    happens when it does land inside is covered by
    test_a_final_that_revises_inside_the_spoken_text_is_emitted_whole (the
    text goes out again) and test_a_final_that_drops_spoken_text_still_says_so
    (it does not go out at all).
    """
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


def test_the_real_monologues_spans_match_the_measured_capture():
    """The transcript's latency column is built from these spans.

    Pinned to the six (t_start, t_end) pairs measured against this capture -
    each interim's end_offset once the two hypotheses feeding a commit have
    both arrived, and the closing final's end_offset for the last unit.

    The FIRST pair is the odd one out and deliberately so: that unit is the
    final of a short opening utterance that committed nothing early, so it
    carries a whole utterance's span - t_start == t_end == its own end offset,
    exactly what FinalsOnlySegmenter produces. Only the five that follow, cut
    out of one long utterance, chain from the previous piece.
    """
    spans = [(u.t_start, u.t_end) for u in _replay("monologue")]
    assert spans == [
        (6.04, 6.04),
        (6.04, 11.12),
        (11.12, 16.12),
        (16.12, 21.12),
        (21.12, 26.12),
        (26.12, 34.7),
    ]
