from sidetap.segment import FinalsOnlySegmenter
from sidetap.types import AsrResult, Direction


def _result(text="hello", is_final=True, t_start=1.0, t_end=2.0):
    return AsrResult(
        direction=Direction.IN,
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


def test_the_segmenter_is_stateless_across_directions():
    segmenter = FinalsOnlySegmenter()
    segmenter.feed(_result(is_final=False, text="partial"))
    units = segmenter.feed(_result(text="complete"))
    # No leakage from the discarded interim.
    assert [u.text for u in units] == ["complete"]
