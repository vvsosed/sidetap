"""Live pipeline state, written by worker threads and read by the UI.

The dependency runs one way only: pipeline threads write here, the TUI polls
snapshot(). Nothing in the pipeline imports the UI, which is what lets
--no-tui and the headless test suite be the same code path.
"""

from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum

from .types import Direction, Latency


class Health(Enum):
    OK = "ok"
    RETRYING = "retrying"
    FAILED = "failed"


@dataclass
class DirectionState:
    interim: str = ""
    final: str = ""
    translation: str = ""
    queue_s: float = 0.0
    dropped: int = 0
    capture_dropped: int = 0
    dead_air: bool = False
    no_audio: bool = False
    playback_failed: bool = False
    asr: Health = Health.OK
    mt: Health = Health.OK
    tts: Health = Health.OK
    latency: Latency = field(default_factory=Latency)


@dataclass
class Snapshot:
    directions: dict[Direction, DirectionState]
    cost_usd: float = 0.0
    bypassed: bool = False
    # Your translated voice is not being sent. Kept separate from `bypassed`,
    # which also suppresses OUT, so leaving bypass restores mute rather than
    # clearing it.
    muted_out: bool = False
    # The translation model actually in use. Session-level: one translator
    # serves both directions, so its downgrade applies to the whole call.
    mt_model: str = ""


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._states = {d: DirectionState() for d in Direction}
        self._cost_usd = 0.0
        self._bypassed = False
        self._muted_out = False
        self._mt_model = ""

    def set_interim(self, direction: Direction, text: str) -> None:
        with self._lock:
            self._states[direction].interim = text

    def set_final(
        self,
        direction: Direction,
        source_text: str,
        translation: str,
        latency: Latency,
    ) -> None:
        with self._lock:
            state = self._states[direction]
            state.interim = ""
            state.final = source_text
            state.translation = translation
            state.latency = latency

    def set_queue_s(self, direction: Direction, seconds: float) -> None:
        with self._lock:
            self._states[direction].queue_s = seconds

    def add_dropped(self, direction: Direction, count: int) -> None:
        with self._lock:
            self._states[direction].dropped += count

    def set_dead_air(self, direction: Direction, value: bool) -> None:
        with self._lock:
            self._states[direction].dead_air = value

    def set_no_audio(self, direction: Direction, value: bool) -> None:
        """No audio at all is reaching this direction's capture queue.

        Unlike dead_air (an utterance produced nothing), this is upstream of
        everything: an unlinked capture delivers zero bytes, not silence, so
        every stage looks healthy while doing nothing.
        """
        with self._lock:
            self._states[direction].no_audio = value

    def set_playback_failed(self, direction: Direction, value: bool) -> None:
        """This direction's pw-cat has died, so nothing it plays is heard."""
        with self._lock:
            self._states[direction].playback_failed = value

    def set_capture_dropped(self, direction: Direction, count: int) -> None:
        """Blocks the CAPTURE queue discarded, as an absolute count.

        Distinct from `dropped`, the utterances the lag cap threw away on the
        playout side. This queue fills when an outage stops recognition
        draining it; unreported, the lost stretch would read as silence.
        """
        with self._lock:
            self._states[direction].capture_dropped = count

    def set_health(
        self,
        direction: Direction,
        *,
        asr: Health | None = None,
        mt: Health | None = None,
        tts: Health | None = None,
    ) -> None:
        with self._lock:
            state = self._states[direction]
            if asr is not None:
                state.asr = asr
            if mt is not None:
                state.mt = mt
            if tts is not None:
                state.tts = tts

    def add_cost(self, usd: float) -> None:
        with self._lock:
            self._cost_usd += usd

    def set_bypassed(self, value: bool) -> None:
        with self._lock:
            self._bypassed = value

    def set_muted_out(self, value: bool) -> None:
        with self._lock:
            self._muted_out = value

    def set_mt_model(self, model: str) -> None:
        """Record which translation model is in use.

        GoogleTranslator's sticky downgrade to NMT is otherwise invisible: the
        next successful NMT call sets mt=Health.OK and the pane goes green.
        """
        with self._lock:
            self._mt_model = model

    def snapshot(self) -> Snapshot:
        """A deep copy. The UI renders from this while threads keep writing."""
        with self._lock:
            return Snapshot(
                directions=deepcopy(self._states),
                cost_usd=self._cost_usd,
                bypassed=self._bypassed,
                muted_out=self._muted_out,
                mt_model=self._mt_model,
            )
