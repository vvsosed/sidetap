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
    silence into the middle of a word, and would also never let a future
    starvation counter increment (it only counts unread == 0), holding the
    duck closed with no bound while a trickling producer stalls.

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

    for _ in range(STARVE_LIMIT_TICKS + 1):
        playout.tick()

    assert duck.is_open is True
    assert item.truncated is True
    assert len(spoken) == 1  # what did play is still reported


def test_a_producer_that_dies_under_the_threshold_does_not_block_the_queue():
    playout = Playout(Direction.IN, FakeAudioSink())
    stalled = playout.begin(_unit(), "stalled")
    playout.append(stalled, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.1 / 2))
    playout.submit(_translated(0.1, text="behind"))

    # Under the 400 ms threshold and never closed: nothing plays, and the
    # utterance queued behind it is stuck too.
    assert playout.tick() is False

    for _ in range(STARVE_LIMIT_TICKS + 2):
        playout.tick()

    # The fragment that did arrive is spoken, and the queue moves again.
    assert stalled.truncated is True
    assert stalled.closed is True


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
