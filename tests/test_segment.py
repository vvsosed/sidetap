import json
import logging
import re
from pathlib import Path

import pytest

from sidetap.segment import FinalsOnlySegmenter, LocalAgreementSegmenter, _agreed, _tokens
from sidetap.types import AsrResult, Direction


def _result(text="hello", is_final=True, t_start=1.0, t_end=2.0, direction=Direction.IN):
    return AsrResult(
        direction=direction,
        text=text,
        is_final=is_final,
        t_start=t_start,
        t_end=t_end,
    )


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


def test_a_committed_prefix_is_dated_by_the_older_hypothesis():
    """Its content is confirmed only through the audio the OLDER one covered.

    The newer hypothesis has heard more but agreed on less. Dating the unit by
    the newer one would report a clause as fresh when it is already five
    seconds old, in the transcript column that is the stated evidence for
    whether committing early was worth doing at all.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    units = segmenter.feed(_interim("что у нас есть, несколько", 10.0))
    assert (units[0].t_start, units[0].t_end) == (0.0, 5.0)


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

    This does NOT prove the committed list was reset - one interim cannot,
    because the previous-hypothesis reset alone forces the same answer. See
    test_a_new_utterance_is_not_offset_by_the_last_one, which feeds two.
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


def test_a_final_that_revises_committed_text_emits_from_the_commit_point():
    """Nothing can be un-spoken, so a revision cannot be honoured.

    The listener already heard "есть". Emitting from where the final diverges
    would repeat "было" over the top of it; emitting from the commit point
    drops a word they will never know was missing. Experiment 6 caught a real
    final doing this, which is why the segmenter logs it rather than trusting
    that finals only ever extend.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    segmenter.feed(_interim("что у нас есть, несколько задач", 10.0))
    units = segmenter.feed(_final_result("что у нас было несколько задач.", 15.0))
    assert [u.text for u in units] == ["несколько задач."]


def test_a_new_utterance_is_not_offset_by_the_last_one():
    """Two interims after the final, not one - one cannot see the leak.

    Without the reset, the stale committed count offsets the new utterance's
    growth slice and silently eats its opening words. One interim hides it,
    because the first interim of any utterance commits nothing anyway.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    segmenter.feed(_interim("что у нас есть, несколько задач", 10.0))
    segmenter.feed(_final_result("что у нас есть, несколько задач.", 15.0))

    segmenter.feed(_interim("совсем другое, но нужно", 20.0))
    units = segmenter.feed(_interim("совсем другое, но нужно сказать", 25.0))
    assert [u.text for u in units] == ["совсем другое,"]


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


def test_a_final_that_drops_committed_text_still_says_so(caplog):
    """The worst disagreement is the one with nothing left to emit.

    A final shorter than what was committed slices to no remainder at all, so
    the emit path never runs. Checking the revision first is what stops that
    case - the loudest available sign that committing early went wrong - from
    passing in complete silence.

    The final here still SHARES its opening words with the commit, which is
    what keeps it on this path: a final sharing nothing at all is the stale
    -state case below, which discards the commit instead of slicing by it.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    segmenter.feed(_interim("что у нас есть, несколько задач", 10.0))
    with caplog.at_level(logging.DEBUG):
        units = segmenter.feed(_final_result("что у нас.", 15.0))
    assert units == []
    assert "revised" in caplog.text


def test_a_final_sharing_nothing_with_the_commit_is_emitted_whole(caplog):
    """The stream restarted mid-utterance, and nothing told the segmenter.

    RecognitionWorker rebuilds its stream every MAX_STREAM_SECONDS and after
    any non-fatal error, and neither path guarantees a final - so a commit
    made before the break is still sitting there when the first final of the
    NEXT utterance arrives. Slicing that final by the stale commit's length
    cuts words off the front of a sentence nobody has heard: six committed
    words turned "completely new sentence here." into nothing at all.

    Warning, not debug: this is speech being lost, not a tail being revised.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("one two three four five six,", 5.0))
    segmenter.feed(_interim("one two three four five six, seven eight", 10.0))
    with caplog.at_level(logging.DEBUG):
        units = segmenter.feed(_final_result("completely new sentence here.", 240.0))
    assert [u.text for u in units] == ["completely new sentence here."]
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_a_stale_commit_does_not_shorten_the_final_it_shares_nothing_with():
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


def test_a_discarded_commit_leaves_the_final_carrying_a_whole_utterances_span():
    """Nothing of the stale commit describes this utterance, its span least
    of all - chaining from it would date the sentence to before the break.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("one two three four five six,", 5.0))
    segmenter.feed(_interim("one two three four five six, seven eight", 10.0))
    unit = segmenter.feed(_final_result("completely new sentence here.", 240.0))[0]
    assert (unit.t_start, unit.t_end) == (240.0, 240.0)


def test_an_ordinary_tail_revision_still_slices_by_the_commit(caplog):
    """The discard rule must not swallow the case Experiment 6 measured.

    There the final's first 51 words matched the commit exactly and the
    insertion was past the commit point. A final that agrees on SOME leading
    words is an ordinary revision: it keeps the count slice and the debug
    log, because emitting it whole would repeat a clause already spoken.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("что у нас есть,", 5.0))
    segmenter.feed(_interim("что у нас есть, несколько задач", 10.0))
    with caplog.at_level(logging.DEBUG):
        units = segmenter.feed(_final_result("что у нас было несколько задач.", 15.0))
    assert [u.text for u in units] == ["несколько задач."]
    assert [r.levelno for r in caplog.records] == [logging.DEBUG]


def test_an_interim_that_revises_committed_text_says_so(caplog):
    """_finalise has had this log since the start; _interim had nothing.

    `growth` slices by POSITION, so a word inserted inside the committed
    prefix shifts every later one and the next commit re-speaks a clause the
    listener already heard - "d," twice below. Not corrected here (Experiment
    6 measured interims as purely additive, so correcting would trade a
    should-never-happen shift for a real risk of repetition), but logged, so
    manual-smoke.md's "no word repeated" check has evidence instead of a
    mystery.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("a b c d,", 5.0))
    segmenter.feed(_interim("a b c d, e", 10.0))
    with caplog.at_level(logging.DEBUG):
        segmenter.feed(_interim("a b X c d, e f", 15.0))
        segmenter.feed(_interim("a b X c d, e f g.", 20.0))
    assert "interim revised committed text" in caplog.text


def test_a_stale_commit_does_not_eat_the_next_utterances_early_commits(caplog):
    """The interim half of the stream-restart defect.

    Same break as the final-side tests, but the new utterance is long enough
    to commit early on its own. Slicing by the stale six-word commit turned
    "alpha beta gamma, delta epsilon" into "eta, theta" - the opening words
    of a sentence nobody has heard, gone, on the default path.

    Warning, not debug, and the mirror of the check in _finalise: fixing only
    the final half would leave the final inheriting a _committed that is
    stale AND wrongly sliced.
    """
    segmenter = LocalAgreementSegmenter()
    segmenter.feed(_interim("one two three four five six,", 5.0))
    segmenter.feed(_interim("one two three four five six, seven eight", 10.0))
    with caplog.at_level(logging.DEBUG):
        # The stream restarted. A new utterance, sharing nothing.
        assert segmenter.feed(_interim("alpha beta gamma, delta", 240.0)) == []
    assert [r.levelno for r in caplog.records] == [logging.WARNING]

    units = segmenter.feed(_interim("alpha beta gamma, delta epsilon zeta", 245.0))
    assert [u.text for u in units] == ["alpha beta gamma,"]


def test_a_discarded_commit_does_not_chain_the_next_span_across_the_break():
    """The watermark is as stale as the commit it belongs to.

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


def test_the_final_after_a_discarded_interim_commit_is_not_sliced_twice():
    """The two halves must not compound.

    Once the interim side has discarded and re-committed honestly, the final
    is an ordinary tail: it agrees with the live commit, so it keeps the
    count slice and emits only what is new. Nothing is lost and nothing is
    repeated.
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
    """Nothing is dropped or duplicated when no final revises a commit.

    Not the general invariant it looks like: _finalise deliberately drops
    words when a final revises text already committed, and this capture never
    triggers that - the inserted "а" lands in the uncommitted remainder, not
    inside the committed prefix. The intentional-drop path is covered by
    test_a_final_that_revises_committed_text_emits_from_the_commit_point and
    its neighbours.
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
