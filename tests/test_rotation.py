import pytest

from sidetap.rotation import MAX_STREAM_SECONDS, AudioTimeline, StreamClock


def test_rotation_threshold_stays_under_googles_five_minute_cap():
    assert MAX_STREAM_SECONDS < 300


def test_does_not_rotate_before_the_limit():
    clock = StreamClock()

    assert clock.should_rotate(239.0) is False
    assert clock.should_rotate(240.0) is True


def test_absolute_applies_the_offset():
    clock = StreamClock(offset=100.0)

    assert clock.absolute(5.0) == 105.0


def test_timestamps_stay_continuous_across_three_rotations():
    clock = StreamClock()
    seen = []

    # Each stream runs for 240 s of audio, reporting times relative to itself.
    for _ in range(3):
        for relative in (10.0, 120.0, 239.0):
            seen.append(clock.absolute(relative))
        clock = clock.rotated(last_chunk_t=clock.absolute(240.0))

    assert seen == sorted(seen), "timestamps must never go backwards"
    assert seen[0] == 10.0
    assert seen[3] == 250.0
    assert seen[6] == 490.0


def test_rotation_never_moves_the_offset_backwards():
    clock = StreamClock(offset=500.0)

    # A late or duplicated chunk reporting an earlier time must not rewind us.
    assert clock.rotated(last_chunk_t=10.0).offset == 500.0


def test_rotation_preserves_the_configured_interval():
    # Losing max_stream_s here would silently reset the rotation interval to
    # the 240 s default after the first rotation, with no other test noticing.
    clock = StreamClock(max_stream_s=1.0)

    rotated = clock.rotated(last_chunk_t=5.0)

    assert rotated.max_stream_s == 1.0
    assert rotated.should_rotate(1.0) is True


def test_timeline_maps_audio_position_back_to_real_time():
    # Two blocks sent 10 seconds apart in real time are adjacent in the audio
    # the engine receives, because the gate dropped everything between them.
    timeline = AudioTimeline()
    timeline.sent(0.0)
    timeline.sent(10.0)

    assert timeline.absolute(0.0) == 0.0
    assert timeline.absolute(0.1) == pytest.approx(10.0)
    assert timeline.absolute(0.15) == pytest.approx(10.05)


def test_timeline_falls_back_to_the_offset_when_nothing_was_sent():
    # A stream that failed on connect sent no audio, so there is nothing to
    # map; behave like StreamClock rather than raising.
    timeline = AudioTimeline(offset=300.0)

    assert timeline.absolute(5.0) == 305.0


def test_timeline_extrapolates_past_the_last_block_it_sent():
    timeline = AudioTimeline()
    timeline.sent(50.0)

    # That block covers audio [0.0, 0.1) captured at 50.0, so position 0.25
    # extrapolates 1:1 to 50.25 - not 50.15; there is no sound slope-1
    # mapping that gives anything else.
    assert timeline.absolute(0.25) == pytest.approx(50.25)
