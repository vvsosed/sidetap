import pytest

from sidetap.types import (
    BLOCK_BYTES,
    MIC,
    REMOTE,
    TTS_RATE,
    AsrResult,
    AudioChunk,
    Direction,
    Latency,
    Record,
    Translated,
    Unit,
)


def test_block_bytes_is_100ms_of_16k_mono_s16():
    # 16000 samples/s * 2 bytes * 0.1 s
    assert BLOCK_BYTES == 3200


def test_direction_in_reads_the_remote_track():
    assert Direction.IN.track == REMOTE
    assert Direction.OUT.track == MIC


def test_direction_opposite_flips():
    assert Direction.IN.opposite is Direction.OUT
    assert Direction.OUT.opposite is Direction.IN


def test_direction_values_match_the_wire_format():
    # These strings reach the durable JSONL transcript and Textual widget ids.
    # Identity-based tests above would not catch the two literals being swapped.
    assert Direction.IN.value == "in"
    assert Direction.OUT.value == "out"


def test_audio_chunk_is_frozen():
    chunk = AudioChunk(track=MIC, pcm=b"\x00" * BLOCK_BYTES, t_start=1.5)
    with pytest.raises(AttributeError):
        chunk.t_start = 2.0


def test_asr_result_carries_finality():
    result = AsrResult(
        direction=Direction.IN, text="привет", is_final=True, t_start=1.0, t_end=2.0
    )
    assert result.is_final
    assert result.confidence is None


def test_unit_from_result_copies_the_span():
    result = AsrResult(
        direction=Direction.IN, text="привет", is_final=True, t_start=1.0, t_end=2.0
    )
    unit = Unit.from_result(result)
    assert unit.direction is Direction.IN
    assert unit.text == "привет"
    assert (unit.t_start, unit.t_end) == (1.0, 2.0)


def test_latency_totals_its_stages():
    latency = Latency(asr_ms=100.0, mt_ms=50.0, tts_ms=250.0)
    assert latency.total_ms == 400.0


def test_latency_defaults_to_zero():
    assert Latency().total_ms == 0.0


def test_translated_duration_is_derived_from_pcm_length():
    unit = Unit(direction=Direction.OUT, text="hello", t_start=0.0, t_end=1.0)
    # 24000 samples/s * 2 bytes = 48000 bytes per second
    translated = Translated(unit=unit, text="привет", pcm=b"\x00" * 48_000)
    assert translated.audio_s == pytest.approx(1.0)
    assert TTS_RATE == 24_000


def test_record_defaults_to_not_dropped():
    unit = Unit(direction=Direction.IN, text="a", t_start=0.0, t_end=1.0)
    record = Record(unit=unit, target_text="b", latency=Latency())
    assert record.dropped is False


def test_full_synthesis_time_is_recorded_but_not_counted_as_felt_latency():
    latency = Latency(asr_ms=100.0, mt_ms=50.0, tts_ms=200.0, tts_total_ms=2339.0)
    # total_ms is what the listener waited, not what synthesis cost.
    assert latency.total_ms == 350.0


def test_a_unit_does_not_continue_by_default():
    """FinalsOnlySegmenter emits whole utterances, so nothing follows them.

    The flag is opt-in for exactly that reason: a segmenter that does not know
    about speech runs cannot accidentally hold the duck closed.
    """
    result = AsrResult(
        direction=Direction.IN, text="hi", is_final=True, t_start=1.0, t_end=2.0
    )
    assert Unit.from_result(result).continues is False
    assert Unit(
        direction=Direction.IN, text="hi", t_start=1.0, t_end=2.0, continues=True
    ).continues is True
