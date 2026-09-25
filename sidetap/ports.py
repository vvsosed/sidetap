"""The boundaries between this program and the outside world.

Every Protocol here has exactly one real implementation and one fake in
tests/conftest.py. Nothing else in the package touches a subprocess, a socket
or the wall clock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import BinaryIO, Iterator, Protocol, Sequence, runtime_checkable

from .graph import PwGraph
from .types import AsrResult, Unit


class LinkResult(Enum):
    LINKED = "linked"
    ALREADY_LINKED = "already_linked"
    FAILED = "failed"


@dataclass(frozen=True)
class LoopbackSpec:
    """Arguments for one pw-loopback process.

    Both sides are property maps exactly as pw-loopback expects them; the
    adapter renders them onto the command line. Keeping this a value type is
    what lets routing.py be tested without PipeWire.
    """

    capture_props: tuple[tuple[str, str], ...]
    playback_props: tuple[tuple[str, str], ...]


@runtime_checkable
class GraphSource(Protocol):
    def snapshot(self) -> PwGraph: ...


@runtime_checkable
class ManagedProcess(Protocol):
    """A process we read from."""

    @property
    def stdout(self) -> BinaryIO: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def stderr_text(self) -> str: ...


@runtime_checkable
class WritableProcess(Protocol):
    """A process we write to. pw-cat --playback and pw-loopback.

    Separate from ManagedProcess: capture only ever reads, and one type with
    both pipes would let a stdin-less fake satisfy a consumer that needs one.
    """

    @property
    def stdin(self) -> BinaryIO: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def stderr_text(self) -> str: ...


@runtime_checkable
class ProcessLauncher(Protocol):
    def spawn(self, argv: Sequence[str]) -> ManagedProcess: ...

    def spawn_writer(self, argv: Sequence[str]) -> WritableProcess: ...


@runtime_checkable
class Linker(Protocol):
    def link(self, src_port: int, dst_port: int) -> LinkResult: ...


@runtime_checkable
class Unlinker(Protocol):
    def unlink(self, src_port: int, dst_port: int) -> LinkResult: ...


@runtime_checkable
class VolumeControl(Protocol):
    def set_volume(self, object_id: int, fraction: float) -> bool:
        """`object_id` is the PipeWire global object.id, NOT object.serial.

        wpctl resolves against the id; a serial yields "Object not found".
        Durable references (the routing journal, the tap's dedup keys) still
        use serials, because ids are recycled. See
        docs/experiments/01-tap-volume.md.
        """
        ...


@runtime_checkable
class LoopbackFactory(Protocol):
    def create(self, spec: LoopbackSpec) -> WritableProcess: ...


@runtime_checkable
class Recognizer(Protocol):
    def stream(self, pcm: Iterator[bytes]) -> Iterator[AsrResult]: ...


@runtime_checkable
class Segmenter(Protocol):
    def feed(self, result: AsrResult) -> list[Unit]: ...


@runtime_checkable
class Translator(Protocol):
    def translate(self, text: str, src: str, tgt: str) -> str: ...


@runtime_checkable
class Synthesizer(Protocol):
    def synthesize(
        self, text: str, voice: str, speaking_rate: float = 1.0
    ) -> Iterator[bytes]:
        """The rate is per call, for the same reason the voice is.

        The directions translate opposite ways, so their useful rates are
        inverses (1.23x one way is about 0.81x the other); one shared rate
        would suit only one direction.
        """
        ...


@runtime_checkable
class AudioSink(Protocol):
    def write(self, pcm: bytes) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...

    def wait(self, event: threading.Event, timeout: float) -> bool:
        """Block until `event` is set or `timeout` elapses.

        Returns whether `event` was set, like threading.Event.wait. Unlike
        `sleep` it wakes as soon as `event` is set from another thread, which
        is why poll loops that shutdown must interrupt use it.
        """
        ...
