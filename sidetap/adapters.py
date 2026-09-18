"""Real implementations of every port except Recognizer.

This is the only module in the package that starts a subprocess. Keeping that
in one place is what lets everything else be tested against fakes.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import IO, BinaryIO, Sequence

from .graph import PwGraph, parse_graph
from .ports import LinkResult, LoopbackSpec

log = logging.getLogger(__name__)

INSTALL_HINT = (
    "Install PipeWire's CLI utilities:\n"
    "  Debian/Ubuntu: sudo apt install pipewire-bin pipewire-audio\n"
    "  Fedora:        sudo dnf install pipewire-utils\n"
    "  Arch:          sudo pacman -S pipewire pipewire-audio"
)

MIN_PW_VERSION = (0, 3, 60)
STDERR_TAIL_BYTES = 8192
LINK_TIMEOUT_S = 5
# Not LINK_TIMEOUT_S. This runs synchronously on the playout thread, the same
# one feeding pw-cat's stdin, so a stall here freezes audio output rather than
# merely delaying a link. Better to give up on the duck than to glitch the
# call.
VOLUME_TIMEOUT_S = 0.3


class MissingToolError(RuntimeError):
    pass


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise MissingToolError(f"{name} not found. {INSTALL_HINT}")
    return path


def parse_pw_version(text: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if match is None:
        return (0, 0, 0)
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch))


def classify_link_output(returncode: int, stderr: str) -> LinkResult:
    if returncode == 0:
        return LinkResult.LINKED
    lowered = stderr.lower()
    # "File exists" means we already linked this pair. "No such link" comes
    # back from `pw-link -d` when the link is already gone. Both describe the
    # desired end state holding, so neither is a failure.
    # Matching "no such link" specifically (not a bare "no such") keeps a
    # real failure like "No such port" - linking to a port that does not
    # exist - from being absorbed as a no-op too.
    if "exists" in lowered or "no such link" in lowered:
        return LinkResult.ALREADY_LINKED
    return LinkResult.FAILED


class PwDumpGraphSource:
    def snapshot(self) -> PwGraph:
        result = subprocess.run(
            [require_tool("pw-dump")],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return parse_graph(result.stdout)


class PopenProcess:
    def __init__(
        self, process: subprocess.Popen, stderr_file: IO[bytes] | None = None
    ):
        self._process = process
        self._stderr_file = stderr_file

    @property
    def stdout(self) -> BinaryIO:
        assert self._process.stdout is not None
        return self._process.stdout

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        try:
            os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            self._process.terminate()
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()

    def stderr_text(self) -> str:
        """The tail of whatever the process wrote to stderr."""
        if self._stderr_file is None:
            return ""
        end = self._stderr_file.seek(0, os.SEEK_END)
        self._stderr_file.seek(max(0, end - STDERR_TAIL_BYTES))
        return self._stderr_file.read().decode(errors="replace")


class PopenWriter:
    """A process we write to. pw-cat --playback and pw-loopback."""

    def __init__(self, process: subprocess.Popen, stderr_file: IO[bytes] | None = None):
        self._process = process
        self._stderr_file = stderr_file

    @property
    def stdin(self) -> BinaryIO:
        assert self._process.stdin is not None
        return self._process.stdin

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        try:
            self._process.stdin.close()
        except (OSError, ValueError, AttributeError):
            pass
        # Closing stdin alone is a polite EOF: a well-behaved pw-cat drains
        # its buffer and exits on its own (observed ~0.4s). Give it a real
        # chance to do that before escalating - without this wait, the
        # killpg below fires within milliseconds, every time, and anything
        # still buffered is cut off rather than played out.
        try:
            self._process.wait(timeout=0.5)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            self._process.terminate()
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()

    def stderr_text(self) -> str:
        if self._stderr_file is None:
            return ""
        end = self._stderr_file.seek(0, os.SEEK_END)
        self._stderr_file.seek(max(0, end - STDERR_TAIL_BYTES))
        return self._stderr_file.read().decode(errors="replace")


class SubprocessLauncher:
    def spawn(self, argv: Sequence[str]) -> PopenProcess:
        resolved = [require_tool(argv[0]), *argv[1:]]
        # stderr goes to a temp file, never a pipe. Nothing reads a pipe until
        # the process has already exited, so a chatty pw-record - an inherited
        # PIPEWIRE_DEBUG is enough - fills the 64 KB buffer and blocks forever
        # on write. That stalls stdout too, since it blocks in the same call
        # stack, so capture goes silent with poll() still returning None and
        # even the dead-track check never fires.
        stderr_file = tempfile.TemporaryFile()
        process = subprocess.Popen(
            resolved,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            bufsize=0,
            start_new_session=True,
        )
        return PopenProcess(process, stderr_file)

    def spawn_writer(self, argv: Sequence[str]) -> PopenWriter:
        resolved = [require_tool(argv[0]), *argv[1:]]
        # Same reasoning as spawn(): stderr to a temp file, never a pipe. A
        # chatty pw-cat filling a 64 KB pipe buffer would block on write and
        # stall playout with poll() still returning None.
        stderr_file = tempfile.TemporaryFile()
        process = subprocess.Popen(
            resolved,
            stdin=subprocess.PIPE,
            stderr=stderr_file,
            bufsize=0,
            start_new_session=True,
        )
        return PopenWriter(process, stderr_file)


class PwLinkLinker:
    def link(self, src_port: int, dst_port: int) -> LinkResult:
        try:
            result = subprocess.run(
                [require_tool("pw-link"), str(src_port), str(dst_port)],
                capture_output=True,
                text=True,
                timeout=LINK_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            # Called inside AppTap's poll loop; a hang here would stall the
            # watcher for the rest of the meeting.
            return LinkResult.FAILED
        return classify_link_output(result.returncode, result.stderr or "")

    def unlink(self, src_port: int, dst_port: int) -> LinkResult:
        try:
            result = subprocess.run(
                [require_tool("pw-link"), "-d", str(src_port), str(dst_port)],
                capture_output=True,
                text=True,
                timeout=LINK_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return LinkResult.FAILED
        return classify_link_output(result.returncode, result.stderr or "")


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def wait(self, event: threading.Event, timeout: float) -> bool:
        return event.wait(timeout)


def installed_pw_version() -> tuple[int, int, int]:
    try:
        output = subprocess.run(
            [require_tool("pw-cli"), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (MissingToolError, subprocess.SubprocessError, OSError):
        return (0, 0, 0)
    return parse_pw_version(output)


PW_CAT = "pw-cat"
PW_LOOPBACK = "pw-loopback"
WPCTL = "wpctl"


# 16 KiB of stdin pipe plus a 20 ms node latency. MEASURED, not guessed.
#
# Playout writes silence continuously between utterances, so whatever the pipe
# holds sits AHEAD of every real utterance. At the default 64 KiB that is over
# a second of queued silence added to glass-to-glass latency.
#
# Steady-state buffering, three 12 s runs each at 24 kHz:
#
#     8 KiB  (171 ms cap)   -57, +129, +139 ms   <- starves; the negative run
#                                                   means the writer fell
#                                                   behind and the buffer ran
#                                                   dry, which is audible
#    16 KiB  (341 ms cap)  +299, +299, +299 ms   <- chosen: zero variance
#    32 KiB  (683 ms cap)  +609, +609, +609 ms
#    64 KiB (1365 ms cap) +1259,+1259,+1249 ms   <- the default
#
# So this buys about 960 ms. 8 KiB's lower median is not worth a buffer that
# demonstrably runs dry - underruns crackle, and a crackle is worse than
# 300 ms.
#
# --latency alone does nothing: at the default pipe size it measured 1180 ms,
# because the OS pipe is the buffer, not pw-cat's node latency. Both are
# needed. See docs/experiments/02-pwcat-playback.md.
PIPE_BYTES = 16384
PW_CAT_LATENCY = "20ms"
F_SETPIPE_SZ = 1031


def pwcat_argv(*, target: int | None, rate: int) -> list[str]:
    """One long-lived playback process, fed raw PCM on stdin.

    PipeWire does the resampling, exactly as pw-record does on the way in,
    which is what keeps numpy out of this project entirely.
    """
    argv = [
        PW_CAT,
        "--playback",
        "--rate",
        str(rate),
        "--channels",
        "1",
        "--format",
        "s16",
        "--latency",
        PW_CAT_LATENCY,
        "--raw",
    ]
    if target is not None:
        argv += ["--target", str(target)]
    argv.append("-")
    return argv


def render_props(props: Sequence[tuple[str, str]]) -> str:
    """Render a pw-loopback property map.

    Values stay quoted for the same reason recorder.format_properties quotes
    them: an unquoted space splits the property and it is dropped silently.
    """
    return " ".join(f'{key}="{value}"' for key, value in props)


def loopback_argv(spec: LoopbackSpec) -> list[str]:
    argv = [PW_LOOPBACK]
    if spec.capture_props:
        argv += ["--capture-props", render_props(spec.capture_props)]
    if spec.playback_props:
        argv += ["--playback-props", render_props(spec.playback_props)]
    return argv


class PwLoopbackFactory:
    def __init__(self, launcher):
        self._launcher = launcher

    def create(self, spec: LoopbackSpec):
        return self._launcher.spawn_writer(loopback_argv(spec))


class WpctlVolumeControl:
    def set_volume(self, object_id: int, fraction: float) -> bool:
        """Returns False rather than raising.

        `object_id` is PipeWire's global object.id, not object.serial - wpctl
        resolves against the id. See docs/experiments/01-tap-volume.md.

        This runs inside the playout loop on every duck transition. A raise
        here would kill playout for the rest of the call over a node that
        momentarily went away.
        """
        try:
            result = subprocess.run(
                [require_tool(WPCTL), "set-volume", str(object_id), f"{fraction:.2f}"],
                capture_output=True,
                text=True,
                timeout=VOLUME_TIMEOUT_S,
            )
        except (MissingToolError, subprocess.SubprocessError, OSError):
            return False
        if result.returncode != 0:
            log.warning(
                "wpctl set-volume %s %.2f failed (%s) - the duck will not "
                "engage, so the original will be audible under the "
                "translation",
                object_id,
                fraction,
                (result.stderr or "").strip(),
            )
            return False
        return True


class PwCatSink:
    """An AudioSink backed by one long-lived pw-cat --playback."""

    def __init__(self, launcher, *, target: int | None, rate: int):
        self._process = launcher.spawn_writer(pwcat_argv(target=target, rate=rate))
        self.failed = False
        self._shrink_pipe()

    def _shrink_pipe(self) -> None:
        """Cap the stdin pipe so utterances are not queued behind a second.

        Best-effort: a kernel that refuses F_SETPIPE_SZ, or a fake in tests
        whose stdin is not a real pipe, costs latency rather than correctness.
        """
        try:
            fcntl.fcntl(self._process.stdin.fileno(), F_SETPIPE_SZ, PIPE_BYTES)
        except (OSError, AttributeError, ValueError) as exc:
            log.debug("could not shrink the playback pipe (%s); latency will be higher", exc)

    def write(self, pcm: bytes) -> None:
        if self.failed:
            return
        try:
            self._process.stdin.write(pcm)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            # pw-cat died. Playout must keep draining its queue rather than
            # deadlocking; the health flag is what the TUI turns red.
            self.failed = True
            log.error("playout sink died: %s", self._process.stderr_text())

    def close(self) -> None:
        self._process.terminate()
