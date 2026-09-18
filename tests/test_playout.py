from sidetap.playout import CHUNK_MS, DuckControl, Playout
from sidetap.types import TTS_BYTES_PER_S, Direction, Translated, Unit
from tests.conftest import FakeAudioSink, FakeVolumeControl

CHUNK_BYTES = TTS_BYTES_PER_S * CHUNK_MS // 1000


def _translated(seconds: float, text: str = "hi") -> Translated:
    unit = Unit(direction=Direction.IN, text=text, t_start=0.0, t_end=seconds)
    return Translated(unit=unit, text=text, pcm=b"\x01\x02" * int(TTS_BYTES_PER_S * seconds / 2))


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
