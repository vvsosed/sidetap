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
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .graph import PLAYBACK_STREAM, SINK, PwGraph, PwLink, PwNode, PwPort
from .ports import GraphSource, Linker, LinkResult, LoopbackFactory, LoopbackSpec, Unlinker

log = logging.getLogger(__name__)

# Matches AppTap's cadence - the two watchers exist for the same reason.
POLL_INTERVAL_S = 2.0

# The prefix of every duck's node.name; each session appends its own suffix.
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


def _new_duck_name() -> str:
    """A duck name no earlier session can have used.

    A session killed without cleanup leaves its pw-loopback running, possibly
    at 0% volume. Sharing its name, the next engage() would route the call
    into that leftover and never raise its volume.
    """
    return f"{DUCK_NODE}.{uuid.uuid4().hex[:8]}"


def _is_duck(node: PwNode) -> bool:
    return node.media_class == SINK and (
        node.name == DUCK_NODE or node.name.startswith(DUCK_NODE + ".")
    )


def duck_loopback_spec(target_sink: str, name: str = DUCK_NODE) -> LoopbackSpec:
    """A sink we own, playing into the real speakers at a volume we control."""
    return LoopbackSpec(
        capture_props=(
            ("node.name", name),
            ("node.description", "sidetap duck"),
            ("media.class", "Audio/Sink"),
            ("audio.position", "[ FL FR ]"),
        ),
        playback_props=(
            ("node.name", f"{name}_out"),
            ("target.object", target_sink),
            ("node.passive", "false"),
        ),
    )


class _Plan(NamedTuple):
    """What one stream needs this pass: duck links to make, sink links to break."""

    stream: PwNode
    make: list[LinkRef]
    brk: list[tuple[LinkRef, PwLink]]


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
        # This session's duck. Unique per engage(), so a leftover can't match.
        self.duck_name: str | None = None
        # Serials already routed through the duck. Serial, not id: a restarted
        # stream can inherit its dead predecessor's id within one poll.
        self._routed: set[int] = set()
        # Serials of links this session removed. A snapshot may still show one
        # briefly; a link re-created afterwards gets a new serial.
        self._broken_links: set[int] = set()
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
                # Otherwise the old loopback leaks.
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

            self.duck_name = _new_duck_name()
            self._loopback = self._loopbacks.create(
                duck_loopback_spec(target_sink_name, self.duck_name)
            )

            self._route_locked(snapshot, app_pattern, initial=True)

    def poll_once(self) -> int:
        """Route any matching stream that is not routed yet. Returns how many.

        Applications re-create streams late and often (a meeting starting, a
        reconnect), and WirePlumber may link a routed stream to a sink again.
        Without this, either would play straight to the speakers - unducked,
        unjournalled and invisible to restore().
        """
        if self._app_pattern is None:
            return 0
        with self._lock:
            snapshot = self._graph.snapshot()
            return self._route_locked(snapshot, self._app_pattern, initial=False)

    def _route_locked(self, snapshot: PwGraph, app_pattern: str, *, initial: bool) -> int:
        """Caller must hold self._lock."""
        duck = snapshot.node_by_name(self.duck_name) if self.duck_name else None
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
        if not duck_inputs:
            return 0

        plans: list[_Plan] = []
        routed = 0
        for stream in snapshot.by_class(PLAYBACK_STREAM):
            if not stream.matches(app_pattern):
                continue
            outputs = snapshot.ports_of(stream.id, "out")
            if not outputs:
                # No ports yet: WirePlumber has not configured it. Next poll.
                continue
            plan = self._plan(snapshot, stream, outputs, duck, duck_inputs)
            if plan.make or plan.brk:
                plans.append(plan)
            elif stream.serial not in self._routed:
                # Already wired to the duck and to nothing else.
                self._routed.add(stream.serial)
                routed += 1

        if not plans:
            return routed

        # Durable BEFORE the graph is touched: a crash in between is exactly
        # what the journal is for.
        self._journal_locked(plans)

        failed: set[int] = set()
        for plan in plans:
            if plan.make:
                log.info(
                    "routing %s (serial=%s) through the duck",
                    plan.stream.label,
                    plan.stream.serial,
                )
            # Into the duck first, and away from the speakers only once that
            # worked: a failure leaves the original audible, never silent.
            results = [self._apply(snapshot, ref, link=True) for ref in plan.make]
            if LinkResult.FAILED in results:
                failed.add(plan.stream.serial)
                continue
            for _, link in plan.brk:
                result = self._unlinker.unlink(link.output_port, link.input_port)
                if result is LinkResult.FAILED:
                    failed.add(plan.stream.serial)
                else:
                    self._broken_links.add(link.serial)
            if plan.stream.serial not in self._routed:
                self._routed.add(plan.stream.serial)
                routed += 1

        if failed:
            log.warning(
                "could not fully route %r through %s (serials=%s) - will retry "
                "on the next poll; the original may be briefly audible over "
                "the translation until then",
                app_pattern,
                duck.name,
                sorted(failed),
            )
        if initial and routed:
            log.info(
                "routed %r through %s (%d links broken, %d made)",
                app_pattern,
                duck.name,
                sum(len(p.brk) for p in plans),
                sum(len(p.make) for p in plans),
            )
        return routed

    def _plan(
        self,
        snapshot: PwGraph,
        stream: PwNode,
        outputs: tuple[PwPort, ...],
        duck: PwNode,
        duck_inputs: tuple[PwPort, ...],
    ) -> _Plan:
        """Which links this stream needs made and broken, from the live graph.

        Broken: the stream's actual links into real sinks, whichever device it
        plays to. Guessing the default sink instead misses a device chosen in
        the app, and restore() would then create links that never existed.
        sidetap's own sinks are never targets.

        Made: its ports into the duck, paired by index as WirePlumber pairs
        them (FL->FL, FR->FR). LIMIT: ports_of() sorts by name, which matches
        channel order only for mono and stereo; 5.1 would cross channels.
        """
        make: list[LinkRef] = []
        if stream.serial not in self._routed:
            for index, out_port in enumerate(outputs):
                in_port = duck_inputs[min(index, len(duck_inputs) - 1)]
                if not snapshot.has_link(out_port.id, in_port.id):
                    make.append(
                        LinkRef(stream.serial, out_port.name, duck.serial, in_port.name)
                    )

        nodes = {n.id: n for n in snapshot.nodes}
        ports = {p.id: p for p in snapshot.ports}
        brk: list[tuple[LinkRef, PwLink]] = []
        for link in snapshot.links_from(stream.id):
            target = nodes.get(link.input_node)
            src = ports.get(link.output_port)
            dst = ports.get(link.input_port)
            if (
                link.serial in self._broken_links
                or target is None
                or target.media_class != SINK
                or target.is_sidetap
                or src is None
                or dst is None
            ):
                continue
            ref = LinkRef(stream.serial, src.name, target.serial, dst.name)
            brk.append((ref, link))
        return _Plan(stream, make, brk)

    def _journal_locked(self, plans: list[_Plan]) -> None:
        """Add what these plans will change to the journal. Caller holds the lock."""
        journal = Journal.load(self._journal_path)
        made = list(journal.made)
        broken = list(journal.broken)
        for plan in plans:
            # Only what is new: a failing stream is retried every poll, and
            # appending blindly would grow the journal without bound.
            made.extend(ref for ref in plan.make if ref not in made)
            new = [ref for ref, _ in plan.brk if ref not in broken]
            if new and plan.stream.serial in self._routed:
                # Something re-linked a routed stream to a different sink: that
                # is now its target. Restoring the old one too would leave it
                # playing on both after exit.
                targets = {ref.dst_serial for ref in new}
                broken = [
                    ref
                    for ref in broken
                    if ref.src_serial != plan.stream.serial or ref.dst_serial in targets
                ]
            broken.extend(new)
        if tuple(made) != journal.made or tuple(broken) != journal.broken:
            Journal(broken=tuple(broken), made=tuple(made)).save(self._journal_path)

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
            # which runs in its own session.
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
            # present before this instance ever engaged can only be such a
            # leftover. Its unique name keeps engage() from routing into it,
            # so the best repair() can do is say how to stop it.
            snapshot = self._graph.snapshot()
            for duck in (n for n in snapshot.nodes if _is_duck(n)):
                log.warning(
                    "found an existing %s node (id=%s) from a previous "
                    "session - its pw-loopback process may still be running "
                    "with nothing able to stop it automatically. If call "
                    "audio still sounds wrong after this repair, stop it by "
                    "hand: pkill -f 'pw-loopback.*%s'",
                    duck.name,
                    duck.id,
                    duck.name,
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
