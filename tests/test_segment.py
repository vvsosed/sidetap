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
