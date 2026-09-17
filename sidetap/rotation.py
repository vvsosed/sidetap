"""Offset bookkeeping across rotated recognition streams.

A single Google StreamingRecognize call is closed by the server at five
minutes. We tear ours down at four and open a fresh one. Each new stream
reports timestamps relative to itself, so we carry an offset forward to keep
the transcript on one continuous timeline.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .types import BLOCK_MS

MAX_STREAM_SECONDS = 240.0
BLOCK_S = BLOCK_MS / 1000


@dataclass(frozen=True)
class StreamClock:
    offset: float = 0.0
    max_stream_s: float = MAX_STREAM_SECONDS

    def should_rotate(self, stream_age_s: float) -> bool:
        return stream_age_s >= self.max_stream_s

    def absolute(self, stream_relative_s: float) -> float:
        """Map a time reported by the current stream onto the session timeline."""
        return self.offset + stream_relative_s

    def rotated(self, last_chunk_t: float) -> StreamClock:
        """Clock for the next stream. max() guards against rewinding.

        `last_chunk_t` must already be on the session-absolute timeline — a
        raw `AudioChunk.t_start`, not a time reported by the closing stream.
        Do not pass it through `absolute()` first: that double-applies the
        offset and compounds on every rotation.
        """
        return replace(self, offset=max(self.offset, last_chunk_t))


class AudioTimeline:
    """Maps a position in the audio we sent back to when it was captured.

    The silence gate drops blocks, so an engine's result offsets count the
    audio it received, not elapsed time. Adding a real-time offset to an
    audio-relative position stamps the transcript early by however much
    silence was dropped - and the two tracks drop different amounts, so they
    drift apart from each other as well, which scrambles the order of the
    saved Markdown.

    One entry per block sent, so a four-minute stream holds at most ~2400
    floats and is discarded at each rotation.
    """

    def __init__(self, offset: float = 0.0):
        self.offset = offset
        self._real_at: list[float] = []

    def sent(self, real_t: float) -> None:
        """Record that the next block of audio was captured at `real_t`."""
        self._real_at.append(real_t)

    def absolute(self, audio_seconds: float) -> float:
        """Real time for a position in the audio we sent."""
        if not self._real_at:
            # Nothing sent yet - a stream that failed on connect. Fall back to
            # the plain offset so behaviour matches StreamClock.
            return self.offset + audio_seconds
        # Float division can land a whole multiple one index low; being out by
        # one block is 100 ms, against the hundreds of seconds this prevents.
        index = int(audio_seconds / BLOCK_S)
        index = max(0, min(index, len(self._real_at) - 1))
        return self._real_at[index] + (audio_seconds - index * BLOCK_S)
