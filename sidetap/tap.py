"""Link a matching application's audio into our capture node.

The link is additive. The application keeps its existing link to the
speakers, so the user still hears the call, and PipeWire delivers an
identical copy to us.

The watcher re-scans because applications create their audio streams late:
Zoom does it when the meeting starts, not when the app launches. Anything
that resolves nodes once at startup records silence. The same re-scan covers
reconnects when someone switches headphones mid-call.
"""

from __future__ import annotations

import logging
import threading

from .graph import PLAYBACK_STREAM
from .ports import Clock, GraphSource, Linker, LinkResult

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0
GRAPH_ERROR_WARN_AFTER = 3


class AppTap:
    def __init__(
        self,
        pattern: str,
        capture_node_name: str,
        graph: GraphSource,
        linker: Linker,
        clock: Clock,
        interval: float = POLL_INTERVAL_S,
    ):
        self._pattern = pattern
        self._capture_node_name = capture_node_name
        self._graph = graph
        self._linker = linker
        self._clock = clock
        self._interval = interval
        self._linked: set[tuple[int, str, str]] = set()
        self._warned: set[tuple[int, str, str]] = set()
        self.tapped_labels: set[str] = set()

    def poll_once(self) -> int:
        """Link any new matching ports. Returns how many links were created."""
        snapshot = self._graph.snapshot()

        node = snapshot.node_by_name(self._capture_node_name)
        if node is None:
            return 0  # pw-record has not registered with the graph yet
        inputs = snapshot.ports_of(node.id, "in")
        if not inputs:
            return 0

        created = 0
        for source in snapshot.by_class(PLAYBACK_STREAM):
            if not source.matches(self._pattern):
                continue
            outputs = snapshot.ports_of(source.id, "out")
            if not outputs:
                continue

            if source.label not in self.tapped_labels:
                log.info("tapping %s (pid=%s)", source.label, source.pid)
                self.tapped_labels.add(source.label)

            for index, out_port in enumerate(outputs):
                # Fan every channel into our mono input; PipeWire sums them.
                in_port = inputs[min(index, len(inputs) - 1)]
                # Keyed on the node's serial and the port NAMES, never on port
                # ids: PipeWire recycles ids, and a restarted stream can be
                # handed its dead predecessor's ids within one poll interval.
                # Keying on ids would make us skip linking it and capture
                # silence for the rest of the meeting.
                pair = (source.serial, out_port.name, in_port.name)
                if pair in self._linked:
                    continue

                result = self._linker.link(out_port.id, in_port.id)
                if result is LinkResult.FAILED:
                    log.debug(
                        "link %s:%s -> %s failed",
                        source.label,
                        out_port.name,
                        in_port.name,
                    )
                    if pair not in self._warned:
                        self._warned.add(pair)
                        log.warning(
                            "could not link %s (%s) into the capture node - "
                            "audio from that application may be missing",
                            source.label,
                            out_port.name,
                        )
                    continue  # deliberately not recorded, so it is retried

                self._linked.add(pair)
                if result is LinkResult.LINKED:
                    created += 1
        return created

    def run(self, stop: threading.Event) -> None:
        consecutive_errors = 0
        while not stop.is_set():
            try:
                self.poll_once()
                consecutive_errors = 0
            except Exception as exc:  # a transient graph read must not kill us
                consecutive_errors += 1
                if consecutive_errors == GRAPH_ERROR_WARN_AFTER:
                    # One blip is unremarkable. Failing repeatedly means we are
                    # blind to new streams for the rest of the meeting, which
                    # must not be debug-only. Warn once, not every poll.
                    # The cause is whatever %s carries - it may be the graph
                    # read, but a missing pw-link lands here too.
                    log.warning(
                        "tap watcher failing repeatedly (%s) - no longer "
                        "picking up new streams matching %r",
                        exc,
                        self._pattern,
                    )
                else:
                    log.debug("tap watcher: %s", exc)
            self._clock.sleep(self._interval)
