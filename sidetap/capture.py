"""Decide what to capture, then own the recorders, queues and threads."""

from __future__ import annotations

import logging
import queue
import threading
import time

from dataclasses import dataclass

from .graph import SINK, SOURCE, PwGraph
from .ports import Clock, GraphSource, Linker, ProcessLauncher
from .recorder import Recorder, RecorderSpec
from .tap import AppTap
from .types import MIC, REMOTE, AudioChunk

log = logging.getLogger(__name__)

QUEUE_BLOCKS = 400  # about 40 seconds of audio at 100 ms per block
DROP_LOG_EVERY = 100
NODE_APPEAR_TIMEOUT_S = 5.0
SHUTDOWN_TIMEOUT_S = 2.0


class CaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class CaptureConfig:
    mic: str | None = None
    remote: str | None = None
    app: str | None = None
    mic_enabled: bool = True
    remote_enabled: bool = True
    latency: str = "100ms"


def plan_recorders(graph: PwGraph, config: CaptureConfig) -> list[RecorderSpec]:
    """Pure: work out what to record from one graph snapshot."""
    specs: list[RecorderSpec] = []

    if config.mic_enabled:
        node = (
            graph.find(config.mic, SOURCE)
            if config.mic
            else graph.node_by_name(graph.default_source or "")
        )
        if node is None:
            raise CaptureError(
                f"No microphone matching {config.mic!r}. Try: sidetap devices"
            )
        specs.append(
            RecorderSpec(track=MIC, target=node.serial, latency=config.latency)
        )

    if config.remote_enabled:
        if config.app:
            # No target: AppTap makes the links itself, and autoconnect must be
            # off so WirePlumber does not attach us to the default microphone.
            specs.append(
                RecorderSpec(
                    track=REMOTE,
                    target=None,
                    autoconnect=False,
                    latency=config.latency,
                )
            )
        else:
            # Whole-sink capture: stream.capture.sink against the monitor, the
            # Linux equivalent of WASAPI loopback. Everything playing lands in
            # the recogniser, notification chimes included.
            node = (
                graph.find(config.remote, SINK)
                if config.remote
                else graph.node_by_name(graph.default_sink or "")
            )
            if node is None:
                raise CaptureError(
                    f"No output device matching {config.remote!r}. "
                    "Try: sidetap devices"
                )
            specs.append(
                RecorderSpec(
                    track=REMOTE,
                    target=node.serial,
                    capture_sink=True,
                    latency=config.latency,
                )
            )

    if not specs:
        raise CaptureError("Nothing to capture: both tracks are disabled.")
    return specs


class DroppingQueue:
    """Bounded queue that drops and counts rather than blocking the producer.

    Blocking here would stall the stdout pump and eventually pw-record itself.
    Audio is lost either way when the engine cannot keep up; this way the
    pipeline survives and says so.
    """

    def __init__(self, maxsize: int = QUEUE_BLOCKS):
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self.dropped = 0
        # Arrivals, not just losses. A track that has gone quiet because its
        # capture node was unlinked delivers zero bytes, not silence, so
        # nothing downstream can tell it apart from nobody talking - this
        # counter is the only place that difference is visible.
        self.accepted = 0

    def put(self, item) -> bool:
        try:
            self._queue.put_nowait(item)
            self.accepted += 1
            return True
        except queue.Full:
            self.dropped += 1
            if self.dropped % DROP_LOG_EVERY == 1:
                log.warning(
                    "audio queue full, dropped %d blocks so far "
                    "(is the engine keeping up?)",
                    self.dropped,
                )
            return False

    def get(self, timeout: float | None = None):
        return self._queue.get(timeout=timeout)


class PipeWireCapture:
    def __init__(
        self,
        config: CaptureConfig,
        graph: GraphSource,
        launcher: ProcessLauncher,
        linker: Linker | None,
        clock: Clock,
    ):
        self._config = config
        self._graph = graph
        self._launcher = launcher
        self._linker = linker
        self._clock = clock

        self.stop = threading.Event()
        self.recorders: dict[str, Recorder] = {}
        self.queues: dict[str, DroppingQueue] = {}
        self.dead_tracks: set[str] = set()
        self._threads: list[threading.Thread] = []
        self._t0 = clock.monotonic()

        for spec in plan_recorders(graph.snapshot(), config):
            self.recorders[spec.track] = Recorder(spec, launcher)
            self.queues[spec.track] = DroppingQueue()

    def start(self) -> None:
        for recorder in self.recorders.values():
            recorder.start()

        for track, recorder in self.recorders.items():
            thread = threading.Thread(
                target=self.pump, args=(track, recorder), daemon=True, name=f"pump-{track}"
            )
            thread.start()
            self._threads.append(thread)

        if self._config.app and REMOTE in self.recorders:
            assert self._linker is not None, "app mode needs a Linker"
            self.await_capture_node(self.recorders[REMOTE])
            tap = AppTap(
                pattern=self._config.app,
                capture_node_name=self.recorders[REMOTE].node_name,
                graph=self._graph,
                linker=self._linker,
                clock=self._clock,
            )
            thread = threading.Thread(
                target=tap.run, args=(self.stop,), daemon=True, name="app-tap"
            )
            thread.start()
            self._threads.append(thread)
            log.info("watching for streams matching %r", self._config.app)

    def await_capture_node(self, recorder: Recorder) -> None:
        """Block until pw-record registers with the graph.

        If it never does, pw-record is broken and linking would silently do
        nothing, so fail loudly with a way to check that.
        """
        deadline = self._clock.monotonic() + NODE_APPEAR_TIMEOUT_S
        while self._clock.monotonic() < deadline:
            if self._graph.snapshot().node_by_name(recorder.node_name) is not None:
                return
            self._clock.sleep(0.2)
        raise CaptureError(
            "Capture node never appeared - is pw-record working? "
            "Test with: pw-record --target=0 /tmp/t.wav"
        )

    def pump(self, track: str, recorder: Recorder) -> None:
        """Move framed blocks from one recorder into its queue."""
        for block in recorder.blocks():
            chunk = AudioChunk(
                track=track,
                pcm=block,
                t_start=self._clock.monotonic() - self._t0,
            )
            self.queues[track].put(chunk)

        failure = recorder.failure()
        if failure and not self.stop.is_set():
            log.error("capture for track %r stopped: %s", track, failure)
            self.dead_tracks.add(track)

    def all_tracks_dead(self) -> bool:
        return bool(self.recorders) and self.dead_tracks == set(self.recorders)

    def shutdown(self) -> None:
        self.stop.set()
        for recorder in self.recorders.values():
            recorder.stop()
        # One shared budget rather than a fresh timeout per thread: joining
        # three threads at 2 s each would stall Ctrl-C for six seconds. Real
        # time, not the injected clock - these are real threads.
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT_S
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        for track, q in self.queues.items():
            if q.dropped:
                log.warning("track %r dropped %d blocks in total", track, q.dropped)
