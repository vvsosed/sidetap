"""Value types shared by every layer. Imports nothing but the standard library."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# Capture side. pw-record resamples to this, so nothing here does conversion.
TARGET_RATE = 16_000
BLOCK_MS = 100
BLOCK_BYTES = TARGET_RATE * 2 * BLOCK_MS // 1000

# Playout side. Chirp 3 HD streaming synthesis returns LINEAR16 at this rate;
# pw-cat resamples it to whatever the sink wants.
TTS_RATE = 24_000
TTS_BYTES_PER_S = TTS_RATE * 2

# Capture track names. These are what the recorders and queues are keyed on.
REMOTE = "remote"
MIC = "mic"

# Seconds of un-spoken audio past which playout starts dropping the oldest.
#
# A dropped utterance is a sentence the user never hears, which is a harder
# failure than briefly trailing the conversation, so the cap errs toward
# holding audio rather than discarding it. It is a ceiling on transient
# spikes, not a cure for a backlog that grows: if the translated language
# runs longer than its source, only --speaking-rate-in can make it drain, and
# a higher cap merely postpones the first drop.
#
# Not measured against a real two-way call - at this setting a reply arrives
# up to 20 s after what it answers, which is past conversational. Lower it
# with --lag-cap when keeping pace matters more than hearing every sentence.
LAG_CAP_S = 20.0
# Seconds of continuous outbound speech with nothing reaching the virtual mic
# before the dead-air alarm fires.
DEAD_AIR_S = 6.0

# No audio at all reaching a capture queue for this long. Distinct from
# DEAD_AIR_S, which is about a finished utterance producing nothing: this is
# the upstream failure, where an unlinked capture node delivers ZERO BYTES
# rather than silence, so the silence gate sees nothing to gate and every
# downstream stage sits idle looking healthy.
NO_AUDIO_S = 15.0


class Direction(StrEnum):
    """Which way a translation flows.

    IN is them -> you, landing on your headphones.
    OUT is you -> them, landing in the virtual mic's sink.
    """

    IN = "in"
    OUT = "out"

    @property
    def track(self) -> str:
        """The capture track this direction consumes."""
        return REMOTE if self is Direction.IN else MIC

    @property
    def opposite(self) -> Direction:
        return Direction.OUT if self is Direction.IN else Direction.IN


@dataclass(frozen=True)
class AudioChunk:
    track: str
    pcm: bytes
    t_start: float


@dataclass(frozen=True)
class AsrResult:
    """One recognition result, interim or final, on the session timeline."""

    direction: Direction
    text: str
    is_final: bool
    t_start: float
    t_end: float
    confidence: float | None = None


@dataclass(frozen=True)
class Unit:
    """A translatable unit emitted by the segmenter.

    With FinalsOnlySegmenter this is one per final AsrResult. With a future
    LocalAgreementSegmenter it would be one per committed clause, which is the
    entire reason this type is distinct from AsrResult.
    """

    direction: Direction
    text: str
    t_start: float
    t_end: float

    @classmethod
    def from_result(cls, result: AsrResult) -> Unit:
        return cls(
            direction=result.direction,
            text=result.text,
            t_start=result.t_start,
            t_end=result.t_end,
        )


@dataclass(frozen=True)
class Translated:
    """A unit, its translation, and the synthesised audio for it."""

    unit: Unit
    text: str
    pcm: bytes = b""

    @property
    def direction(self) -> Direction:
        return self.unit.direction

    @property
    def audio_s(self) -> float:
        # Assumes pcm is a whole number of s16 frames; an odd byte count
        # would silently treat the stray trailing byte as half a sample.
        return len(self.pcm) / TTS_BYTES_PER_S


@dataclass(frozen=True)
class Latency:
    asr_ms: float = 0.0
    mt_ms: float = 0.0
    tts_ms: float = 0.0
    # Full synthesis wall time. Deliberately NOT part of total_ms: playout
    # starts on the first chunk, so what the listener waited for is tts_ms,
    # and adding the rest back would make the TUI overstate felt latency by
    # exactly the amount streaming saved. Kept because it is still the
    # throughput and cost signal.
    tts_total_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return self.asr_ms + self.mt_ms + self.tts_ms


@dataclass(frozen=True)
class Record:
    """One transcript row."""

    unit: Unit
    target_text: str
    latency: Latency = field(default_factory=Latency)
    dropped: bool = False
    truncated: bool = False

    @property
    def direction(self) -> Direction:
        return self.unit.direction
