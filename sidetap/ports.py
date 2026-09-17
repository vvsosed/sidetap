"""The boundaries between this program and the outside world.

Every Protocol here has exactly one real implementation and one fake in
tests/conftest.py. Nothing else in the package touches a subprocess, a socket
or the wall clock.
"""

from __future__ import annotations

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

    Separate from ManagedProcess rather than one type with both pipes: the
    ported capture code only ever reads, and widening its Protocol would let a
    stdin-less fake satisfy a consumer that needs one.
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

        wpctl resolves against the id; the serial is a separate counter and
        yields "Object not found". This does not contradict the project's
        "identify nodes by object.serial" rule - that is about DURABLE
        references (the routing journal, the tap's dedup keys) where ids get
        recycled over time. See docs/experiments/01-tap-volume.md, which hit
        this exact trap.
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
    def synthesize(self, text: str, voice: str) -> Iterator[bytes]: ...


@runtime_checkable
class AudioSink(Protocol):
    def write(self, pcm: bytes) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...
