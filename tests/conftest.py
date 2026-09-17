"""Fakes for every port, plus fixture loading helpers."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Iterator, Sequence

import pytest

from sidetap.graph import PwGraph, parse_graph
from sidetap.ports import LinkResult, LoopbackSpec
from sidetap.types import AsrResult

FIXTURES = Path(__file__).parent / "fixtures"


def load_graph(name: str) -> PwGraph:
    return parse_graph((FIXTURES / name).read_text())


class ChunkedBytesIO(io.BytesIO):
    """A stream whose read() returns short, the way a real pipe does."""

    def __init__(self, data: bytes, max_read: int):
        super().__init__(data)
        self._max_read = max_read

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        if size is None or size < 0:
            return super().read()
        return super().read(min(size, self._max_read))


class FakeProcess:
    def __init__(self, stdout: io.BytesIO, stderr: str = ""):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode: int | None = None
        self.terminated = False

    @property
    def stdout(self) -> io.BytesIO:
        return self._stdout

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        # A real SIGTERM exit is -15, not a clean 0. Recorder.failure() treats
        # 0 as "no failure", so reporting 0 here would hide the shutdown path
        # capture.py's dead-track check depends on.
        self.returncode = -15

    def stderr_text(self) -> str:
        return self._stderr

    def die(self, returncode: int = 1, stderr: str = "boom") -> None:
        """Simulate the process exiting mid-session."""
        self.returncode = returncode
        self._stderr = stderr


class FakeWritableProcess:
    """Stands in for pw-cat --playback and pw-loopback."""

    def __init__(self, stderr: str = ""):
        self._stdin = io.BytesIO()
        self._stderr = stderr
        self.returncode: int | None = None
        self.terminated = False

    @property
    def stdin(self) -> io.BytesIO:
        return self._stdin

    @property
    def written(self) -> bytes:
        return self._stdin.getvalue()

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def stderr_text(self) -> str:
        return self._stderr

    def die(self, returncode: int = 1, stderr: str = "boom") -> None:
        self.returncode = returncode
        self._stderr = stderr


class FakeLauncher:
    def __init__(self, script: bytes = b"", max_read: int | None = None):
        self.script = script
        self.max_read = max_read
        self.calls: list[list[str]] = []
        self.writer_calls: list[list[str]] = []
        self.processes: list[FakeProcess] = []
        self.writers: list[FakeWritableProcess] = []

    def spawn(self, argv: Sequence[str]) -> FakeProcess:
        self.calls.append(list(argv))
        stream: io.BytesIO
        if self.max_read is None:
            stream = io.BytesIO(self.script)
        else:
            stream = ChunkedBytesIO(self.script, self.max_read)
        process = FakeProcess(stream)
        self.processes.append(process)
        return process

    def spawn_writer(self, argv: Sequence[str]) -> FakeWritableProcess:
        self.writer_calls.append(list(argv))
        writer = FakeWritableProcess()
        self.writers.append(writer)
        return writer


class FakeGraphSource:
    """Returns each snapshot in turn, then repeats the last one forever."""

    def __init__(self, *snapshots: PwGraph):
        assert snapshots, "give FakeGraphSource at least one snapshot"
        self._queue = list(snapshots)
        self.calls = 0

    def snapshot(self) -> PwGraph:
        self.calls += 1
        if len(self._queue) > 1:
            return self._queue.pop(0)
        return self._queue[0]


class FakeLinker:
    """Serves as both Linker and Unlinker."""

    def __init__(self, result: LinkResult = LinkResult.LINKED):
        self.result = result
        self.links: list[tuple[int, int]] = []
        self.unlinks: list[tuple[int, int]] = []

    def link(self, src_port: int, dst_port: int) -> LinkResult:
        self.links.append((src_port, dst_port))
        return self.result

    def unlink(self, src_port: int, dst_port: int) -> LinkResult:
        self.unlinks.append((src_port, dst_port))
        return self.result


class FakeVolumeControl:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[tuple[int, float]] = []

    def set_volume(self, serial: int, fraction: float) -> bool:
        self.calls.append((serial, fraction))
        return self.ok

    @property
    def current(self) -> float | None:
        return self.calls[-1][1] if self.calls else None


class FakeLoopbackFactory:
    def __init__(self):
        self.specs: list[LoopbackSpec] = []
        self.processes: list[FakeWritableProcess] = []

    def create(self, spec: LoopbackSpec) -> FakeWritableProcess:
        self.specs.append(spec)
        process = FakeWritableProcess()
        self.processes.append(process)
        return process


class FakeAudioSink:
    def __init__(self):
        self.chunks: list[bytes] = []
        self.closed = False

    def write(self, pcm: bytes) -> None:
        self.chunks.append(pcm)

    def close(self) -> None:
        self.closed = True

    @property
    def written(self) -> bytes:
        return b"".join(self.chunks)


class FakeTranslator:
    """Returns a deterministic marker so tests can assert routing, not wording."""

    def __init__(self, mapping: dict[str, str] | None = None, error: Exception | None = None):
        self.mapping = mapping or {}
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    def translate(self, text: str, src: str, tgt: str) -> str:
        self.calls.append((text, src, tgt))
        if self.error is not None:
            raise self.error
        return self.mapping.get(text, f"[{tgt}]{text}")


class FakeSynthesizer:
    def __init__(self, bytes_per_char: int = 480, error: Exception | None = None):
        self.bytes_per_char = bytes_per_char
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def synthesize(self, text: str, voice: str) -> Iterator[bytes]:
        self.calls.append((text, voice))
        if self.error is not None:
            raise self.error
        # Two chunks, so consumers that assume one are caught.
        total = b"\x01\x02" * (len(text) * self.bytes_per_char // 2)
        half = len(total) // 2
        yield total[:half]
        yield total[half:]


class FakeRecognizer:
    def __init__(self, results: list[AsrResult], error: Exception | None = None):
        self.results = results
        self.error = error
        self.sent: list[bytes] = []

    def stream(self, pcm: Iterator[bytes]) -> Iterator[AsrResult]:
        for block in pcm:
            self.sent.append(block)
            if self.error is not None:
                raise self.error
            if self.results:
                yield self.results.pop(0)


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_launcher() -> FakeLauncher:
    return FakeLauncher()


@pytest.fixture
def fake_linker() -> FakeLinker:
    return FakeLinker()


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_volume() -> FakeVolumeControl:
    return FakeVolumeControl()


@pytest.fixture
def idle_graph() -> PwGraph:
    return load_graph("pw_dump_idle.json")


@pytest.fixture
def zoom_graph() -> PwGraph:
    return load_graph("pw_dump_zoom_active.json")
