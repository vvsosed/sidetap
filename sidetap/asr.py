"""Google Cloud Speech-to-Text v2 (chirp_3) streaming recognition."""

from __future__ import annotations

import logging
import os
import queue as queue_module
import threading
from dataclasses import dataclass
from typing import Callable, Iterator, Protocol, runtime_checkable

from google.api_core import exceptions as gexc

from .ports import Clock, Recognizer
from .rotation import MAX_STREAM_SECONDS, AudioTimeline, StreamClock
from .types import BLOCK_BYTES, BLOCK_MS, TARGET_RATE, AsrResult, Direction
from .vad import SilenceGate

log = logging.getLogger(__name__)

BACKOFF_START_S = 2.0
BACKOFF_CAP_S = 30.0
ESCALATE_AFTER_FAILURES = 5

# Healthy streams rotate at MAX_STREAM_SECONDS. A stream still open well past
# that is stalled - gRPC sets no deadline and no keepalive, so a black-holed
# connection would otherwise leave this direction silently recognising nothing
# for the rest of the call. DeadlineExceeded is not fatal, so the worker
# reconnects.
STREAM_TIMEOUT_S = MAX_STREAM_SECONDS + 30

# Google ends a stream it stops receiving requests on, with a 409 "Stream timed
# out after receiving no more client requests". Two different things stop the
# requests, and both were seen live in meetscribe:
#
#   - the gate drops a quiet stretch, so nothing is worth sending; and
#   - blocks stop arriving at all, because the tapped application's node went
#     away. A finished call or a closed tab unlinks the capture node, and
#     PipeWire does not drive a stream with no input - pw-record then emits
#     nothing whatsoever, not silence.
#
# So the keepalive sits in the worker, where the audio actually stops, rather
# than in SilenceGate, which only ever sees blocks that did arrive.
KEEPALIVE_S = 2.0
SILENCE_BLOCK = b"\x00" * BLOCK_BYTES

# Retrying any of these is pointless: the configuration or the credentials are
# wrong and will stay wrong.
FATAL_ERRORS = (
    gexc.Unauthenticated,
    gexc.PermissionDenied,
    gexc.InvalidArgument,
    gexc.NotFound,
)


def duration_seconds(value) -> float:
    """protobuf Duration or timedelta -> float seconds. Tolerates None."""
    if value is None:
        return 0.0
    total_seconds = getattr(value, "total_seconds", None)
    return float(total_seconds()) if callable(total_seconds) else 0.0


@runtime_checkable
class SessionTime(Protocol):
    """Maps a stream-relative position onto the session timeline.

    Both StreamClock and AudioTimeline satisfy this, and which one arrives
    here is NOT interchangeable:

      StreamClock.absolute()   adds a flat offset.
      AudioTimeline.absolute() indexes into the recorded capture time of each
                               block actually sent.

    Chirp 3's result_end_offset is a position in the audio we SENT, and the
    silence gate drops blocks before sending - so in any real conversation
    that position is not elapsed time. Only AudioTimeline compensates for the
    gap. Passing a StreamClock here would understate every timestamp by
    however much silence was gated, quietly corrupting the transcript's
    latency column, which is the stated evidence for the later
    LocalAgreement-2 decision.

    This Protocol exists so the annotation states that contract rather than
    naming one concrete class and inviting a "type cleanup" that swaps in the
    wrong one.
    """

    def absolute(self, seconds: float) -> float: ...


def result_from_response(response_result, clock: SessionTime, direction: Direction) -> AsrResult | None:
    """Map one recognition result onto the session timeline.

    Returns None for results with nothing usable in them.
    """
    alternatives = getattr(response_result, "alternatives", None) or []
    if not alternatives:
        return None

    alternative = alternatives[0]
    text = (alternative.transcript or "").strip()
    if not text:
        return None

    # Direct attribute access, not getattr with a default: on a real protobuf
    # these fields are always present, so a default could only ever mask an
    # upstream rename - turning a loud AttributeError into 0.0 timestamps
    # written straight to the durable JSONL.
    end = clock.absolute(duration_seconds(response_result.result_end_offset))

    return AsrResult(
        direction=direction,
        text=text,
        is_final=bool(response_result.is_final),
        # Chirp 3 gives no word timings in streaming mode, so the utterance's
        # end offset is the only timestamp there is.
        t_start=end,
        t_end=end,
        confidence=alternative.confidence or None,
    )


def is_fatal(exc: BaseException) -> bool:
    return isinstance(exc, FATAL_ERRORS)


def next_backoff(previous: float) -> float:
    return min(previous * 2, BACKOFF_CAP_S)


def speech_endpoint(region: str) -> str:
    if region == "global":
        return "speech.googleapis.com"
    return f"{region}-speech.googleapis.com"


def recognizer_path(project_id: str, region: str) -> str:
    return f"projects/{project_id}/locations/{region}/recognizers/_"


def project_from_environment() -> str:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError(
            "No GCP project. Pass --project or set GOOGLE_CLOUD_PROJECT, and "
            "point GOOGLE_APPLICATION_CREDENTIALS at a service account key."
        )
    return project


RecognizerFactory = Callable[[AudioTimeline], Recognizer]
