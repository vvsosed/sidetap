"""Google Cloud Speech-to-Text v2 streaming recognition.

The model is `chirp_2` in `europe-west4`. Measured against the live API on
2026-09-18:

    chirp_3  any region      -> 403 "no longer generally available"
    chirp_2  europe-west3    -> 400 "does not exist in this location"
    chirp_2  europe-west4    -> works, and is the closest region that does
    long     europe-west3    -> works for en-US, but 400 for ru-RU

Frankfurt (europe-west3) is the tempting mistake: it is nearest and works for
English, then fails every other language with a 400 once audio is flowing.
TTS is unaffected; Chirp 3 HD voices remain available (tts.py).
"""

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

# Healthy streams rotate at MAX_STREAM_SECONDS, so one open well past that has
# stalled. gRPC sets no deadline or keepalive of its own, so a black-holed
# connection would leave this direction deaf for the rest of the call.
# DeadlineExceeded is not fatal, so the worker reconnects.
STREAM_TIMEOUT_S = MAX_STREAM_SECONDS + 30

# Google ends a stream that stops receiving requests (409 "Stream timed out").
# Requests stop either because the gate drops a quiet stretch, or because no
# blocks arrive at all: when the tapped application's node goes away,
# pw-record emits nothing, not silence. So the keepalive lives in the worker,
# where the audio actually stops, not in SilenceGate, which only sees blocks
# that arrived.
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

    StreamClock and AudioTimeline both satisfy this, and are NOT
    interchangeable:

      StreamClock.absolute()   adds a flat offset.
      AudioTimeline.absolute() indexes into the recorded capture time of each
                               block actually sent.

    Chirp's result_end_offset is a position in the audio SENT, and the
    silence gate drops blocks before sending, so it is not elapsed time. A
    StreamClock here would understate every timestamp by however much silence
    was gated. The Protocol states that contract so the annotation cannot be
    "cleaned up" to the wrong class.
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

    # Direct attribute access: these fields always exist on a real protobuf,
    # so a default could only turn an upstream rename into silent 0.0
    # timestamps.
    end = clock.absolute(duration_seconds(response_result.result_end_offset))

    return AsrResult(
        direction=direction,
        text=text,
        is_final=bool(response_result.is_final),
        # Chirp gives no word timings in streaming mode, so the utterance's
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
            "No GCP project. Pass --project or set GOOGLE_CLOUD_PROJECT. "
            "For credentials, either run `gcloud auth application-default "
            "login` or point GOOGLE_APPLICATION_CREDENTIALS at a service "
            "account key. The project is asked for separately on purpose: "
            "the one your ADC defaults to is whatever gcloud was last "
            "pointed at, which is routinely not the one with these APIs "
            "enabled."
        )
    return project


RecognizerFactory = Callable[[AudioTimeline], Recognizer]


class RecognitionWorker:
    """Drives one direction's audio through rotating recognition streams."""

    def __init__(
        self,
        direction: Direction,
        recognizer_factory: RecognizerFactory,
        gate: SilenceGate,
        clock: Clock,
        max_stream_s: float = MAX_STREAM_SECONDS,
        on_fatal: Callable[[Direction, BaseException], None] | None = None,
        on_audio_sent: Callable[[float], None] | None = None,
    ):
        self._on_fatal = on_fatal
        self._direction = direction
        self._factory = recognizer_factory
        self._gate = gate
        self._clock = clock
        self._max_stream_s = max_stream_s
        self._on_audio_sent = on_audio_sent

    @staticmethod
    def _dropped(audio_q) -> int:
        """Blocks the queue has discarded, when it counts them.

        A plain queue.Queue does not, so this reports 0 rather than failing.
        """
        return getattr(audio_q, "dropped", 0)

    def run(self, audio_q, out_q: queue_module.Queue, stop: threading.Event) -> None:
        stream_clock = StreamClock(max_stream_s=self._max_stream_s)
        backoff = BACKOFF_START_S
        consecutive_failures = 0
        dropped_at_success = self._dropped(audio_q)

        while not stop.is_set():
            last_chunk_t = stream_clock.offset
            started = self._clock.monotonic()
            # One timeline per stream: the engine numbers its results from the
            # start of the audio we send it, and the gate means that is not
            # elapsed time.
            timeline = AudioTimeline(stream_clock.offset)

            def blocks() -> Iterator[bytes]:
                nonlocal last_chunk_t
                last_chunk_at = started
                last_sent_at = started
                while not stop.is_set():
                    now = self._clock.monotonic()
                    if stream_clock.should_rotate(now - started):
                        log.debug("rotating %s stream", self._direction.value)
                        return
                    try:
                        chunk = audio_q.get(timeout=0.25)
                    except queue_module.Empty:
                        chunk = None
                    now = self._clock.monotonic()
                    if chunk is not None:
                        last_chunk_t = chunk.t_start
                        last_chunk_at = now
                    if chunk is not None and self._gate.allows(chunk.pcm):
                        last_sent_at = now
                        timeline.sent(chunk.t_start)
                        if self._on_audio_sent is not None:
                            self._on_audio_sent(BLOCK_MS / 1000)
                        yield chunk.pcm
                    elif now - last_sent_at >= KEEPALIVE_S:
                        # Nothing worth sending, or nothing arriving at all.
                        # Either way the stream dies unless we say something.
                        last_sent_at = now
                        timeline.sent(last_chunk_t + (now - last_chunk_at))
                        if self._on_audio_sent is not None:
                            # Keepalive silence is still billed audio.
                            self._on_audio_sent(BLOCK_MS / 1000)
                        yield SILENCE_BLOCK

            try:
                # No stop check here: blocks() ends the stream when stop is
                # set, and breaking out would discard the finals the engine
                # emits on the way out.
                for result in self._factory(timeline).stream(blocks()):
                    out_q.put(result)
                consecutive_failures = 0
                backoff = BACKOFF_START_S
                dropped_at_success = self._dropped(audio_q)
            except Exception as exc:
                if stop.is_set():
                    break
                if is_fatal(exc):
                    # Stops THIS direction only: the directions use different
                    # language codes, so a config error in one says nothing
                    # about the other, and interpreting one way beats dropping
                    # the call. Session decides when to give up.
                    log.error(
                        "speech configuration error (%s), not retryable — this "
                        "direction is now dead: %s",
                        self._direction.value,
                        exc,
                    )
                    stop.set()
                    if self._on_fatal is not None:
                        self._on_fatal(self._direction, exc)
                    return
                consecutive_failures += 1
                level = (
                    logging.ERROR
                    if consecutive_failures >= ESCALATE_AFTER_FAILURES
                    else logging.WARNING
                )
                lost_blocks = self._dropped(audio_q) - dropped_at_success
                lost = (
                    f"; {lost_blocks * BLOCK_MS / 1000:.0f}s of audio dropped "
                    "while offline"
                    if lost_blocks
                    else ""
                )
                log.log(
                    level,
                    "speech stream error (%s), retrying in %.0fs: %s%s",
                    self._direction.value,
                    backoff,
                    exc,
                    lost,
                )
                self._clock.sleep(backoff)
                backoff = next_backoff(backoff)

            stream_clock = stream_clock.rotated(last_chunk_t)


@dataclass(frozen=True)
class AsrConfig:
    project_id: str
    region: str = "europe-west4"
    model: str = "chirp_2"
    language_code: str = "en-US"
    phrases: tuple[str, ...] = ()
    interim: bool = True


class GoogleRecognizer:
    """One StreamingRecognize call. Discarded and rebuilt on every rotation."""

    def __init__(self, config: AsrConfig, client, timeline: AudioTimeline, direction: Direction):
        self._config = config
        self._client = client
        # An AudioTimeline, NOT a StreamClock; see SessionTime.
        self._clock = timeline
        self._direction = direction

    def _config_request(self):
        from google.cloud.speech_v2.types import cloud_speech as cs

        adaptation = None
        if self._config.phrases:
            adaptation = cs.SpeechAdaptation(
                phrase_sets=[
                    cs.SpeechAdaptation.AdaptationPhraseSet(
                        inline_phrase_set=cs.PhraseSet(
                            phrases=[
                                # Boost is 0-20, and high values degrade
                                # general accuracy; 15 suits names and jargon
                                # without that cost.
                                cs.PhraseSet.Phrase(value=p, boost=15.0)
                                for p in self._config.phrases
                            ]
                        )
                    )
                ]
            )

        recognition_config = cs.RecognitionConfig(
            explicit_decoding_config=cs.ExplicitDecodingConfig(
                encoding=cs.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=TARGET_RATE,
                audio_channel_count=1,
            ),
            # One code per direction: the channel fixes the source language,
            # so there is nothing to detect.
            language_codes=[self._config.language_code],
            model=self._config.model,
            # No enable_word_time_offsets: Chirp rejects it in streaming mode
            # with a fatal InvalidArgument.
            features=cs.RecognitionFeatures(enable_automatic_punctuation=True),
            **({"adaptation": adaptation} if adaptation else {}),
        )

        streaming_config = cs.StreamingRecognitionConfig(
            config=recognition_config,
            streaming_features=cs.StreamingRecognitionFeatures(
                interim_results=self._config.interim,
            ),
        )
        return cs.StreamingRecognizeRequest(
            recognizer=recognizer_path(self._config.project_id, self._config.region),
            streaming_config=streaming_config,
        )

    def stream(self, pcm: Iterator[bytes]) -> Iterator[AsrResult]:
        from google.cloud.speech_v2.types import cloud_speech as cs

        def requests():
            yield self._config_request()
            for block in pcm:
                yield cs.StreamingRecognizeRequest(audio=block)

        for response in self._client.streaming_recognize(
            requests=requests(), timeout=STREAM_TIMEOUT_S
        ):
            for response_result in response.results:
                result = result_from_response(response_result, self._clock, self._direction)
                if result is not None:
                    yield result


def build_recognizer_factory(config: AsrConfig, direction: Direction) -> RecognizerFactory:
    """Create the factory the RecognitionWorker calls on every rotation."""
    from google.api_core.client_options import ClientOptions
    from google.cloud.speech_v2 import SpeechClient

    client = SpeechClient(
        client_options=ClientOptions(api_endpoint=speech_endpoint(config.region))
    )

    def factory(timeline: AudioTimeline) -> Recognizer:
        return GoogleRecognizer(config, client, timeline, direction)

    return factory
