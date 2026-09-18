import pytest
from google.api_core import exceptions as gexc

from sidetap.asr import (
    duration_seconds,
    is_fatal,
    next_backoff,
    recognizer_path,
    result_from_response,
    speech_endpoint,
)
from sidetap.rotation import StreamClock
from sidetap.types import Direction


class FakeAlternative:
    def __init__(self, transcript, confidence=0.9):
        self.transcript = transcript
        self.confidence = confidence


class FakeResult:
    def __init__(self, transcript="hello", is_final=True, end=3.0, confidence=0.9):
        self.alternatives = [FakeAlternative(transcript, confidence)] if transcript is not None else []
        self.is_final = is_final
        self.result_end_offset = _Duration(end)


class _Duration:
    def __init__(self, seconds):
        self._seconds = seconds

    def total_seconds(self):
        return self._seconds


def test_duration_seconds_tolerates_none():
    assert duration_seconds(None) == 0.0


def test_duration_seconds_reads_a_protobuf_duration():
    assert duration_seconds(_Duration(2.5)) == 2.5


def test_result_maps_onto_the_session_timeline():
    clock = StreamClock(offset=100.0)
    result = result_from_response(FakeResult(end=3.0), clock, Direction.IN)
    assert result is not None
    assert result.t_end == 103.0
    assert result.direction is Direction.IN


def test_result_without_word_timings_stamps_start_at_the_end_offset():
    # Chirp 3 gives no word timings in streaming mode, so there is nothing
    # better to stamp the start with.
    clock = StreamClock(offset=0.0)
    result = result_from_response(FakeResult(end=3.0), clock, Direction.OUT)
    assert result.t_start == result.t_end == 3.0


def test_result_with_no_alternatives_is_dropped():
    assert result_from_response(FakeResult(transcript=None), StreamClock(), Direction.IN) is None


def test_result_with_blank_text_is_dropped():
    assert result_from_response(FakeResult(transcript="   "), StreamClock(), Direction.IN) is None


def test_result_strips_surrounding_whitespace():
    result = result_from_response(FakeResult(transcript="  hi  "), StreamClock(), Direction.IN)
    assert result.text == "hi"


def test_zero_confidence_becomes_none():
    # Chirp 3 reports 0.0 for interims; carrying that through would read as
    # "certainly wrong" rather than "not scored".
    result = result_from_response(
        FakeResult(confidence=0.0), StreamClock(), Direction.IN
    )
    assert result.confidence is None


@pytest.mark.parametrize(
    "exc",
    [
        gexc.Unauthenticated("no"),
        gexc.PermissionDenied("no"),
        gexc.InvalidArgument("no"),
        gexc.NotFound("no"),
    ],
)
def test_configuration_errors_are_fatal(exc):
    assert is_fatal(exc) is True


@pytest.mark.parametrize(
    "exc",
    [gexc.DeadlineExceeded("later"), gexc.ServiceUnavailable("later"), OSError("later")],
)
def test_transient_errors_are_retryable(exc):
    assert is_fatal(exc) is False


def test_backoff_doubles_up_to_the_cap():
    assert next_backoff(2.0) == 4.0
    assert next_backoff(20.0) == 30.0
    assert next_backoff(30.0) == 30.0


def test_endpoint_is_regional_except_for_global():
    assert speech_endpoint("europe-west3") == "europe-west3-speech.googleapis.com"
    assert speech_endpoint("global") == "speech.googleapis.com"


def test_recognizer_path_uses_the_wildcard_recognizer():
    assert (
        recognizer_path("proj", "europe-west3")
        == "projects/proj/locations/europe-west3/recognizers/_"
    )


import queue
import threading

from sidetap.asr import KEEPALIVE_S, SILENCE_BLOCK, RecognitionWorker
from sidetap.capture import DroppingQueue
from sidetap.rotation import AudioTimeline
from sidetap.types import BLOCK_BYTES, AudioChunk, MIC
from sidetap.vad import SilenceGate
from tests.conftest import FakeClock, FakeRecognizer

SPEECH = b"\x10\x00" * (BLOCK_BYTES // 2)


def _chunk(t: float) -> AudioChunk:
    return AudioChunk(track=MIC, pcm=SPEECH, t_start=t)


def _worker(recognizer, clock, *, max_stream_s=240.0, gate=None):
    return RecognitionWorker(
        direction=Direction.OUT,
        recognizer_factory=lambda timeline: recognizer,
        gate=gate or SilenceGate(None),
        clock=clock,
        max_stream_s=max_stream_s,
    )


def test_worker_forwards_results_to_the_output_queue():
    result = AsrResultFactory()
    recognizer = FakeRecognizer([result])
    audio_q = DroppingQueue()
    audio_q.put(_chunk(0.0))
    out_q: queue.Queue = queue.Queue()
    stop = threading.Event()

    clock = FakeClock()
    worker = _worker(recognizer, clock)
    thread = threading.Thread(target=worker.run, args=(audio_q, out_q, stop))
    thread.start()
    got = out_q.get(timeout=2)
    stop.set()
    thread.join(timeout=2)

    assert got is result


def AsrResultFactory():
    from sidetap.types import AsrResult

    return AsrResult(
        direction=Direction.OUT, text="hello", is_final=True, t_start=0.0, t_end=1.0
    )


def test_a_fatal_error_stops_this_direction_only():
    """The two directions carry different language codes.

    A config error in one says nothing about the other, and dropping a live
    call because the OTHER direction was misconfigured is worse than
    interpreting one way. The event passed in is per-direction; Session
    decides when enough of them are dead to give up.
    """
    fatal = []
    recognizer = FakeRecognizer([], error=gexc.InvalidArgument("bad config"))
    audio_q = DroppingQueue()
    audio_q.put(_chunk(0.0))
    out_q: queue.Queue = queue.Queue()
    stop = threading.Event()

    worker = RecognitionWorker(
        direction=Direction.OUT,
        recognizer_factory=lambda timeline: recognizer,
        gate=SilenceGate(None),
        clock=FakeClock(),
        on_fatal=lambda d, exc: fatal.append((d, exc)),
    )
    worker.run(audio_q, out_q, stop)

    # Not a retry loop: the config is wrong and will stay wrong.
    assert stop.is_set()
    # And the session is told WHICH direction died, so it can decide.
    assert [d for d, _ in fatal] == [Direction.OUT]


def test_a_transient_error_backs_off_and_retries():
    recognizer = FakeRecognizer([], error=gexc.ServiceUnavailable("later"))
    audio_q = DroppingQueue()
    for i in range(6):
        audio_q.put(_chunk(float(i)))
    out_q: queue.Queue = queue.Queue()
    stop = threading.Event()
    clock = FakeClock()

    def stop_soon():
        while len(clock.slept) < 3:
            pass
        stop.set()

    watcher = threading.Thread(target=stop_soon, daemon=True)
    watcher.start()
    _worker(recognizer, clock).run(audio_q, out_q, stop)
    watcher.join(timeout=2)

    # Exponential, and never escalated to a fatal stop by itself.
    assert clock.slept[:3] == [2.0, 4.0, 8.0]


def test_the_gate_suppresses_silence_but_the_keepalive_still_sends():
    """A stream sent nothing is killed by Google with a 409.

    The gate drops a quiet stretch, so the keepalive has to fire from the
    worker - the gate never sees the other cause, blocks not arriving at all.
    """
    silence = b"\x00" * BLOCK_BYTES
    recognizer = FakeRecognizer([])
    audio_q = DroppingQueue()
    audio_q.put(AudioChunk(track=MIC, pcm=silence, t_start=0.0))
    out_q: queue.Queue = queue.Queue()
    stop = threading.Event()
    clock = FakeClock()

    gate = SilenceGate(lambda pcm: False, tail_blocks=0)
    worker = _worker(recognizer, clock, gate=gate)
    thread = threading.Thread(target=worker.run, args=(audio_q, out_q, stop))
    thread.start()
    for _ in range(200):
        clock.advance(KEEPALIVE_S)
        if recognizer.sent:
            break
    stop.set()
    thread.join(timeout=2)

    assert SILENCE_BLOCK in recognizer.sent
