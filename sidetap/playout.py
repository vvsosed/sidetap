"""Speak translated audio, duck the original, and bound the lag.

One long-lived pw-cat per direction, fed raw PCM. Between utterances this
writes silence rather than stopping, so playout always knows exactly when it
is emitting speech - which is what drives the duck.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .ports import AudioSink, VolumeControl
from .types import LAG_CAP_S, TTS_BYTES_PER_S, TTS_RATE, Direction, Translated, Unit

log = logging.getLogger(__name__)

CHUNK_MS = 20
CHUNK_BYTES = TTS_BYTES_PER_S * CHUNK_MS // 1000
SILENCE_CHUNK = b"\x00" * CHUNK_BYTES

# An utterance starts playing once it holds this much audio, or is closed. The
# first synthesis chunk is always 200 ms and the second lands ~30 ms later
# (docs/experiments/04-tts-streaming.md), so 400 ms costs ~30 ms and doubles
# the margin against a network stall.
START_BUFFER_S = 0.4

# Consecutive starved ticks before giving up: 100 x 20 ms = 2 s. Counted in
# ticks so playout needs no clock.
#
# Shared by three cases, each with its own yardstick. A playing utterance:
# synthesis runs 4.7-7.1x faster than playback, so 2 s of nothing means the
# producer is gone. A queued head that never started: first chunks arrive in
# 182-543 ms, and giving up loses the whole sentence. The continuation hold
# (`expect_continuation`): the longest the duck may stay shut with no speech.
# Retuning this moves all three, and the last decides how long a broken
# pipeline can silence the other party.
STARVE_LIMIT_TICKS = 100


class DuckControl:
    """Silences the remote party's original while the translation speaks.

    Starts open and only calls the volume control on a transition, so a stream
    of 20 ms speech chunks produces one wpctl call, not fifty per second.
    """

    def __init__(self, volume: VolumeControl, object_id: int | Callable[[], int | None]):
        """`object_id` may be a callable, and for a live session it must be.

        Router.engage() returns before pw-loopback has registered the duck, so
        its id is still None when Session.setup() builds this. Resolving it on
        each transition lets the id arrive a poll later.
        """
        self._volume = volume
        self._object_id = object_id
        self._closed = False

    def _resolve(self) -> int | None:
        if callable(self._object_id):
            return self._object_id()
        return self._object_id

    def close(self) -> None:
        # Only flip on success: set_volume returns False when wpctl fails, and
        # flipping anyway would desync the flag and skip the retry. A duck not
        # yet registered is the same case.
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


@dataclass
class Utterance:
    """One sentence, possibly still being synthesised.

    Mutated only under Playout._lock. Callbacks still receive `Translated`,
    since the audio is complete by the time they fire.

    The producer holds this as a handle for append()/finish() and never
    writes to it. After finish() returns it may read two fields:

    - `truncated`: safe, because a closed utterance has no other writer.
    - `dropped`: unsynchronised, since flush() can set it from the TUI thread
      at any moment. Benign: the window is one tick and the audio is gone
      either way, so a lost race only reports "truncated" for "dropped".
    """

    unit: Unit
    text: str
    pcm: bytearray = field(default_factory=bytearray)
    closed: bool = False      # synthesis finished, or failed
    dropped: bool = False     # flushed; the producer must stop synthesising
    truncated: bool = False   # closed by a failure, not by completion

    @property
    def audio_s(self) -> float:
        return len(self.pcm) / TTS_BYTES_PER_S

    def snapshot(self) -> Translated:
        return Translated(unit=self.unit, text=self.text, pcm=bytes(self.pcm))


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
        self._queue: deque[Utterance] = deque()
        self._current: Utterance | None = None
        self._offset = 0
        self._starved_ticks = 0
        self._continuation = False
        self._hold_ticks = 0

    @property
    def duck(self) -> DuckControl | None:
        return self._duck

    def begin(self, unit: Unit, text: str) -> Utterance:
        """Queue an utterance that has not been synthesised yet."""
        item = Utterance(unit=unit, text=text)
        with self._lock:
            self._queue.append(item)
            victims = self._trim_locked()
        self._report_dropped(victims)
        return item

    def append(self, item: Utterance, chunk: bytes) -> bool:
        """Add synthesised audio. False means stop synthesising: the rest of
        this utterance will never be heard.

        Either it was flushed (bypass, mute or the drop-backlog hotkey) or
        playout already closed it at the starvation bound; callers must not
        assume the first.
        """
        with self._lock:
            if item.dropped or item.closed:
                return False
            item.pcm.extend(chunk)
            victims = self._trim_locked()
            accepted = not item.dropped
        self._report_dropped(victims)
        return accepted

    def finish(self, item: Utterance, *, truncated: bool = False) -> None:
        """No more audio is coming."""
        with self._lock:
            item.closed = True
            # Sticky: playout may already have set this by abandoning a
            # stalled utterance, and a later finish(truncated=False) must not
            # hide that the listener heard a cut-off sentence.
            item.truncated = item.truncated or truncated
            if not item.pcm and item is not self._current:
                # No audio ever arrived, so there is nothing for tick() to
                # play. Already gone from the queue (lag cap or flush) is fine.
                try:
                    self._queue.remove(item)
                except ValueError:
                    pass

    def submit(self, item: Translated) -> None:
        """Queue a complete utterance: the degenerate streaming case.

        Built and queued under one lock, so the lag cap can never drop it
        before its pcm is in place.
        """
        utterance = Utterance(
            unit=item.unit, text=item.text, pcm=bytearray(item.pcm), closed=True
        )
        with self._lock:
            self._queue.append(utterance)
            victims = self._trim_locked()
        self._report_dropped(victims)

    def backlog_s(self) -> float:
        with self._lock:
            return self._backlog_locked()

    def flush(self) -> int:
        """Drop everything not yet handed to the sink. Returns how many
        whole items went.

        Chunks already written to pw-cat cannot be recalled. The in-progress
        item is cut short but not counted, since part of it was spoken;
        counting it would make the drop-backlog hotkey misreport what the
        listener hears.
        """
        with self._lock:
            count = len(self._queue)
            for item in self._queue:
                item.dropped = True
            self._queue.clear()
            if self._current is not None:
                self._current.dropped = True
            self._current = None
            self._offset = 0
            self._starved_ticks = 0
            # The run this hold belonged to is gone. Leaving it armed would
            # hold the duck shut for audio just thrown away, including across
            # a whole bypass, since set_suppressed() flushes on both edges.
            self._continuation = False
            self._hold_ticks = 0
            return count

    def _trim_locked(self) -> list[tuple[Utterance, float, int]]:
        """Drop the oldest utterances until the backlog fits the cap.

        Returns (victim, backlog_at_drop, running_total) for the caller to
        report after releasing the lock. on_dropped writes the transcript, and
        a blocking write under the lock would stall tick() with the duck
        possibly closed - a stuck-closed path run()'s finally cannot clear,
        because the thread is blocked rather than dead.
        """
        # A sole queued item longer than the cap is kept, or a long sentence
        # would be unsayable. With _current playing, every queued item stays
        # droppable - otherwise the cap would never act on the ordinary
        # monologue shape of one playing and one queued.
        victims: list[tuple[Utterance, float, int]] = []
        while (
            self._queue
            and (len(self._queue) > 1 or self._current is not None)
            and self._backlog_locked() > self._lag_cap_s
        ):
            if not self._queue[0].closed:
                # Still being synthesised: its duration is unknown, so
                # dropping it cannot be shown to help. It is always the
                # newest entry, because _speak is the only production caller
                # of begin()/append()/finish() and handles one unit at a time
                # per direction; submit() never queues an open item.
                #
                # Nothing enforces that. If broken, the cap under-trims -
                # keeping audio rather than dropping a sentence or sticking
                # the duck - so break rather than spin on the head.
                break
            # The backlog that made this a victim, not what is left after.
            backlog_at_drop = self._backlog_locked()
            victim = self._queue.popleft()
            self.dropped += 1
            victim.dropped = True
            victims.append((victim, backlog_at_drop, self.dropped))
        return victims

    def _report_dropped(self, victims: list[tuple[Utterance, float, int]]) -> None:
        """Log and report drops with no lock held. See _trim_locked."""
        for victim, backlog_at_drop, total in victims:
            log.warning(
                "%s playout %.1fs behind; dropped an utterance (%d total)",
                self.direction.value,
                backlog_at_drop,
                total,
            )
            self._invoke(self._on_dropped, victim.snapshot())

    def _invoke(
        self, callback: Callable[[Translated], None] | None, item: Translated
    ) -> None:
        """Run a consumer callback without letting it take down this thread.

        `on_spoken` runs on the playout thread inside tick(); `on_dropped`
        runs from begin(), append() or submit() after the lock is released.
        Either raising - a transcript write on a full disk, say - must not
        propagate: out of tick() it would kill the playout thread with the
        duck possibly closed, and out of the producer's calls it would stop
        that direction submitting anything further.
        """
        if callback is None:
            return
        try:
            callback(item)
        except Exception:
            log.exception("playout callback raised; continuing")

    def _backlog_locked(self) -> float:
        unread = 0
        if self._current is not None:
            unread = len(self._current.pcm) - self._offset
        return (unread + sum(len(i.pcm) for i in self._queue)) / TTS_BYTES_PER_S

    def set_suppressed(self, value: bool) -> None:
        """Both edges throw the queue away.

        A translation of what was said while suppressed is stale by the time
        it could play: the parties talked unmediated under bypass, or the
        remote party simply did not hear you under mute.

        Leaving matters as much as entering: nothing upstream knows this
        playout is suppressed, so pipeline._speak keeps queueing throughout,
        and without this flush, coming back would replay up to LAG_CAP_S of
        stale translation.

        The flag is set before the flush so a tick in flight returns early
        rather than pulling a fresh item.
        """
        self.suppressed = value
        self.flush()

    def expect_continuation(self, value: bool) -> None:
        """More of the speech run in progress is on its way.

        Set while a segmenter commits one utterance clause by clause. The
        queue drains between clauses, and tick() would otherwise open the duck
        and let a burst of the untranslated original through the middle of
        what the listener hears as one sentence.

        This only arms the hold; it clears no counter, and that is the whole
        of its safety. `_hold_ticks` counts ticks with the duck shut, a hold
        armed and no chunk written, and only a written chunk or flush()
        clears it. If arming cleared it, a caller re-arming periodically
        could keep the duck shut forever. A queued clause does not count as
        speech either: the pipeline queues a clause before its audio exists,
        so a stalled, unstartable head is an ordinary shape.

        The guarantee: once armed, the hold cannot keep the duck shut for
        STARVE_LIMIT_TICKS ticks without translated speech being written,
        whatever the caller does (worst case measured under adversarial
        re-arming: 99 ticks, 1.98 s). A starvation arm already part-way to
        its own limit may fire first; that is harmless, since it closes or
        abandons its utterance rather than holding the duck. Unarmed, this
        does nothing.

        A duck stuck closed silences the person you are talking to, which
        this module treats as worse than not working.
        """
        with self._lock:
            self._continuation = value

    def _startable_locked(self, item: Utterance) -> bool:
        """Hold a new utterance until it can absorb a stall.

        `closed` must stay `or`, not `and`: an utterance shorter than the
        threshold is complete, and waiting for more would lose it.

        A head whose producer dies before it clears the threshold would block
        the queue for the rest of the call; `_advance_locked` bounds that wait
        with `_starved_ticks` and then closes the head in place, so the
        fragment that did arrive is spoken.
        """
        return item.closed or item.audio_s >= START_BUFFER_S

    def _advance_locked(self) -> tuple[bytes | None, Translated | None, bool]:
        """Pull, read and retire under the lock.

        Returns (chunk, finished, starved). `starved` means the duck stays
        closed although nothing is playing: either the listener is part-way
        through an utterance (`_offset` has moved), or the producer announced
        a continuation via expect_continuation(). The second is a claim, not
        an observation - it can be armed on an idle playout - so it is bounded
        on ticks by `_charge_hold_locked`.

        Starved does not mean "nothing playable": that would shut the duck
        before the first word of a translation, which is where the original
        belongs.
        """
        if (
            self._current is None
            and self._queue
            and self._startable_locked(self._queue[0])
        ):
            self._current = self._queue.popleft()
            self._offset = 0

        chunk = None
        finished = None
        starved = False
        if self._current is not None:
            unread = len(self._current.pcm) - self._offset
            # A remainder under one chunk plays only once the utterance is
            # closed; while it is open, zero-padding would splice silence into
            # a word. The starvation branch still counts these ticks, so a
            # producer trickling fragments cannot hold the duck forever.
            if unread >= CHUNK_BYTES or (unread > 0 and self._current.closed):
                chunk = bytes(
                    self._current.pcm[self._offset : self._offset + CHUNK_BYTES]
                )
                self._offset += len(chunk)
                if self._current.closed and self._offset >= len(self._current.pcm):
                    finished = self._current.snapshot()
                    self._current = None
                    self._offset = 0
                if len(chunk) < CHUNK_BYTES:
                    chunk = chunk + b"\x00" * (CHUNK_BYTES - len(chunk))
                self._starved_ticks = 0
            elif self._current.closed:
                # Closed after its last byte was written, or it never produced
                # audio. Only the first was spoken, so only it gets on_spoken.
                if self._offset > 0:
                    finished = self._current.snapshot()
                self._current = None
                self._offset = 0
                self._starved_ticks = 0
            else:
                # Case 1 of 2, started and starved; see the `elif` below for
                # a queued head that never started. Both share _starved_ticks
                # with different remedies.
                #
                # Synthesis has not kept up. `offset > 0` means the listener
                # is mid-sentence, which is what the duck follows. Since an
                # open _current starts only at START_BUFFER_S, offset is
                # already > 0 here; the check stays in case that threshold is
                # ever tuned below one chunk.
                starved = self._offset > 0
                self._starved_ticks += 1
                if self._starved_ticks >= STARVE_LIMIT_TICKS:
                    # The producer is gone, not slow. Abandon the utterance
                    # rather than hold the duck closed for the rest of the
                    # call.
                    log.warning(
                        "%s playout: utterance stalled %.1fs mid-sentence; "
                        "abandoning as truncated",
                        self.direction.value,
                        STARVE_LIMIT_TICKS * CHUNK_MS / 1000,
                    )
                    self._current.closed = True
                    self._current.truncated = True
                    if self._offset > 0:
                        finished = self._current.snapshot()
                    self._current = None
                    self._offset = 0
                    self._starved_ticks = 0
                    # This tick opens the duck, so `_charge_hold_locked`
                    # skips it: the hold's deadline slips 20 ms, never more.
                    starved = False
        elif self._queue:
            # Case 2 of 2, queued but under the start threshold. The same
            # bound applies so a producer that died mid-fragment cannot block
            # the queue; nothing has been heard yet, so the duck is open and
            # the risk is the direction going quiet unexplained.
            #
            # An armed hold means this is the gap before the next clause, so
            # the duck stays shut - but the head keeps its own full
            # `_starved_ticks` budget, and the tick is billed to the hold's
            # deadline as well.
            starved = self._continuation
            self._starved_ticks += 1
            if self._starved_ticks >= STARVE_LIMIT_TICKS:
                # Close it in place rather than discard it: `closed` makes it
                # startable, so the fragment that did arrive is spoken, and
                # everything queued behind it is unblocked.
                log.warning(
                    "%s playout: queued utterance stuck under the start "
                    "threshold for %.1fs; closing what arrived",
                    self.direction.value,
                    STARVE_LIMIT_TICKS * CHUNK_MS / 1000,
                )
                self._queue[0].closed = True
                self._queue[0].truncated = True
                self._starved_ticks = 0
        else:
            # Nothing playing or queued, so `_starved_ticks` resets even
            # during a hold; carrying it across the gap would shorten the next
            # clause's budget and cut it off as truncated.
            self._starved_ticks = 0
            if self._continuation:
                # The producer says more of this run is coming: hold the duck
                # shut across the gap, charged to the hold's deadline below.
                starved = True

        return chunk, finished, self._charge_hold_locked(chunk, starved)

    def _charge_hold_locked(self, chunk: bytes | None, starved: bool) -> bool:
        """Bill this tick to the continuation hold, and expire it if due.

        Returns `starved` unchanged unless the hold has run out, in which case
        it disarms and returns False. Called once, after the branch chain,
        because the hold reaches the duck from two arms and a deadline
        watching only one would not bound it.

        `_hold_ticks` counts consecutive ticks the duck was held shut with a
        hold armed and no chunk reaching the sink. Only a written chunk (here)
        or flush() clears it; arming and expiring do not.
        """
        if chunk is not None:
            self._hold_ticks = 0
            return starved
        if starved and self._continuation:
            self._hold_ticks += 1
            if self._hold_ticks >= STARVE_LIMIT_TICKS:
                log.warning(
                    "%s playout: held the duck for %.1fs with no translated "
                    "speech; reopening it and dropping the hold",
                    self.direction.value,
                    STARVE_LIMIT_TICKS * CHUNK_MS / 1000,
                )
                # Disarm, but do not zero the counter. Left armed, the hold
                # would re-arm itself and the duck would sawtooth; zeroed, each
                # re-arm would buy another full bound. Left spent, a re-arm
                # expires after one tick until real audio resets it.
                self._continuation = False
                return False
        return starved

    def tick(self) -> bool:
        """Write exactly one chunk. True if it carried speech."""
        if self.suppressed:
            # Keep writing silence so pw-cat stays primed and the duck stays
            # open, but speak nothing.
            if self._duck is not None:
                self._duck.open()
            self._sink.write(SILENCE_CHUNK)
            return False

        with self._lock:
            chunk, finished, starved = self._advance_locked()

        if chunk is None:
            if self._duck is not None:
                if starved:
                    # Mid-sentence gap. close() is idempotent, so this is not
                    # a wpctl call per tick.
                    self._duck.close()
                else:
                    self._duck.open()
            self._sink.write(SILENCE_CHUNK)
            if finished is not None:
                self._invoke(self._on_spoken, finished)
            return False

        if self._duck is not None:
            self._duck.close()
        self._sink.write(chunk)
        if finished is not None:
            self._invoke(self._on_spoken, finished)
        return True

    def run(self, stop: threading.Event) -> None:
        """Pace comes from the sink.

        pw-cat blocks on write once its buffer is full, so this loop runs in
        real time without a sleep; a sleep-paced loop drifts and under-feeds
        the sink (docs/experiments/02-pwcat-playback.md).

        PwCatSink.write() turns into a non-blocking no-op once the pipe dies,
        which would leave this loop spinning a CPU core; the `failed` wait
        below paces it instead.

        A sink that is alive but blocked is not covered: tick() closes the
        duck, then blocks in write(), and the starvation bound cannot advance
        until write() returns. Bypass stays a manual escape, since it opens
        the duck directly rather than via tick().

        The finally reopens the duck if tick() ever raises, because a dead
        pipeline must leave the remote party audible, not silent.
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
    a silent OUT failure has to make a sound. Generated with math.sin rather
    than numpy, which the audio path deliberately does without.
    """
    import math
    import struct

    samples = int(TTS_BYTES_PER_S * duration_s) // 2
    amplitude = int(32767 * level)
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * frequency * i / TTS_RATE)))
        for i in range(samples)
    )
