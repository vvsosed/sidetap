"""Turn `pw-dump` output into data.

Pure on purpose: this module takes a string and returns a PwGraph. Running
pw-dump is adapters.py's job, which is what lets every test here work off a
fixture with no PipeWire session.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

# media.class values, per pipewire-props(7)
SINK = "Audio/Sink"
SOURCE = "Audio/Source"
PLAYBACK_STREAM = "Stream/Output/Audio"  # an application producing sound

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PwNode:
    id: int
    serial: int
    name: str
    description: str
    media_class: str
    app_name: str | None = None
    app_binary: str | None = None
    pid: int | None = None

    @property
    def label(self) -> str:
        return self.app_name or self.description or self.name

    def matches(self, needle: str) -> bool:
        lowered = needle.lower()
        fields = (self.name, self.description, self.app_name, self.app_binary)
        return any(lowered in (value or "").lower() for value in fields)


@dataclass(frozen=True)
class PwPort:
    id: int
    node_id: int
    name: str
    direction: str  # "out" | "in"


@dataclass(frozen=True)
class PwGraph:
    nodes: tuple[PwNode, ...] = ()
    ports: tuple[PwPort, ...] = ()
    default_sink: str | None = None
    default_source: str | None = None

    def by_class(self, media_class: str) -> tuple[PwNode, ...]:
        return tuple(n for n in self.nodes if n.media_class == media_class)

    def node_by_name(self, name: str) -> PwNode | None:
        return next((n for n in self.nodes if n.name == name), None)

    def find(self, needle: str, media_class: str | None = None) -> PwNode | None:
        pool = self.by_class(media_class) if media_class else self.nodes
        exact = next((n for n in pool if n.name == needle), None)
        return exact or next((n for n in pool if n.matches(needle)), None)

    def ports_of(self, node_id: int, direction: str) -> tuple[PwPort, ...]:
        matching = (
            p for p in self.ports if p.node_id == node_id and p.direction == direction
        )
        return tuple(sorted(matching, key=lambda p: p.name))


def parse_graph(dump_text: str) -> PwGraph:
    nodes: list[PwNode] = []
    ports: list[PwPort] = []
    default_sink: str | None = None
    default_source: str | None = None

    for obj in json.loads(dump_text):
        obj_type = obj.get("type", "")
        props = (obj.get("info") or {}).get("props") or {}

        if obj_type.endswith("Interface:Node"):
            media_class = props.get("media.class")
            if not media_class:
                continue  # links, filters and other plumbing we do not care about
            serial = props.get("object.serial")
            if serial is None:
                serial = obj["id"]
                log.warning(
                    "node %r has no object.serial; falling back to id %s, which "
                    "PipeWire recycles - a long capture targeting it may end up "
                    "on the wrong stream",
                    props.get("node.name", ""),
                    serial,
                )
            nodes.append(
                PwNode(
                    id=obj["id"],
                    serial=serial,
                    name=props.get("node.name", ""),
                    description=props.get("node.description", ""),
                    media_class=media_class,
                    app_name=props.get("application.name"),
                    app_binary=props.get("application.process.binary"),
                    pid=props.get("application.process.id"),
                )
            )

        elif obj_type.endswith("Interface:Port"):
            ports.append(
                PwPort(
                    id=obj["id"],
                    node_id=props.get("node.id", -1),
                    name=props.get("port.name", ""),
                    direction=props.get("port.direction", ""),
                )
            )

        elif obj_type.endswith("Interface:Metadata"):
            if (obj.get("props") or {}).get("metadata.name") != "default":
                continue
            for entry in obj.get("metadata") or []:
                value = entry.get("value")
                name = value.get("name") if isinstance(value, dict) else value
                if entry.get("key") == "default.audio.sink":
                    default_sink = name
                elif entry.get("key") == "default.audio.source":
                    default_source = name

    return PwGraph(tuple(nodes), tuple(ports), default_sink, default_source)
