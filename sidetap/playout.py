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
from .types import LAG_CAP_S, TTS_BYTES_PER_S, TTS_RATE, Direction, Translated

log = logging.getLogger(__name__)

CHUNK_MS = 20
CHUNK_BYTES = TTS_BYTES_PER_S * CHUNK_MS // 1000
SILENCE_CHUNK = b"\x00" * CHUNK_BYTES


class DuckControl:
    """Silences the remote party's original while the translation speaks.

    Starts open and only calls the volume control on a transition, so a stream
    of 20 ms speech chunks produces one wpctl call, not fifty per second.
    """

    def __init__(self, volume: VolumeControl, object_id: int | Callable[[], int | None]):
        """`object_id` may be a callable, and for a live session it must be.

        Router.engage() finishes before pw-loopback has registered the duck
        with the graph - deliberately, because the alternative is journalling
        links to ports that do not exist yet - so the duck's object id is
        still None when Session.setup() builds this. Reading it once there
        meant the duck was never created at all, ducking never happened, and
        the user heard the original underneath every translation for the whole
        call, with nothing logged. Resolving it on each transition lets the id
        arrive a poll later, which is exactly when it does arrive.
        """
        self._volume = volume
        self._object_id = object_id
        self._closed = False

    def _resolve(self) -> int | None:
        if callable(self._object_id):
            return self._object_id()
        return self._object_id

    def close(self) -> None:
        # Only flip on a successful call. set_volume returns False rather
        # than raising when wpctl fails; flipping anyway would desync the
        # flag from the real volume and the next transition would think it
        # is already in the target state and skip retrying. A duck that has
        # not appeared yet is the same case: not an error, just not yet.
        if self._closed:
            return
        object_id = self._resolve()
        if object_id is not None and self._volume.set_volume(object_id, 0.0):
            self._closed = True

    def open(self) -> None:
        if not self._closed:
            return
        object_id = self._resolve()
        if object_id is not None and self._volume.set_volume(object_id, 1.0):
            self._closed = False

    @property
    def is_open(self) -> bool:
        return not self._closed


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
        self.suppressed = False
        self._sink = sink
        self._duck = duck
        self._lag_cap_s = lag_cap_s
        self._on_spoken = on_spoken
        self._on_dropped = on_dropped
        self._lock = threading.Lock()
        self._queue: deque[Translated] = deque()
        self._current: Translated | None = None
        self._pending = b""

    @property
    def duck(self) -> DuckControl | None:
        return self._duck

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
        # long sentence unsayable at any cap setting. That rule is about the
        # queue's sole survivor, not about _current - if something is already
        # playing in _current, every queued item is still droppable, because
        # dropping them still leaves _current to finish. Looking only at
        # len(self._queue) misses exactly the ordinary shape of a monologue
        # (one utterance playing, the next queued), where the cap would
        # otherwise never act.
        while (
            self._queue
            and (len(self._queue) > 1 or self._current is not None)
            and self._backlog_locked() > self._lag_cap_s
        ):
            victim = self._queue.popleft()
            self.dropped += 1
            log.warning(
                "%s playout %.1fs behind; dropped an utterance (%d total)",
                self.direction.value,
                self._backlog_locked(),
                self.dropped,
            )
            self._invoke(self._on_dropped, victim)

    def _invoke(
        self, callback: Callable[[Translated], None] | None, item: Translated
    ) -> None:
        """Run a consumer callback without letting it take down this thread.

        `on_spoken` fires on the playout thread, inside tick(), right after
        the duck closes. `on_dropped` fires wherever submit() is called,
        while the lock is held. Either one raising - a transcript write
        hitting a full disk, say (Task 25) - must not propagate: out of
        tick() it would kill the playout thread with the duck stuck closed,
        which is exactly the fail-safe this module exists to provide,
        inverted into silence; out of submit() it would silently stop the
        producer thread from submitting anything further.
        """
        if callback is None:
            return
        try:
            callback(item)
        except Exception:
            log.exception("playout callback raised; continuing")

    def _backlog_locked(self) -> float:
        return sum(i.audio_s for i in self._queue) + len(self._pending) / TTS_BYTES_PER_S

    def set_suppressed(self, value: bool) -> None:
        """Entering bypass throws the queue away.

        Two reasons. The conversation during bypass happens unmediated, so a
        translation of it is worth nothing by the time it plays - it would
        arrive as a voice recapping a minute the user has already had. And the
        lag cap lives below the suppressed branch in tick(), so a backlog built
        while suppressed is never trimmed: a two-minute bypass would come back
        with two minutes queued and push the lot through on_dropped at once.

        The flag is set before the flush so a tick already in flight returns
        early rather than pulling a fresh item; the 20 ms chunk it may already
        have written is gone, for the reason flush() documents.
        """
        self.suppressed = value
        if value:
            self.flush()

    def tick(self) -> bool:
        """Write exactly one chunk. True if it carried speech."""
        if self.suppressed:
            # Bypass: the parties are talking to each other unmediated. Keep
            # writing silence so pw-cat's buffer stays primed and the duck
            # stays open, but speak nothing.
            if self._duck is not None:
                self._duck.open()
            self._sink.write(SILENCE_CHUNK)
            return False

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
        if finished is not None:
            self._invoke(self._on_spoken, finished)
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

        The try/finally is defense in depth: on_spoken/on_dropped are already
        isolated by _invoke, so in practice tick() should not raise, but if
        it ever does, cleanup still has to run. Leaving the duck closed would
        replace the module's one fail-safe - a dead pipeline leaves the user
        hearing the remote party untranslated - with silence instead, which
        is the opposite.
        """
        try:
            while not stop.is_set():
                self.tick()
                if getattr(self._sink, "failed", False):
                    stop.wait(CHUNK_MS / 1000)
        finally:
            if self._duck is not None:
                self._duck.open()
            self._sink.close()


def earcon(duration_s: float = 0.25, frequency: float = 880.0, level: float = 0.25) -> bytes:
    """A short tone for the dead-air alarm.

    During a call you are looking at the other person, not at a dashboard, so
    the OUT direction failing silently has to make a sound. Generated rather
    than shipped as an asset, and with math.sin rather than numpy, because the
    capture path deliberately has no numpy in it.
    """
    import math
    import struct

    samples = int(TTS_BYTES_PER_S * duration_s) // 2
    amplitude = int(32767 * level)
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * frequency * i / TTS_RATE)))
        for i in range(samples)
    )
