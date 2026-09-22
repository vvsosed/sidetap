import logging

import pytest

from sidetap.playout import CHUNK_MS, STARVE_LIMIT_TICKS, DuckControl, Playout
from sidetap.types import TTS_BYTES_PER_S, Direction, Translated, Unit
from tests.conftest import FakeAudioSink, FakeVolumeControl

CHUNK_BYTES = TTS_BYTES_PER_S * CHUNK_MS // 1000


def _unit(text: str = "hi") -> Unit:
    return Unit(direction=Direction.IN, text=text, t_start=0.0, t_end=1.0)


def _translated(seconds: float, text: str = "hi") -> Translated:
    return Translated(
        unit=_unit(text), text=text, pcm=b"\x01\x02" * int(TTS_BYTES_PER_S * seconds / 2)
    )


def test_an_idle_playout_writes_silence():
    sink = FakeAudioSink()
    playout = Playout(Direction.IN, sink)
    assert playout.tick() is False
    assert sink.written == b"\x00" * CHUNK_BYTES


def test_queued_audio_is_written_in_chunks():
    sink = FakeAudioSink()
    playout = Playout(Direction.IN, sink)
    playout.submit(_translated(0.1))  # 100 ms -> 5 chunks at 20 ms

    speech_chunks = 0
    for _ in range(5):
        if playout.tick():
            speech_chunks += 1
    assert speech_chunks == 5
    assert sink.written == b"\x01\x02" * (CHUNK_BYTES * 5 // 2)


def test_playout_returns_to_silence_when_the_queue_empties():
    playout = Playout(Direction.IN, FakeAudioSink())
    playout.submit(_translated(0.02))
    assert playout.tick() is True
    assert playout.tick() is False


def test_the_duck_closes_on_speech_and_opens_on_silence():
    volume = FakeVolumeControl()
    duck = DuckControl(volume, object_id=42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    playout.submit(_translated(0.02))
    playout.tick()
    assert volume.calls[-1] == (42, 0.0)
    playout.tick()
    assert volume.calls[-1] == (42, 1.0)


def test_the_duck_is_idempotent():
    """One wpctl call per transition, not one per 20 ms chunk."""
    volume = FakeVolumeControl()
    duck = DuckControl(volume, object_id=42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    playout.submit(_translated(0.1))
    for _ in range(5):
        playout.tick()
    assert volume.calls == [(42, 0.0)]


def test_the_duck_starts_open():
    """Default-open is the IN direction's fail-safe.

    If the pipeline dies nothing is ever written, the duck never closes, and
    the remote party's raw voice stays audible.
    """
    volume = FakeVolumeControl()
    playout = Playout(Direction.IN, FakeAudioSink(), duck=DuckControl(volume, object_id=42))
    for _ in range(3):
        playout.tick()
    assert volume.calls == []


def test_backlog_is_measured_in_seconds_of_unspoken_audio():
    playout = Playout(Direction.IN, FakeAudioSink())
    playout.submit(_translated(2.0))
    playout.submit(_translated(3.0))
    assert playout.backlog_s() == 5.0


def test_over_the_cap_the_oldest_are_dropped():
    dropped = []
    playout = Playout(
        Direction.IN, FakeAudioSink(), lag_cap_s=5.0, on_dropped=dropped.append
    )
    playout.submit(_translated(2.0, "first"))
    playout.submit(_translated(2.0, "second"))   # 4.0s, still under the cap
    playout.submit(_translated(2.0, "third"))    # 6.0s, over it

    # Oldest go first, and only as many as it takes to fit.
    assert [d.unit.text for d in dropped] == ["first"]
    assert playout.backlog_s() == 4.0
    assert playout.dropped == 1


def test_a_single_item_longer_than_the_cap_is_still_spoken():
    """Dropping it would make a long sentence unsayable at any cap."""
    dropped = []
    playout = Playout(
        Direction.IN, FakeAudioSink(), lag_cap_s=1.0, on_dropped=dropped.append
    )
    playout.submit(_translated(4.0))
    assert dropped == []
    assert playout.tick() is True


def test_finished_items_are_reported_once():
    spoken = []
    playout = Playout(Direction.IN, FakeAudioSink(), on_spoken=spoken.append)
    playout.submit(_translated(0.04, "done"))
    playout.tick()
    assert spoken == []
    playout.tick()
    assert [s.unit.text for s in spoken] == ["done"]
    playout.tick()
    assert len(spoken) == 1


def test_a_submitted_utterance_with_no_audio_reports_nothing():
    """A real TTS response can return zero bytes for non-empty text - unlike
    finish()'s empty-pcm case, submit() never unqueues it, so this is the
    only way the closed/offset==0 branch in _advance_locked is reached."""
    spoken = []
    playout = Playout(Direction.IN, FakeAudioSink(), on_spoken=spoken.append)
    playout.submit(_translated(0))
    assert playout.tick() is False
    assert spoken == []


def test_flush_drops_the_queue_but_not_the_chunk_in_flight():
    """pw-cat cannot unplay bytes already in the pipe.

    Reporting otherwise would make the drop-backlog hotkey lie.
    """
    playout = Playout(Direction.IN, FakeAudioSink())
    playout.submit(_translated(1.0))
    playout.submit(_translated(1.0))
    playout.tick()
    n = playout.flush()
    assert n == 1
    assert playout.backlog_s() == 0.0


def test_a_dead_sink_does_not_become_a_busy_loop():
    """pw-cat blocking is the ONLY thing pacing run() on the healthy path.

    A dead sink's write() returns immediately, so without the fallback wait
    this loop spins a core until hangup.
    """
    import threading
    import time

    class DeadSink(FakeAudioSink):
        failed = True

        def write(self, pcm):
            self.chunks.append(pcm)

    stop = threading.Event()
    sink = DeadSink()
    playout = Playout(Direction.IN, sink)
    thread = threading.Thread(target=playout.run, args=(stop,), daemon=True)
    thread.start()
    time.sleep(0.25)
    stop.set()
    thread.join(timeout=2)

    # At ~20 ms per iteration it cannot exceed ~15 writes in 250 ms. An
    # unpaced loop would manage orders of magnitude more.
    assert len(sink.chunks) < 100


def test_a_failed_sink_does_not_stall_the_queue():
    class DeadSink(FakeAudioSink):
        failed = True

        def write(self, pcm):
            pass

    playout = Playout(Direction.IN, DeadSink())
    playout.submit(_translated(0.02))
    playout.tick()
    playout.tick()
    assert playout.backlog_s() == 0.0


def test_a_raising_on_spoken_still_leaves_the_duck_open_and_the_sink_closed():
    """A consumer's bookkeeping failure (e.g. Task 25's transcript write)
    must not leave the duck stuck closed.

    A dead pipeline is supposed to fail safe: nothing gets written, the duck
    never closes, and the user hears the remote party untranslated. A duck
    stuck closed because on_spoken raised inverts that into silence, the one
    outcome this module exists to prevent.
    """
    import threading
    import time

    def boom(item):
        raise ValueError("boom")

    volume = FakeVolumeControl()
    duck = DuckControl(volume, object_id=42)
    sink = FakeAudioSink()
    playout = Playout(Direction.IN, sink, duck=duck, on_spoken=boom)
    playout.submit(_translated(0.02))

    stop = threading.Event()
    thread = threading.Thread(target=playout.run, args=(stop,), daemon=True)
    thread.start()
    time.sleep(0.1)
    stop.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert volume.calls[-1] == (42, 1.0)  # duck ended up open
    assert sink.closed is True


def test_a_raising_on_dropped_does_not_propagate_out_of_submit():
    """A raise here today happens inside _trim_locked, called from submit()
    on the producer thread - so uncaught it would silently stop that thread
    from submitting anything further.
    """

    def boom(item):
        raise ValueError("boom")

    playout = Playout(
        Direction.IN, FakeAudioSink(), lag_cap_s=5.0, on_dropped=boom
    )
    playout.submit(_translated(2.0, "first"))
    playout.submit(_translated(2.0, "second"))
    playout.submit(_translated(2.0, "third"))  # over the cap; on_dropped raises

    assert playout.dropped == 1
    assert playout.backlog_s() == 4.0


def test_the_cap_drops_a_queued_item_even_while_the_current_one_still_plays():
    """The ordinary shape of a monologue: one utterance already playing in
    _current, the next one queued behind it. Looking only at _queue's length
    misses exactly this, the most common case - the cap would never act
    during an ordinary monologue.
    """
    dropped = []
    playout = Playout(
        Direction.IN, FakeAudioSink(), lag_cap_s=5.0, on_dropped=dropped.append
    )
    playout.submit(_translated(10.0, "long"))
    playout.tick()  # moves "long" into _current; _queue is now empty
    playout.submit(_translated(3.0, "short"))  # _queue is len 1, but backlog is ~13s

    assert [d.unit.text for d in dropped] == ["short"]
    assert playout.dropped == 1


def test_a_failed_duck_transition_does_not_flip_the_closed_flag():
    """set_volume returns False rather than raising when wpctl fails.

    Flipping the flag anyway would desync it from the real volume: the next
    speech chunk would see `_closed` already True and skip retrying the
    close, so the duck silently stops working after one transient failure.
    """
    volume = FakeVolumeControl(ok=False)
    duck = DuckControl(volume, object_id=42)

    duck.close()
    duck.close()
    # Both calls actually reached wpctl - the flag never flipped, so close()
    # kept retrying rather than assuming the first call had worked.
    assert volume.calls == [(42, 0.0), (42, 0.0)]


def test_a_suppressed_playout_writes_silence_and_speaks_nothing():
    sink = FakeAudioSink()
    playout = Playout(Direction.IN, sink)
    playout.set_suppressed(True)
    assert playout.tick() is False
    # sink.written is already the joined bytes (see FakeAudioSink.written in
    # conftest.py), not a list of chunks - re-joining it with b"".join()
    # would iterate over its individual byte VALUES (ints) and raise
    # TypeError, not check anything.
    assert sink.written and set(sink.written) == {0}


def test_entering_bypass_throws_the_backlog_away():
    """It is a translation of a conversation that already happened without it."""
    playout = Playout(Direction.IN, FakeAudioSink())
    playout.submit(_translated(1.0))
    playout.set_suppressed(True)
    assert playout.backlog_s() == 0.0


def test_leaving_suppression_throws_away_what_piled_up_behind_it():
    """Nothing upstream knows a playout is suppressed.

    pipeline._speak keeps synthesising and keeps queueing the whole time, so
    the queue refills while nobody is listening. begin() trims it to the lag
    cap, which bounds the pile at ~20 s rather than preventing it - and 20 s
    of a conversation that has already moved on is exactly what README
    promises un-muting does not replay.
    """
    playout = Playout(Direction.OUT, FakeAudioSink())
    playout.set_suppressed(True)
    playout.submit(_translated(1.0))
    assert playout.backlog_s() == 1.0, "the queue does fill while suppressed"

    playout.set_suppressed(False)
    assert playout.backlog_s() == 0.0


def test_a_duck_whose_node_appears_late_still_ducks():
    """Router.engage() returns before pw-loopback registers the duck node.

    Resolving the id once, at construction, meant no duck was ever built: the
    original played underneath every translation for the entire call, and
    nothing was logged because nothing ever tried.
    """
    volume = FakeVolumeControl()
    duck_id = None
    duck = DuckControl(volume, lambda: duck_id)

    duck.close()
    assert volume.calls == [], "nothing to set the volume on yet"

    duck_id = 77
    duck.close()
    assert volume.calls == [(77, 0.0)], "the duck never engaged once it appeared"
    duck.open()
    assert volume.calls == [(77, 0.0), (77, 1.0)]


def test_a_plain_integer_object_id_still_works():
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    duck.close()
    assert volume.calls == [(42, 0.0)]


def test_an_utterance_can_be_played_while_it_is_still_arriving():
    """The two appends straddle a chunk boundary (410 ms then 390 ms, not an
    even 400/400) so the read that crosses from one append's tail into the
    other's head is exercised, not just a clean join."""
    sink = FakeAudioSink()
    playout = Playout(Direction.IN, sink)
    item = playout.begin(_unit(), "hi")

    # Nothing has arrived yet, so there is nothing to play.
    assert playout.tick() is False

    # Enough has arrived for a full 20 ms chunk to be read.
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.41 / 2))
    assert playout.tick() is True

    # More arrives while the first part is still playing.
    playout.append(item, b"\x03\x04" * int(TTS_BYTES_PER_S * 0.39 / 2))
    playout.finish(item)
    spoken = sum(1 for _ in range(60) if playout.tick())
    assert spoken == 39  # 800 ms total, minus the one chunk already written


def test_backlog_counts_only_bytes_that_have_arrived():
    playout = Playout(Direction.IN, FakeAudioSink())
    item = playout.begin(_unit(), "hi")
    assert playout.backlog_s() == 0.0

    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.5 / 2))
    assert playout.backlog_s() == pytest.approx(0.5)

    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.5 / 2))
    assert playout.backlog_s() == pytest.approx(1.0)


def test_on_spoken_fires_once_when_a_streamed_utterance_drains():
    """The streaming case: finish() arrives only after tick() has already
    read every byte that had arrived, so the utterance retires - and
    on_spoken fires - from the silence branch, not the speech branch. That
    is a different code path from test_finished_items_are_reported_once,
    where the whole item is closed from the start and always retires
    mid-chunk, on the speech branch.

    0.42 s, not some shorter, tidier number: it has to clear the 400 ms
    start threshold while still open, or it never starts playing at all and
    this never reaches the silence branch it exists to exercise.
    """
    spoken = []
    playout = Playout(Direction.IN, FakeAudioSink(), on_spoken=spoken.append)
    item = playout.begin(_unit(), "hi")
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.42 / 2))  # 21 chunks

    for _ in range(21):
        assert playout.tick() is True
    assert spoken == []  # not closed yet - nothing to report

    playout.finish(item)
    assert playout.tick() is False  # silence tick: this is where it retires
    assert len(spoken) == 1
    assert spoken[0].text == "hi"
    assert spoken[0].audio_s == pytest.approx(0.42)


def test_a_sub_chunk_append_does_not_get_padded_and_played():
    """0 < unread < CHUNK_BYTES on an utterance that is still open must wait
    for more, not zero-pad the fragment into the sink - that would splice
    silence into the middle of a word, and would also skip the starvation
    counter's increment for this tick, holding the duck closed with no bound
    while a trickling producer stalls.

    The item must first clear the 400 ms start threshold and become
    _current, or it is never promoted at all and this exercises nothing -
    a fragment sitting untouched in _queue looks identical to one that was
    correctly withheld from an open _current.
    """
    sink = FakeAudioSink()
    playout = Playout(Direction.IN, sink)
    item = playout.begin(_unit(), "hi")
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.4 / 2))  # 20 chunks
    for _ in range(20):
        assert playout.tick() is True  # promotes it, then drains it - still open

    playout.append(item, b"\x01\x02" * 10)  # far under one 20 ms chunk

    assert playout.tick() is False
    assert sink.chunks[-1] == b"\x00" * CHUNK_BYTES


def test_the_final_partial_chunk_of_a_closed_utterance_is_padded():
    """Unlike the sub-chunk case above, a partial remainder on a CLOSED
    utterance is a genuine tail and must be padded and played."""
    sink = FakeAudioSink()
    playout = Playout(Direction.IN, sink)
    item = playout.begin(_unit(), "hi")
    audio = b"\x01\x02" * int(TTS_BYTES_PER_S * 0.03 / 2)  # one chunk + a tail
    playout.append(item, audio)
    playout.finish(item)

    assert playout.tick() is True  # the full first chunk
    assert playout.tick() is True  # the padded tail
    assert playout.tick() is False  # drained

    assert sink.written[: len(audio)] == audio
    assert set(sink.written[len(audio) :]) == {0}


def test_append_after_finish_is_refused():
    """closed is what makes the zero-pad's premise sound: a partial tail is
    only played because closed means no more audio is coming. That is only
    true if append() actually refuses audio once closed - otherwise a late
    chunk could arrive after the tail was already played and padded.

    (The dropped half of append()'s refusal is bypass's job, covered
    elsewhere; this is specifically about closed.)
    """
    playout = Playout(Direction.IN, FakeAudioSink())
    item = playout.begin(_unit(), "hi")
    playout.append(item, b"\x01\x02" * 100)
    playout.finish(item)

    before = bytes(item.pcm)
    assert playout.append(item, b"\x03\x04" * 100) is False
    assert bytes(item.pcm) == before


def test_an_utterance_that_never_produces_audio_reports_nothing():
    """Empty and open, it never clears the start threshold, so this tick
    does not promote it to _current - it stays queued. finish() then
    unqueues it directly (it has no pcm and is not _current), so there is
    nothing left for a second tick to retire; either way, nothing was ever
    read from it, so on_spoken must not fire."""
    spoken = []
    playout = Playout(Direction.IN, FakeAudioSink(), on_spoken=spoken.append)
    item = playout.begin(_unit(), "hi")

    assert playout.tick() is False  # nothing startable yet; still empty
    playout.finish(item, truncated=True)
    assert playout.tick() is False  # nothing left to retire; still nothing to report
    assert spoken == []


def test_a_truncated_utterance_with_no_audio_is_unqueued():
    """Synthesis failed before producing anything. tick() must not have to
    step over an empty entry that will never grow."""
    playout = Playout(Direction.IN, FakeAudioSink())
    item = playout.begin(_unit(), "hi")
    playout.finish(item, truncated=True)

    assert playout.flush() == 0


def test_finish_with_no_audio_and_no_truncation_still_unqueues():
    """A synthesis that simply produced nothing is not a failure, but there
    is still no reason to leave a permanently-empty entry in the queue."""
    playout = Playout(Direction.IN, FakeAudioSink())
    item = playout.begin(_unit(), "hi")
    playout.finish(item)  # truncated defaults to False

    assert playout.flush() == 0


def test_finish_on_an_already_dropped_item_does_not_raise():
    """The producer may call finish() for cleanup after append() told it to
    stop; by then the item may already be gone from the queue entirely -
    flush() (bypass) can empty it out from under a synthesis in progress."""
    playout = Playout(Direction.IN, FakeAudioSink())
    item = playout.begin(_unit(), "hi")
    playout.flush()

    playout.finish(item, truncated=True)  # must not raise
    assert playout.backlog_s() == 0.0


def test_an_utterance_waits_for_the_start_threshold():
    playout = Playout(Direction.IN, FakeAudioSink())
    item = playout.begin(_unit(), "hi")

    # 200 ms is under the 400 ms threshold: nothing plays yet.
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.2 / 2))
    assert playout.tick() is False

    # 400 ms total clears it.
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.2 / 2))
    assert playout.tick() is True


def test_a_short_utterance_plays_as_soon_as_it_is_closed():
    playout = Playout(Direction.IN, FakeAudioSink())
    item = playout.begin(_unit(), "hi")
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.05 / 2))

    # 50 ms is far under the threshold, but the utterance is complete, so
    # waiting for more would mean waiting forever.
    assert playout.tick() is False
    playout.finish(item)
    assert playout.tick() is True


def test_a_starved_utterance_holds_the_duck_closed():
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    item = playout.begin(_unit(), "hi")
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.4 / 2))
    for _ in range(20):  # play all 400 ms
        playout.tick()
    assert duck.is_open is False

    # Synthesis stalls. The duck must NOT flap open mid-sentence.
    for _ in range(10):
        assert playout.tick() is False
    assert duck.is_open is False

    # More audio arrives and playback resumes where it left off.
    playout.append(item, b"\x03\x04" * int(TTS_BYTES_PER_S * 0.1 / 2))
    assert playout.tick() is True


def test_a_long_stall_abandons_the_utterance_and_reopens_the_duck():
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    spoken = []
    playout = Playout(
        Direction.IN, FakeAudioSink(), duck=duck, on_spoken=spoken.append
    )

    item = playout.begin(_unit(), "hi")
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.4 / 2))
    for _ in range(20):
        playout.tick()
    assert duck.is_open is False

    # Exactly STARVE_LIMIT_TICKS: the abandonment happens on this tick, not
    # some tick after it - a bound off by one either way is a real bug here.
    for _ in range(STARVE_LIMIT_TICKS):
        playout.tick()

    assert duck.is_open is True
    assert item.truncated is True
    assert len(spoken) == 1  # what did play is still reported


def test_a_producer_that_dies_under_the_threshold_does_not_block_the_queue():
    sink = FakeAudioSink()
    playout = Playout(Direction.IN, sink)
    stalled = playout.begin(_unit(), "stalled")
    stalled_pcm = b"\x01\x02" * int(TTS_BYTES_PER_S * 0.1 / 2)
    playout.append(stalled, stalled_pcm)
    playout.submit(_translated(0.1, text="behind"))

    # Under the 400 ms threshold and never closed: nothing plays, and the
    # utterance queued behind it is stuck too.
    assert playout.tick() is False

    # Exactly STARVE_LIMIT_TICKS total (the one above plus these): the head
    # is closed in place on this tick, not some tick after it.
    for _ in range(STARVE_LIMIT_TICKS - 1):
        playout.tick()
    assert stalled.truncated is True
    assert stalled.closed is True

    # The fragment that did arrive is actually spoken - not discarded by a
    # mutant that closes the head and then pops it - and playback resumes
    # exactly where the fragment left off.
    before = len(sink.written)
    for _ in range(5):  # 0.1s / 20ms chunks
        assert playout.tick() is True
    assert sink.written[before:] == stalled_pcm

    # The queue moves again: the utterance queued behind it gets its turn.
    assert playout.tick() is True


def test_an_utterance_that_has_not_started_leaves_the_duck_open():
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    # Queued and under the threshold: nothing has been heard, so the original
    # must stay audible.
    item = playout.begin(_unit(), "hi")
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.1 / 2))
    for _ in range(10):
        playout.tick()
    assert duck.is_open is True


def test_the_starve_bound_is_consecutive_not_cumulative():
    """100 total starved ticks spread across many short hiccups must never
    truncate the utterance - only 100 in an unbroken row does. Deleting the
    counter reset on the successful-chunk path turns the bound cumulative;
    this is the test that would catch it."""
    playout = Playout(Direction.IN, FakeAudioSink())
    item = playout.begin(_unit(), "hi")
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.4 / 2))
    for _ in range(20):  # clear the start threshold
        playout.tick()

    one_chunk = b"\x01\x02" * (CHUNK_BYTES // 2)
    for _ in range(30):  # 30 x 10 = 300 starved ticks total, well over the bound
        for _ in range(10):
            assert playout.tick() is False  # starved, nowhere near the bound
        playout.append(item, one_chunk)
        assert playout.tick() is True  # delivery resumes; the run is broken

    assert item.truncated is False
    assert item.closed is False


def test_abandoning_does_not_carry_the_counter_to_the_next_head():
    """One dead producer must cost exactly one sentence, not two. The
    existing single-utterance abandon test can't see this: it needs a
    second, healthy utterance queued behind the one that stalls."""
    playout = Playout(Direction.IN, FakeAudioSink())
    stalling = playout.begin(_unit(), "stalling")
    playout.append(stalling, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.4 / 2))
    for _ in range(20):
        playout.tick()  # clears the threshold; stalling becomes _current

    # Begun moments later, with its own live producer - just not enough
    # audio yet to clear the start threshold on its own.
    healthy = playout.begin(_unit(), "healthy")
    playout.append(healthy, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.1 / 2))

    for _ in range(STARVE_LIMIT_TICKS):
        playout.tick()
    assert stalling.truncated is True  # abandoned on this exact tick

    # `healthy` gets its own full STARVE_LIMIT_TICKS budget rather than
    # inheriting the counter that just abandoned `stalling`.
    for _ in range(STARVE_LIMIT_TICKS - 1):
        playout.tick()
    assert healthy.truncated is False
    assert healthy.closed is False


def test_finish_does_not_downgrade_a_truncation_playout_already_recorded():
    """playout can truncate an utterance itself (the starvation bound)
    before the producer's own generator exhausts normally and calls
    finish(truncated=False). That later call must not erase the fact that
    the listener heard a cut-off sentence."""
    playout = Playout(Direction.IN, FakeAudioSink())
    item = playout.begin(_unit(), "hi")
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.4 / 2))
    for _ in range(20):
        playout.tick()

    for _ in range(STARVE_LIMIT_TICKS):
        playout.tick()
    assert item.truncated is True  # abandoned by the bound

    playout.finish(item)  # producer's generator exhausts normally, after the fact
    assert item.truncated is True  # still true - not overwritten by the default


def test_finish_without_truncation_leaves_the_utterance_unmarked():
    """The sibling of the sticky-truncated test above: nothing pins the
    default. Mutating `item.truncated = item.truncated or truncated` to an
    unconditional `True` would mark every normally-completed utterance
    truncated, and no existing assertion catches it - the suite's other two
    `truncated is False` checks are both on items that were never
    finish()ed at all."""
    playout = Playout(Direction.IN, FakeAudioSink())
    item = playout.begin(_unit(), "hi")
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.4 / 2))
    playout.finish(item)
    assert item.truncated is False


def test_retiring_a_starved_utterance_does_not_carry_the_counter_to_the_next_head():
    """Same failure shape as the abandon-path counter leak, reached by a
    different route: the producer itself calls finish() while _current is
    starved but before the bound fires, so the next tick takes the retire
    branch (`elif self._current.closed`) instead of the abandon branch.
    Without that branch's own reset, N leftover ticks would cost the next
    queued utterance N of its own 100-tick budget - illustrated here with
    N=90, a 1.8s stall followed by the producer erroring out."""
    playout = Playout(Direction.IN, FakeAudioSink())
    stalling = playout.begin(_unit(), "stalling")
    playout.append(stalling, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.4 / 2))
    for _ in range(20):
        playout.tick()  # clears the threshold; stalling becomes _current

    for _ in range(90):
        assert playout.tick() is False  # starved, well under the bound

    playout.finish(stalling, truncated=True)  # the producer itself gives up
    assert playout.tick() is False  # retires here - the retire branch, not abandon

    # Begun after the retirement, with its own live producer - just not
    # enough audio yet to clear the start threshold on its own.
    healthy = playout.begin(_unit(), "healthy")
    playout.append(healthy, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.1 / 2))

    # `healthy` gets its own full budget rather than inheriting the 90
    # leftover ticks from `stalling`'s retirement.
    for _ in range(STARVE_LIMIT_TICKS - 1):
        playout.tick()
    assert healthy.truncated is False
    assert healthy.closed is False


def test_an_emptied_queue_does_not_carry_the_counter_to_the_next_head():
    """finish() unqueues a never-started, empty-audio head immediately, with
    no length guard - so the queue can empty out from under the un-started-
    head branch while it is mid-count. Only the idle branch's own reset
    clears that count before the next utterance begins."""
    playout = Playout(Direction.IN, FakeAudioSink())
    empty = playout.begin(_unit(), "empty")  # no append: audio_s stays 0

    for _ in range(10):
        assert playout.tick() is False  # under threshold, counted in case 2

    playout.finish(empty)  # no audio ever arrived: unqueued immediately
    assert playout.tick() is False  # queue and _current both empty: idle path

    healthy = playout.begin(_unit(), "healthy")
    playout.append(healthy, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.1 / 2))

    # `healthy` gets its own full budget rather than inheriting the 10
    # leftover ticks from the cleared head.
    for _ in range(STARVE_LIMIT_TICKS - 1):
        playout.tick()
    assert healthy.truncated is False
    assert healthy.closed is False


def test_the_cap_does_not_drop_an_utterance_that_is_still_arriving():
    dropped = []
    playout = Playout(
        Direction.IN, FakeAudioSink(), lag_cap_s=1.0, on_dropped=dropped.append
    )
    playout.submit(_translated(2.0))     # becomes _current
    playout.tick()

    item = playout.begin(_unit(), "new")
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 3.0 / 2))

    # Well over the 1 s cap, but the only queued item is still open.
    assert playout.backlog_s() > 1.0
    assert dropped == []
    assert item.dropped is False

    # Once closed it becomes droppable like anything else. Assert on the item
    # itself, not only on len(dropped): closing it lets the cap drain the
    # whole queue in one pass, so the count below is 2, not 1 - _trim_locked
    # must keep going after one victim, since it runs only from
    # begin()/append()/submit(), never from tick(), so an under-draining
    # trim would leave the backlog over cap indefinitely once the producer
    # goes quiet.
    playout.finish(item)
    playout.submit(_translated(0.1))
    assert item.dropped is True
    assert len(dropped) == 2


def test_the_cap_exempts_the_head_not_merely_some_queued_utterance():
    """The guard exempts the queue HEAD specifically.

    No production caller can build this shape today, because submit() closes
    an Utterance before queueing it - so nothing open is ever queued at all.
    Once the pipeline streams, an open utterance exists and stays at the
    tail only as long as one begin->finish runs at a time per direction.
    This constructs the inverted shape on purpose: if that ever slips, the
    cap must still spare the open item. Reading the tail instead pops the
    wrong utterance entirely.
    """
    dropped = []
    playout = Playout(
        Direction.IN, FakeAudioSink(), lag_cap_s=1.0, on_dropped=dropped.append
    )
    playout.submit(_translated(2.0))
    playout.tick()

    open_head = playout.begin(_unit(), "arriving")
    playout.append(open_head, b"\x01\x02" * int(TTS_BYTES_PER_S * 2.0 / 2))
    playout.submit(_translated(0.5, text="behind"))

    assert open_head.dropped is False
    assert dropped == []


def test_a_backlog_exactly_at_the_cap_drops_nothing():
    """The comparison is strictly greater-than: sitting exactly at the cap
    is not yet over it."""
    dropped = []
    playout = Playout(
        Direction.IN, FakeAudioSink(), lag_cap_s=2.0, on_dropped=dropped.append
    )
    playout.submit(_translated(1.0, "first"))
    playout.submit(_translated(1.0, "second"))  # backlog == 2.0s, exactly the cap
    assert playout.backlog_s() == 2.0
    assert dropped == []


def test_flushing_tells_the_producer_to_stop_synthesising():
    playout = Playout(Direction.IN, FakeAudioSink())
    item = playout.begin(_unit(), "hi")
    assert playout.append(item, b"\x01\x02" * 100) is True

    playout.set_suppressed(True)
    assert playout.append(item, b"\x01\x02" * 100) is False


def test_on_dropped_runs_with_the_lock_released():
    """_trim_locked used to invoke on_dropped itself, while holding
    Playout._lock - so a callback that does real work (Session's transcript
    write: json.dumps, a file write, a flush) ran with that lock held. If a
    write ever blocked, tick() would stall waiting for the same lock, and
    tick() sets the duck's state BEFORE writing to the sink - so the duck
    would freeze wherever it last was, closed if speech was playing. That is
    the unbounded stuck-closed failure CLAUDE.md calls silently cruel.

    threading.Lock is not reentrant: if on_dropped still ran with the lock
    held, re-acquiring that same lock from inside the callback - on this same
    thread, which is already holding it - could never succeed, since nothing
    else will ever release it. Bounded with a short timeout rather than a
    bare acquire() so a regression times out and fails this one test instead
    of hanging the whole suite.
    """
    results = []

    def check_lock_is_free(item):
        acquired = playout._lock.acquire(timeout=0.2)
        if acquired:
            playout._lock.release()
        results.append(acquired)

    playout = Playout(
        Direction.IN, FakeAudioSink(), lag_cap_s=5.0, on_dropped=check_lock_is_free
    )
    playout.submit(_translated(2.0, "first"))
    playout.submit(_translated(2.0, "second"))
    playout.submit(_translated(2.0, "third"))  # over the cap; drops "first"

    assert results == [True]


def test_the_reported_backlog_reflects_what_triggered_the_drop(caplog):
    """_trim_locked used to read the backlog with self._backlog_locked()
    AFTER popleft() had already removed the victim, so the number in a
    "dropped an utterance" warning excluded the utterance it had just
    dropped - a message whose entire point is explaining why something went,
    printing a figure that no longer looks over the cap at all.

    Concretely: cap 2.0s, two 2.0s utterances submitted back to back. The
    drop happens because the backlog reached 4.0s; logging it after the pop
    would report 2.0s instead - at the cap, not over it.
    """
    playout = Playout(Direction.IN, FakeAudioSink(), lag_cap_s=2.0)
    with caplog.at_level("WARNING"):
        playout.submit(_translated(2.0, "first"))
        playout.submit(_translated(2.0, "second"))  # 4.0s total, over the 2.0s cap

    behind = [r.getMessage() for r in caplog.records if "behind" in r.getMessage()]
    assert behind == ["in playout 4.0s behind; dropped an utterance (1 total)"]


def test_a_dropped_streamed_utterance_reports_exactly_the_pcm_that_had_arrived():
    """Pins the deferred-snapshot guarantee for the streaming path this
    branch added: _trim_locked() only ever pops from _queue, never from
    _current, so a victim is out of the queue and marked dropped() before the
    lock is released - and append() refuses to extend a dropped item's pcm -
    so nothing can grow victim.pcm between the pop and the snapshot taken
    after the lock is released. The reported audio must be exactly what had
    arrived by drop time: not less (a snapshot taken too early) and not more
    (one racing a producer that kept extending it).
    """
    dropped = []
    playout = Playout(
        Direction.IN, FakeAudioSink(), lag_cap_s=2.0, on_dropped=dropped.append
    )
    victim = playout.begin(_unit(), "victim")
    chunk = b"\x01\x02" * int(TTS_BYTES_PER_S * 1.5 / 2)  # 1.5s
    playout.append(victim, chunk)
    playout.finish(victim)  # closed, but alone in the queue: not yet trimmable

    playout.submit(_translated(1.0, "second"))  # 2.5s total, over the 2.0s cap

    assert [d.text for d in dropped] == ["victim"]
    assert dropped[0].pcm == chunk
    assert playout.backlog_s() == 1.0


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


def test_an_expired_hold_does_not_re_arm_itself(caplog):
    """The bound has to disarm the hold, not merely pause it.

    Leaving _continuation set where the bound fires re-arms the hold on the
    very next tick: the duck opens for one 20 ms tick, shuts for another two
    seconds, and repeats for the rest of the call. That is the stuck-closed
    failure with a sawtooth on it, and the exactly-at-the-bound test above
    passes straight through it - it only looks at the one tick where the duck
    does open.
    """
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    playout.submit(_translated(0.04))
    playout.expect_continuation(True)
    playout.tick()
    playout.tick()
    with caplog.at_level(logging.WARNING, logger="sidetap.playout"):
        for _ in range(STARVE_LIMIT_TICKS):
            playout.tick()
        assert duck.is_open is True

        for _ in range(STARVE_LIMIT_TICKS * 2):
            playout.tick()
            assert duck.is_open is True

    # Disarming is also what stops the warning repeating. Left armed, the
    # deadline is re-reached on every tick from here to hangup: 50 lines a
    # second into the journal and the TUI, burying whatever else went wrong.
    assert len([r for r in caplog.records if "held the duck" in r.getMessage()]) == 1


def test_a_hold_does_not_spend_the_next_clause_s_start_budget():
    """Two different failures must not share one deadline.

    A hold that has already run most of its bound used to leave a freshly
    queued clause only the remainder before force-closing it as truncated -
    losing the rest of a clause that was arriving perfectly normally.
    """
    playout = Playout(Direction.IN, FakeAudioSink())
    playout.submit(_translated(0.04))
    playout.expect_continuation(True)
    playout.tick()
    playout.tick()

    for _ in range(STARVE_LIMIT_TICKS - 10):  # 1.8s of gap, most of the bound
        assert playout.tick() is False

    clause = playout.begin(_unit(), "next clause")
    playout.append(clause, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.1 / 2))

    # Its own full budget, counted from where it was queued rather than from
    # where the gap left off: one tick short of the bound it is still intact.
    for _ in range(STARVE_LIMIT_TICKS - 1):
        assert playout.tick() is False
    assert clause.closed is False
    assert clause.truncated is False

    # Which is what the budget is for: the producer is still allowed to
    # deliver the rest of it. A force-closed head refuses every later append.
    rest = b"\x01\x02" * int(TTS_BYTES_PER_S * 0.4 / 2)
    assert playout.append(clause, rest) is True
    assert playout.tick() is True


def test_speech_clears_the_hold_deadline():
    """A chunk reaching the sink is what resets `_hold_ticks` - nothing else.

    The old name said the re-arm did it. It does not, and has not since the
    counter split: `expect_continuation` only arms, and clearing there made
    the deadline a lease a caller could renew forever. What clears the
    counter here is the two ticks of the second clause's audio between the
    gaps, which is why the second gap gets a full bound rather than the ten
    ticks the first one left.

    What that pins is the per-gap budget. Without it the bound is cumulative
    across a monologue: after a couple of ordinary gaps it expires part-way
    through the next one and the duck opens mid-sentence anyway - the exact
    burst of untranslated original the hold exists to prevent.

    It does NOT pin the absence of the lease, and never did: restoring
    reset-on-arm passes this test unchanged, because the counter is zero
    either way by the time the second gap starts.
    test_a_producer_cannot_renew_the_hold_forever below is the one that
    catches that, verified by mutation.
    """
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    playout.submit(_translated(0.04))
    playout.expect_continuation(True)
    playout.tick()
    playout.tick()
    for _ in range(STARVE_LIMIT_TICKS - 10):  # most of the first gap's budget
        playout.tick()
    assert duck.is_open is False

    # The next clause lands and the producer re-arms for the gap behind it.
    playout.submit(_translated(0.04))
    playout.expect_continuation(True)
    assert playout.tick() is True
    assert playout.tick() is True

    # A full bound of gap, not the 10 ticks left over from the first one.
    for _ in range(STARVE_LIMIT_TICKS - 1):
        assert playout.tick() is False
    assert duck.is_open is False


def test_a_producer_cannot_renew_the_hold_forever():
    """The bound is a deadline, not a lease.

    Re-arming must not buy another two seconds when no audio has arrived in
    between - otherwise a caller that arms per clause holds the duck shut for
    the rest of the call, and the remote party is silenced with nothing on
    screen saying why.
    """
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    playout.expect_continuation(True)
    for tick in range(5000):
        if tick % 50 == 0:
            playout.expect_continuation(True)  # a caller arming per clause
        playout.tick()
        if duck.is_open:
            break
    assert duck.is_open is True
    assert tick <= STARVE_LIMIT_TICKS

    # And it stays open: a re-arm after the deadline has passed buys at most
    # the one tick it takes to expire again, not another full bound.
    open_ticks = 0
    for tick in range(5000):
        if tick % 50 == 0:
            playout.expect_continuation(True)
        playout.tick()
        open_ticks += duck.is_open
    assert open_ticks >= 4900


def test_a_queue_that_never_becomes_playable_does_not_suspend_the_bound():
    """A queued clause is not audio. Task 8 queues one before its audio exists,
    so an unstartable head is the ordinary shape, and the bound has to count
    those ticks or a recurring TTS stall silences the call.
    """
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    playout.expect_continuation(True)  # armed exactly once
    for tick in range(5000):
        if tick % 90 == 0:
            # A clause whose synthesis stalls under START_BUFFER_S, replaced
            # every 90 ticks - never playable, never speech.
            item = playout.begin(_unit(), "stalling")
            playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.1 / 2))
        playout.tick()
        if duck.is_open:
            break
    assert duck.is_open is True
    assert tick <= STARVE_LIMIT_TICKS


def test_a_flush_gives_the_next_run_a_full_hold():
    """Only real speech and a flush clear the deadline - so a flush must.

    Bypass and the drop-backlog hotkey both funnel through flush(), and the
    run after one starts from nothing. Carrying the spent deadline across it
    would expire the very next hold within a tick or two, and the duck would
    open mid-sentence through the first clause after every bypass.
    """
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    playout.expect_continuation(True)
    for _ in range(STARVE_LIMIT_TICKS - 10):  # spend most of the deadline
        playout.tick()
    assert duck.is_open is False

    playout.flush()
    playout.expect_continuation(True)

    # A full deadline again, with no speech in between to have cleared it.
    for _ in range(STARVE_LIMIT_TICKS - 10):
        playout.tick()
    assert duck.is_open is False
