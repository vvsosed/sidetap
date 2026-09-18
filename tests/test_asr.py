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
