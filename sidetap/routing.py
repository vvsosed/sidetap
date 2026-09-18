"""Own the duck path, and be able to give the graph back.

Engaging unlinks the application from your speakers and routes it through a
loopback whose volume playout controls. That makes restoration a correctness
requirement rather than cleanup: a crash without it leaves the user with no
call audio and nothing on screen explaining why.

Every change is journalled to disk BEFORE it is made, so a kill -9 between the
two is recoverable. Journal entries name nodes by object.serial and ports by
name - never by port id, which PipeWire recycles.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from .graph import PLAYBACK_STREAM, PwGraph
from .ports import GraphSource, Linker, LoopbackFactory, LoopbackSpec, Unlinker

log = logging.getLogger(__name__)

# Matches AppTap's cadence - the two watchers exist for the same reason.
POLL_INTERVAL_S = 2.0

DUCK_NODE = "sidetap_duck"
VIRTMIC_SINK = "sidetap_tts_sink"
VIRTMIC_SOURCE = "sidetap_virtmic"
VIRTMIC_DESCRIPTION = "sidetap Virtual Mic"

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
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "broken": [r.to_dict() for r in self.broken],
            "made": [r.to_dict() for r in self.made],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Journal:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A half-written file from a kill -9 must not stop the next run.
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
        # stream can inherit its dead predecessor's id within one poll, the
        # same hazard AppTap's dedup key guards against.
        self._routed: set[int] = set()
        self._app_pattern: str | None = None
        # BOTH, deliberately. The serial is the durable identifier the journal
        # records; the id is what wpctl resolves against for the duck volume.
        # Conflating them makes the duck silently never close - see
        # docs/experiments/01-tap-volume.md.
        self.duck_serial: int | None = None
        self.duck_id: int | None = None

    def engage(self, app_pattern: str, capture_node_name: str | None) -> None:
        self._app_pattern = app_pattern
        # Exactly one snapshot for this call, taken BEFORE the loopback is
        # spawned - we need the current default sink's name to give
        # pw-loopback a --playback-props target.object, and that has to
        # happen before create() is called at all.
        #
        # It is then reused, rather than re-read, to look up the duck itself.
        # spawn_writer() returns as soon as the process forks; there is no
        # guarantee the loopback has registered its nodes with the graph by
        # the time a subsequent pw-dump would run, so a fresh read here could
        # race it either way. Reusing this snapshot means engage() simply
        # finds no duck yet (_route() below then does nothing but record the
        # pattern) and the very next poll_once() - at most POLL_INTERVAL_S
        # later, the same bound AppTap already relies on for a restarted
        # stream - completes the routing once the duck has appeared. What
        # engage() must never do is guess at duck ports from a snapshot that
        # cannot possibly contain them and journal something that was never
        # actually linked.
        snapshot = self._graph.snapshot()
        default_sink = snapshot.node_by_name(snapshot.default_sink or "")
        target_sink_name = default_sink.name if default_sink else ""

        self._loopback = self._loopbacks.create(duck_loopback_spec(target_sink_name))

        self._route(snapshot, app_pattern, initial=True)

    def poll_once(self) -> int:
        """Route any matching stream that is not routed yet. Returns how many.

        Applications create and re-create their streams late and often - Zoom
        when the meeting starts, again if the call drops and reconnects. A
        stream that appears after engage() would otherwise be autoconnected
        straight to the speakers by WirePlumber: unducked, unjournalled, and
        invisible to restore(), so the original plays over the translation for
        the rest of the call and the graph is left modified on exit.
        """
        if self._app_pattern is None:
            return 0
        snapshot = self._graph.snapshot()
        return self._route(snapshot, self._app_pattern, initial=False)

    def _route(self, snapshot: PwGraph, app_pattern: str, *, initial: bool) -> int:
        duck = snapshot.node_by_name(DUCK_NODE)
        # Refreshed on every call, not just engage()'s. engage()'s own
        # snapshot can predate the loopback actually registering (see the
        # comment in engage()), so if duck_serial/duck_id only ever got set
        # there, a slow-to-register duck would leave them None forever even
        # once poll_once() goes on to find and route through it - and the
        # duck volume control that reads duck_id from here would then never
        # be able to close it, exactly the failure
        # docs/experiments/01-tap-volume.md flags.
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
        routed = 0

        for stream in snapshot.by_class(PLAYBACK_STREAM):
            if not stream.matches(app_pattern):
                continue
            if stream.serial in self._routed:
                continue
            for index, out_port in enumerate(snapshot.ports_of(stream.id, "out")):
                # Only the DEFAULT sink, and paired by index the way
                # WirePlumber links them (FL->FL, FR->FR). Breaking every out
                # port against every in port of every sink would journal links
                # that never existed - and restore would then create them,
                # leaving the user worse off than before sidetap ran.
                #
                # LIMIT: index pairing is correct only because ports_of()
                # sorts by NAME and, for stereo and mono, alphabetical order
                # happens to equal channel order. A 5.1 sink alphabetizes to
                # FC, FL, FR, LFE, SL, SR - NOT positional order - and this
                # would silently cross channels. v1 is scoped to stereo/mono.
                # Fixing it properly means carrying audio.channel through
                # PwPort, which graph.py currently drops.
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
            self._routed.add(stream.serial)
            routed += 1
            log.info("routing %s (serial=%s) through the duck", stream.label, stream.serial)

        if not broken and not made:
            return routed

        # Durable BEFORE the graph is touched. A crash in between is exactly
        # the case the journal exists for. On the first call the journal is
        # empty on disk, so loading and appending also covers engage().
        journal = Journal.load(self._journal_path)
        Journal(
            broken=journal.broken + tuple(broken),
            made=journal.made + tuple(made),
        ).save(self._journal_path)

        for ref in broken:
            self._apply(snapshot, ref, link=False)
        for ref in made:
            self._apply(snapshot, ref, link=True)

        if initial:
            log.info(
                "routed %r through %s (%d links broken, %d made)",
                app_pattern,
                DUCK_NODE,
                len(broken),
                len(made),
            )
        return routed

    def run(self, stop: threading.Event, interval: float = POLL_INTERVAL_S) -> None:
        """Re-scan until stopped. A transient graph read must not kill this."""
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                log.debug("routing watcher: %s", exc)
            stop.wait(interval)

    def restore(self) -> None:
        journal = Journal.load(self._journal_path)
        if journal.is_empty():
            return
        snapshot = self._graph.snapshot()
        for ref in journal.made:
            self._apply(snapshot, ref, link=False)
        for ref in journal.broken:
            self._apply(snapshot, ref, link=True)
        Journal().save(self._journal_path)
        if self._loopback is not None:
            self._loopback.terminate()
            self._loopback = None

    def repair(self) -> bool:
        """Replay a journal left behind by a session that died.

        Returns True if there was one. Idempotent: entries whose nodes are
        gone are skipped, and the journal is cleared either way.
        """
        journal = Journal.load(self._journal_path)
        if journal.is_empty():
            return False
        log.warning(
            "found a routing journal from a previous session; repairing the graph"
        )
        self.restore()
        return True

    def _apply(self, snapshot: PwGraph, ref: LinkRef, *, link: bool) -> None:
        ports = resolve(snapshot, ref)
        if ports is None:
            # The application exited; its serial is gone for good. Nothing to
            # restore, and refusing to continue would strand the rest.
            log.debug("skipping stale link %s", ref)
            return
        src, dst = ports
        if link:
            self._linker.link(src, dst)
        else:
            self._unlinker.unlink(src, dst)
