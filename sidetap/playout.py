"""Speak translated audio, duck the original, and bound the lag.

One long-lived pw-cat per direction, fed raw PCM. Between utterances this
writes silence rather than stopping - the mirror of the ASR keepalive. It
avoids underrun ambiguity and, more importantly, gives playout exact knowledge
of when it is emitting speech, which is what drives the duck.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Callable

from .ports import AudioSink, VolumeControl
from .types import LAG_CAP_S, TTS_BYTES_PER_S, Direction, Translated

log = logging.getLogger(__name__)

CHUNK_MS = 20
CHUNK_BYTES = TTS_BYTES_PER_S * CHUNK_MS // 1000
SILENCE_CHUNK = b"\x00" * CHUNK_BYTES


class DuckControl:
    """Silences the remote party's original while the translation speaks.

    Starts open and only calls the volume control on a transition, so a stream
    of 20 ms speech chunks produces one wpctl call, not fifty per second.
    """

    def __init__(self, volume: VolumeControl, object_id: int):
        self._volume = volume
        self._object_id = object_id
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._volume.set_volume(self._object_id, 0.0)
            self._closed = True

    def open(self) -> None:
        if self._closed:
            self._volume.set_volume(self._object_id, 1.0)
            self._closed = False


class Playout:
    def __init__(
        self,
        direction: Direction,
        sink: AudioSink,
        *,
        duck: DuckControl | None = None,
        lag_cap_s: float = LAG_CAP_S,
        on_spoken: Callable[[Translated], None] | None = None,
        on_dropped: Callable[[Translated], None] | None = None,
    ):
        self.direction = direction
        self.dropped = 0
        self._sink = sink
        self._duck = duck
        self._lag_cap_s = lag_cap_s
        self._on_spoken = on_spoken
        self._on_dropped = on_dropped
        self._lock = threading.Lock()
        self._queue: deque[Translated] = deque()
        self._current: Translated | None = None
        self._pending = b""

    def submit(self, item: Translated) -> None:
        with self._lock:
            self._queue.append(item)
            self._trim_locked()

    def backlog_s(self) -> float:
        with self._lock:
            return self._backlog_locked()

    def flush(self) -> int:
        """Drop everything not yet handed to the sink. Returns how many
        whole items went.

        The 20 ms chunk already passed to sink.write() cannot be recalled -
        pw-cat has it, and by the time flush() runs it is no longer part of
        this object's state at all (self._pending has already been advanced
        past it). Everything still inside Playout - the queue, and whatever
        of the in-progress item has not yet reached the sink - has NOT been
        handed off and is dropped here. Only whole, not-yet-started items are
        counted as dropped: the in-progress item is cut short, not "dropped",
        since part of it was already spoken. Counting the in-flight chunk (or
        the rest of that same utterance) as dropped would make the
        drop-backlog hotkey lie about what the listener will hear.
        """
        with self._lock:
            count = len(self._queue)
            self._queue.clear()
            self._current = None
            self._pending = b""
            return count

    def _trim_locked(self) -> None:
        # A single item longer than the cap is kept: dropping it would make a
        # long sentence unsayable at any cap setting.
        while len(self._queue) > 1 and self._backlog_locked() > self._lag_cap_s:
            victim = self._queue.popleft()
            self.dropped += 1
            log.warning(
                "%s playout %.1fs behind; dropped an utterance (%d total)",
                self.direction.value,
                self._backlog_locked(),
                self.dropped,
            )
            if self._on_dropped is not None:
                self._on_dropped(victim)

    def _backlog_locked(self) -> float:
        return sum(i.audio_s for i in self._queue) + len(self._pending) / TTS_BYTES_PER_S

    def tick(self) -> bool:
        """Write exactly one chunk. True if it carried speech."""
        finished: Translated | None = None
        with self._lock:
            if not self._pending and self._queue:
                self._current = self._queue.popleft()
                self._pending = self._current.pcm

            if self._pending:
                chunk = self._pending[:CHUNK_BYTES]
                self._pending = self._pending[CHUNK_BYTES:]
                if not self._pending:
                    finished = self._current
                    self._current = None
                if len(chunk) < CHUNK_BYTES:
                    chunk = chunk + b"\x00" * (CHUNK_BYTES - len(chunk))
            else:
                chunk = None

        if chunk is None:
            if self._duck is not None:
                self._duck.open()
            self._sink.write(SILENCE_CHUNK)
            return False

        if self._duck is not None:
            self._duck.close()
        self._sink.write(chunk)
        if finished is not None and self._on_spoken is not None:
            self._on_spoken(finished)
        return True

    def run(self, stop: threading.Event) -> None:
        """Pace comes from the sink.

        pw-cat blocks on write once its buffer is full, so this loop runs at
        real time without a sleep. A sleep on the healthy path would fight
        that and drift - see docs/experiments/02-pwcat-playback.md, where a
        sleep-paced loop under-fed the sink by 0.69% over 30 minutes while a
        loop that let pw-cat's blocking pace it measured 1.018 over 60s.

        But PwCatSink.write() swallows a dead pipe and becomes a no-op, and a
        no-op never blocks - so a sink that dies mid-call removes the only
        thing pacing this loop and it would pin a CPU core until hangup. The
        fallback wait below is not belt-and-braces; it is the whole reason the
        `failed` flag is readable from here.
        """
        while not stop.is_set():
            self.tick()
            if getattr(self._sink, "failed", False):
                stop.wait(CHUNK_MS / 1000)
        if self._duck is not None:
            self._duck.open()
        self._sink.close()
