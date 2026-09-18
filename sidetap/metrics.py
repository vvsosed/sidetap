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
    asr: Health = Health.OK
    mt: Health = Health.OK
    tts: Health = Health.OK
    latency: Latency = field(default_factory=Latency)


@dataclass
class Snapshot:
    directions: dict[Direction, DirectionState]
    cost_usd: float = 0.0
    bypassed: bool = False


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._states = {d: DirectionState() for d in Direction}
        self._cost_usd = 0.0
        self._bypassed = False

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

    def set_capture_dropped(self, direction: Direction, count: int) -> None:
        """Blocks the CAPTURE queue discarded, as an absolute count.

        Distinct from `dropped`, which counts utterances the lag cap threw
        away on the playout side. These two queues overflow for unrelated
        reasons - this one fills when a network outage stops the recogniser
        draining it - and meetscribe's documented bug was exactly this one
        going unreported, so a lost stretch read as nobody talking.
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

    def snapshot(self) -> Snapshot:
        """A deep copy. The UI renders from this while threads keep writing."""
        with self._lock:
            return Snapshot(
                directions=deepcopy(self._states),
                cost_usd=self._cost_usd,
                bypassed=self._bypassed,
            )
