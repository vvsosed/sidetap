"""Construct and drive a pw-record subprocess.

pw-record resamples to our target format and writes raw PCM to stdout, which
is why nothing in this package does sample-rate conversion.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import BinaryIO, Iterator

from .ports import ManagedProcess, ProcessLauncher
from .types import BLOCK_BYTES, TARGET_RATE

PW_RECORD = "pw-record"


def format_properties(props: dict[str, str]) -> str:
    """Render the --properties argument.

    PipeWire parses this as JSON-ish. Every value must stay quoted: an
    unquoted space splits the property, which is then dropped silently.
    Nothing escapes double quotes, so no key or value may contain one.
    """
    body = " ".join(f'{key}="{value}"' for key, value in props.items())
    return "{ " + body + " }"


def build_argv(
    *,
    node_name: str,
    media_name: str,
    target: int | None = None,
    capture_sink: bool = False,
    autoconnect: bool = True,
    latency: str = "100ms",
) -> list[str]:
    props = {"node.name": node_name, "media.name": media_name}
    if capture_sink:
        props["stream.capture.sink"] = "true"
    if not autoconnect:
        props["node.autoconnect"] = "false"

    argv = [
        PW_RECORD,
        "--rate",
        str(TARGET_RATE),
        "--channels",
        "1",
        "--format",
        "s16",
        "--latency",
        latency,
        "--properties",
        format_properties(props),
        "--raw",
    ]
    if target is not None:
        argv += ["--target", str(target)]
    argv.append("-")
    return argv


def read_blocks(stream: BinaryIO, block_bytes: int = BLOCK_BYTES) -> Iterator[bytes]:
    """Yield fixed-size blocks, reassembling short reads.

    A partial block at end of stream is dropped: consumers are entitled to
    assume every chunk is exactly one block.
    """
    while True:
        buf = stream.read(block_bytes)
        if not buf:
            return
        while len(buf) < block_bytes:
            more = stream.read(block_bytes - len(buf))
            if not more:
                return
            buf += more
        yield buf


@dataclass(frozen=True)
class RecorderSpec:
    track: str
    target: int | None = None
    capture_sink: bool = False
    autoconnect: bool = True
    latency: str = "100ms"


class Recorder:
    def __init__(self, spec: RecorderSpec, launcher: ProcessLauncher):
        self.spec = spec
        self.node_name = f"sidetap.{spec.track}.{uuid.uuid4().hex[:8]}"
        self._launcher = launcher
        self._process: ManagedProcess | None = None

    def argv(self) -> list[str]:
        return build_argv(
            node_name=self.node_name,
            media_name=f"sidetap {self.spec.track}",
            target=self.spec.target,
            capture_sink=self.spec.capture_sink,
            autoconnect=self.spec.autoconnect,
            latency=self.spec.latency,
        )

    def start(self) -> None:
        self._process = self._launcher.spawn(self.argv())

    def blocks(self) -> Iterator[bytes]:
        assert self._process is not None, "call start() first"
        yield from read_blocks(self._process.stdout)

    def failure(self) -> str | None:
        """Non-None once pw-record has exited unexpectedly."""
        if self._process is None:
            return None
        code = self._process.poll()
        if code in (None, 0):
            return None
        return self._process.stderr_text().strip() or f"pw-record exited {code}"

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
