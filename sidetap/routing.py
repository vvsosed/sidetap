"""Own the duck path, and be able to give the graph back.

Engaging unlinks the application from your speakers and routes it through a
loopback whose volume playout controls. Restoring it is therefore a
correctness requirement, not cleanup: without it a crash leaves the user with
no call audio.

Every change is journalled to disk BEFORE it is made, so a kill -9 between the
two is recoverable. Entries name nodes by object.serial and ports by name,
never by port id, which PipeWire recycles.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from .graph import PLAYBACK_STREAM, PwGraph
from .ports import GraphSource, Linker, LinkResult, LoopbackFactory, LoopbackSpec, Unlinker

log = logging.getLogger(__name__)

# Matches AppTap's cadence - the two watchers exist for the same reason.
POLL_INTERVAL_S = 2.0

DUCK_NODE = "sidetap_duck"
VIRTMIC_SINK = "sidetap_tts_sink"
VIRTMIC_SOURCE = "sidetap_virtmic"
VIRTMIC_DESCRIPTION = "sidetap Virtual Mic"
# Deliberately different from VIRTMIC_DESCRIPTION: sidetap_tts_sink is written
# only by sidetap, sidetap_virtmic is the mic the messenger should select. With
# one description they look identical in a volume UI, and picking the wrong one
# silently sends the remote party nothing.
VIRTMIC_CAPTURE_DESCRIPTION = "sidetap TTS input"

VIRTMIC_CONFIG = f"""\
# Installed by `sidetap doctor`.
#
# Presents two nodes: a sink sidetap writes synthesised speech into, and a
# source your messenger sees as an ordinary microphone.
#
# This is a permanent config file rather than something sidetap creates at
# runtime, and that is deliberate. A runtime-created device has a different
# identity every session, so Zoom and Viber lose the saved selection and fall
# back to your real microphone - silently, with the remote party hearing your
# untranslated voice.
#
# Apply with: systemctl --user restart pipewire pipewire-pulse

context.modules = [
  {{ name = libpipewire-module-loopback
    args = {{
      node.description = "{VIRTMIC_DESCRIPTION}"
      capture.props = {{
        node.name       = "{VIRTMIC_SINK}"
        node.description = "{VIRTMIC_CAPTURE_DESCRIPTION}"
        media.class     = Audio/Sink
        audio.position  = [ MONO ]
        audio.rate      = 48000
      }}
      playback.props = {{
        node.name        = "{VIRTMIC_SOURCE}"
        node.description = "{VIRTMIC_DESCRIPTION}"
        media.class      = Audio/Source
        audio.position   = [ MONO ]
        node.passive     = false
      }}
    }}
  }}
]
"""

VIRTMIC_CONFIG_PATH = Path.home() / ".config/pipewire/pipewire.conf.d/90-sidetap-mic.conf"
JOURNAL_PATH = Path.home() / ".local/state/sidetap/routing-journal.json"


@dataclass(frozen=True)
class LinkRef:
    """One link, named durably.

    Serial plus port name, never port id: PipeWire recycles ids, and a
    restarted stream can be handed its dead predecessor's within one poll.
    """

    src_serial: int
    src_port: str
    dst_serial: int
    dst_port: str

    def to_dict(self) -> dict:
        return {
            "src_serial": self.src_serial,
            "src_port": self.src_port,
            "dst_serial": self.dst_serial,
            "dst_port": self.dst_port,
        }

    @classmethod
    def from_dict(cls, data: dict) -> LinkRef:
        return cls(
            src_serial=int(data["src_serial"]),
            src_port=str(data["src_port"]),
            dst_serial=int(data["dst_serial"]),
            dst_port=str(data["dst_port"]),
        )


@dataclass(frozen=True)
class Journal:
    broken: tuple[LinkRef, ...] = ()
    made: tuple[LinkRef, ...] = ()

    def save(self, path: Path) -> None:
        """Write atomically: a temp file in the same directory, then os.replace().

        _route_locked() rewrites the ACCUMULATED journal on every routing
        call, so a torn write would erase the record of every change still
        live in the graph, not just the entry being added.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "broken": [r.to_dict() for r in self.broken],
            "made": [r.to_dict() for r in self.made],
        }
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except BaseException:
            # Do not leave a stray .tmp to accumulate on every failure.
            tmp.unlink(missing_ok=True)
            raise

    @classmethod
    def load(cls, path: Path) -> Journal:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # No session has ever journalled here: the common case.
            return cls()
        except (OSError, json.JSONDecodeError) as exc:
            # A file that exists but will not parse means an interrupted
            # write, and it may be the only record of a link still live in the
            # graph. Starting with nothing to restore beats refusing to start,
            # but not silently.
            log.error(
                "routing journal at %s is unreadable (%s) - proceeding as if "
                "there is nothing to restore, but the audio graph may still "
                "be modified from a previous session. Run `sidetap doctor "
                "--repair` and check manually if call audio sounds wrong.",
                path,
                exc,
            )
            return cls()
        return cls(
            broken=tuple(LinkRef.from_dict(d) for d in payload.get("broken", ())),
            made=tuple(LinkRef.from_dict(d) for d in payload.get("made", ())),
        )

    def is_empty(self) -> bool:
        return not self.broken and not self.made


def resolve(graph: PwGraph, ref: LinkRef) -> tuple[int, int] | None:
    """LinkRef -> live port ids, or None if either end is gone."""
    by_serial = {n.serial: n for n in graph.nodes}
    src_node = by_serial.get(ref.src_serial)
    dst_node = by_serial.get(ref.dst_serial)
    if src_node is None or dst_node is None:
        return None

    src = next(
        (p for p in graph.ports_of(src_node.id, "out") if p.name == ref.src_port), None
    )
    dst = next(
        (p for p in graph.ports_of(dst_node.id, "in") if p.name == ref.dst_port), None
    )
    if src is None:
        src = next(
            (p for p in graph.ports if p.node_id == src_node.id and p.name == ref.src_port),
            None,
        )
    if dst is None:
        dst = next(
            (p for p in graph.ports if p.node_id == dst_node.id and p.name == ref.dst_port),
            None,
        )
    if src is None or dst is None:
        return None
    return (src.id, dst.id)


def duck_loopback_spec(target_sink: str) -> LoopbackSpec:
    """A sink we own, playing into the real speakers at a volume we control."""
    return LoopbackSpec(
        capture_props=(
            ("node.name", DUCK_NODE),
            ("node.description", "sidetap duck"),
            ("media.class", "Audio/Sink"),
            ("audio.position", "[ FL FR ]"),
        ),
        playback_props=(
            ("node.name", f"{DUCK_NODE}_out"),
            ("target.object", target_sink),
            ("node.passive", "false"),
        ),
    )


class Router:
    def __init__(
        self,
        graph: GraphSource,
        linker: Linker,
        unlinker: Unlinker,
        loopbacks: LoopbackFactory,
        journal_path: Path = JOURNAL_PATH,
    ):
        self._graph = graph
        self._linker = linker
        self._unlinker = unlinker
        self._loopbacks = loopbacks
        self._journal_path = journal_path
        self._loopback = None
        # Serials already routed through the duck. Serial, not id: a restarted
        # stream can inherit its dead predecessor's id within one poll.
        self._routed: set[int] = set()
        self._app_pattern: str | None = None
        # Held across whole method bodies: the watcher thread polls while the
        # main thread can restore() at any moment, and without it a poll could
        # re-break a link restore() just fixed. These run a few times a
        # session, never per audio block, so contention is irrelevant.
        self._lock = threading.Lock()
        # Both, deliberately: the journal records the durable serial, while
        # wpctl only accepts the id. Conflating them means the duck silently
        # never closes (docs/experiments/01-tap-volume.md).
        self.duck_serial: int | None = None
        self.duck_id: int | None = None

    @property
    def has_routed(self) -> bool:
        """Has any application stream actually been rewired through the duck?

        This arms the IN direction's no-audio watch; see Session._armed.
        """
        return bool(self._routed)

    def engage(self, app_pattern: str) -> None:
        with self._lock:
            if self._loopback is not None:
                # Otherwise the old loopback leaks and a second node named
                # sidetap_duck makes node_by_name(DUCK_NODE) ambiguous.
                raise RuntimeError(
                    "Router.engage() called while already engaged - call "
                    "restore() before engaging again"
                )
            self._app_pattern = app_pattern
            # One snapshot, taken before the loopback is spawned (its target
            # is the current default sink) and then reused rather than
            # re-read: spawn_writer() returns as soon as the process forks,
            # so a fresh read could race the duck's registration either way.
            # With the snapshot, engage() just finds no duck yet and the next
            # poll_once() finishes routing. It must never guess at duck ports
            # and journal links that were never made.
            snapshot = self._graph.snapshot()
            default_sink = snapshot.node_by_name(snapshot.default_sink or "")
            target_sink_name = default_sink.name if default_sink else ""

            self._loopback = self._loopbacks.create(duck_loopback_spec(target_sink_name))

            self._route_locked(snapshot, app_pattern, initial=True)

    def poll_once(self) -> int:
        """Route any matching stream that is not routed yet. Returns how many.

        Applications re-create streams late and often (a meeting starting, a
        reconnect). Without this, WirePlumber would autoconnect a new stream
        straight to the speakers - unducked, unjournalled and invisible to
        restore().
        """
        if self._app_pattern is None:
            return 0
        with self._lock:
            snapshot = self._graph.snapshot()
            return self._route_locked(snapshot, self._app_pattern, initial=False)

    def _route_locked(self, snapshot: PwGraph, app_pattern: str, *, initial: bool) -> int:
        """Caller must hold self._lock."""
        duck = snapshot.node_by_name(DUCK_NODE)
        # Refreshed on every call, not just engage()'s: engage()'s snapshot can
        # predate the duck registering, and a duck_id stuck at None means the
        # duck can never close.
        if duck is not None:
            self.duck_serial = duck.serial
            self.duck_id = duck.id
        elif initial:
            self.duck_serial = None
            self.duck_id = None
        if duck is None:
            return 0
        duck_inputs = snapshot.ports_of(duck.id, "in")

        default_sink = snapshot.node_by_name(snapshot.default_sink or "")
        sink_inputs = snapshot.ports_of(default_sink.id, "in") if default_sink else ()

        broken: list[LinkRef] = []
        made: list[LinkRef] = []
        candidates: set[int] = set()

        for stream in snapshot.by_class(PLAYBACK_STREAM):
            if not stream.matches(app_pattern):
                continue
            if stream.serial in self._routed:
                continue
            for index, out_port in enumerate(snapshot.ports_of(stream.id, "out")):
                # Only the DEFAULT sink, paired by index as WirePlumber links
                # them (FL->FL, FR->FR). Anything broader would journal links
                # that never existed, and restore() would then create them.
                #
                # LIMIT: index pairing works because ports_of() sorts by name,
                # which matches channel order only for mono and stereo. 5.1
                # sorts to FC, FL, FR, LFE, SL, SR and would cross channels;
                # fixing it means carrying audio.channel through PwPort.
                if sink_inputs and default_sink is not None:
                    in_port = sink_inputs[min(index, len(sink_inputs) - 1)]
                    broken.append(
                        LinkRef(
                            stream.serial, out_port.name, default_sink.serial, in_port.name
                        )
                    )
                if duck_inputs:
                    in_port = duck_inputs[min(index, len(duck_inputs) - 1)]
                    made.append(
                        LinkRef(stream.serial, out_port.name, duck.serial, in_port.name)
                    )
            candidates.add(stream.serial)
            log.info("routing %s (serial=%s) through the duck", stream.label, stream.serial)

        if not broken and not made:
            return 0

        # Durable BEFORE the graph is touched: a crash in between is exactly
        # what the journal is for.
        journal = Journal.load(self._journal_path)
        # Append only what is new. A failing stream is retried on every poll
        # (see below) and recomputes the same refs, and appending blindly would
        # grow the journal without bound - each a full rewrite under the lock
        # shutdown needs, and each replayed again by restore().
        known_broken = set(journal.broken)
        known_made = set(journal.made)
        new_broken = tuple(ref for ref in broken if ref not in known_broken)
        new_made = tuple(ref for ref in made if ref not in known_made)
        if new_broken or new_made:
            Journal(
                broken=journal.broken + new_broken,
                made=journal.made + new_made,
            ).save(self._journal_path)

        # A FAILED apply is not done, as in AppTap. If the unlink from the
        # speakers succeeds but the link into the duck fails, the stream is
        # connected to nothing; leaving its serial out of self._routed makes
        # the next poll_once() retry it.
        failed: set[int] = set()
        for ref in broken:
            if self._apply(snapshot, ref, link=False) is LinkResult.FAILED:
                failed.add(ref.src_serial)
        for ref in made:
            if self._apply(snapshot, ref, link=True) is LinkResult.FAILED:
                failed.add(ref.src_serial)

        succeeded = candidates - failed
        self._routed |= succeeded
        if failed:
            log.warning(
                "could not fully route %r through %s (serials=%s) - will retry "
                "on the next poll; the original may be briefly audible over "
                "the translation until then",
                app_pattern,
                DUCK_NODE,
                sorted(failed),
            )

        if initial and succeeded:
            log.info(
                "routed %r through %s (%d links broken, %d made)",
                app_pattern,
                DUCK_NODE,
                len(broken),
                len(made),
            )
        return len(succeeded)

    def run(self, stop: threading.Event, interval: float = POLL_INTERVAL_S) -> None:
        """Re-scan until stopped. A transient graph read must not kill this."""
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                log.debug("routing watcher: %s", exc)
            stop.wait(interval)

    def restore(self) -> None:
        with self._lock:
            self._restore_locked()

    def _restore_locked(self) -> None:
        """Caller must hold self._lock."""
        try:
            self._replay_locked()
        finally:
            # Unconditional: engage() creates the duck even when no stream is
            # ever routed and the journal stays empty (start before the call,
            # quit before it begins). Skipping this would orphan pw-loopback,
            # which runs in its own session, and the next engage() would make
            # a second node named sidetap_duck.
            if self._loopback is not None:
                self._loopback.terminate()
                self._loopback = None

    def _replay_locked(self) -> None:
        journal = Journal.load(self._journal_path)
        if journal.is_empty():
            return
        snapshot = self._graph.snapshot()
        failed = False
        for ref in journal.made:
            if self._apply(snapshot, ref, link=False) is LinkResult.FAILED:
                failed = True
        for ref in journal.broken:
            if self._apply(snapshot, ref, link=True) is LinkResult.FAILED:
                failed = True

        if failed:
            # Keep the journal: a transient pw-link failure on exit must not
            # also destroy the only record that could repair the graph.
            log.error(
                "could not fully restore the audio graph - at least one link "
                "failed to apply. Keeping the routing journal so the next "
                "repair can retry; run `sidetap doctor --repair`."
            )
        else:
            Journal().save(self._journal_path)

    def repair(self) -> bool:
        """Replay a journal left behind by a session that died.

        Returns True if there was one. Idempotent: entries whose nodes are
        gone are skipped, and the journal is cleared only once every apply in
        the replay succeeded - see restore().
        """
        with self._lock:
            # A kill -9 leaves pw-loopback running in its own session, and
            # neither this new Router nor the journal knows its PID. A duck
            # node present before this instance ever engaged can only be that
            # leftover, so the best repair() can do is say so.
            snapshot = self._graph.snapshot()
            duck = snapshot.node_by_name(DUCK_NODE)
            if duck is not None:
                log.warning(
                    "found an existing %s node (id=%s) from a previous "
                    "session - its pw-loopback process may still be running "
                    "with nothing able to stop it automatically. If call "
                    "audio still sounds wrong after this repair, stop it by "
                    "hand: pkill -f 'pw-loopback.*%s'",
                    DUCK_NODE,
                    duck.id,
                    DUCK_NODE,
                )

            journal = Journal.load(self._journal_path)
            if journal.is_empty():
                return False
            log.warning(
                "found a routing journal from a previous session; repairing the graph"
            )
            self._restore_locked()
            return True

    def _apply(self, snapshot: PwGraph, ref: LinkRef, *, link: bool) -> LinkResult:
        ports = resolve(snapshot, ref)
        if ports is None:
            # The application exited and its serial is gone for good: nothing
            # to restore. ALREADY_LINKED is the LinkResult for "nothing needed
            # doing", and must not keep a journal alive.
            log.debug("skipping stale link %s", ref)
            return LinkResult.ALREADY_LINKED
        src, dst = ports
        if link:
            return self._linker.link(src, dst)
        else:
            return self._unlinker.unlink(src, dst)
