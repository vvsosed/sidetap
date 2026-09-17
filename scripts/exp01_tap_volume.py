"""Experiment 1: is an additive pw-link tap pre- or post-volume?

If post-volume, ducking the application's own stream volume would also
silence the recogniser, and routing.py must own a separate duck node.
If pre-volume, routing.py can collapse to a single wpctl call.

This script gets exactly one run, weeks from now, on live hardware, and its
verdict decides whether Task 20 keeps a crash-recovery journal or collapses
to one wpctl call. It is written defensively on purpose: every measurement
either produces a trustworthy number or fails loudly, it never leaves the
tapped application muted, and it always releases the pw-record process it
starts.

Usage, with an application playing audio:

    uv run python scripts/exp01_tap_volume.py --app zoom
"""

from __future__ import annotations

import argparse
import array
import math
import subprocess
import sys
import threading
import time

from sidetap.adapters import PwDumpGraphSource, PwLinkLinker, SubprocessLauncher
from sidetap.graph import PLAYBACK_STREAM
from sidetap.ports import LinkResult
from sidetap.recorder import Recorder, RecorderSpec


def rms(pcm: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(pcm)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def measure(
    recorder: Recorder, seconds: float, hard_cap: float | None = None
) -> tuple[float, int, float]:
    """Mean RMS over `seconds` of captured blocks.

    Returns (mean_rms, block_count, elapsed_seconds). Always returns within
    roughly `hard_cap` wall-clock seconds (default: `seconds + 3.0`), even if
    `recorder.blocks()` never yields a single block.

    This matters because `Recorder.blocks()` performs a blocking `read()`
    with no timeout, and an unlinked or dead pw-record capture produces ZERO
    BYTES rather than silence -- so the "am I past the deadline" check below,
    which only runs *after* a block has been yielded, would otherwise never
    run at all in the most likely failure mode. The watchdog timer is the
    real bound: it force-stops the recorder after `hard_cap`, which unblocks
    the read and ends the generator, so this function always returns.
    """
    hard_cap = seconds + 3.0 if hard_cap is None else hard_cap
    start = time.monotonic()
    deadline = start + seconds

    def _force_stop() -> None:
        # Best-effort: if this races with a clean finish, cancel() below
        # will already have disarmed it. If recorder.stop() itself misbehaves,
        # that is surfaced downstream by recorder.failure() / a zero block
        # count, not by crashing this background thread.
        try:
            recorder.stop()
        except Exception:
            pass

    watchdog = threading.Timer(hard_cap, _force_stop)
    watchdog.daemon = True
    watchdog.start()

    values: list[float] = []
    try:
        if time.monotonic() >= deadline:
            return 0.0, 0, 0.0
        for block in recorder.blocks():
            values.append(rms(block))
            if time.monotonic() >= deadline:
                break
    finally:
        watchdog.cancel()

    elapsed = time.monotonic() - start
    mean = sum(values) / len(values) if values else 0.0
    return mean, len(values), elapsed


def recorder_alive_or_report(recorder: Recorder, block_count: int, phase: str) -> bool:
    """False (after printing why) if this measurement cannot be trusted.

    A reader of this script's output must be able to tell "the tap is
    post-volume" from "the measurement never happened" -- an empty reading
    and a genuine silent one print identically unless this check runs and
    aborts before a fake verdict is printed.
    """
    failure = recorder.failure()
    if failure is not None:
        print(
            f"ERROR: pw-record failed during the {phase!r} measurement: {failure}",
            file=sys.stderr,
        )
        return False
    if block_count == 0:
        print(
            f"ERROR: captured zero blocks during the {phase!r} measurement. "
            "An unlinked or dead capture yields no bytes at all, not silence "
            "-- this is NOT a valid reading and must not be reported as one.",
            file=sys.stderr,
        )
        return False
    return True


def get_volume(serial: int) -> str:
    """Return the raw fraction token from `wpctl get-volume`, e.g. "0.65".

    Feeding this straight back into `wpctl set-volume <serial> <token>` is
    the actual restore of whatever the user had set before this script
    touched anything -- never a hardcoded guess. `wpctl get-volume` also
    appends "[MUTED]" when the stream is muted; that is stripped here since
    `set-volume` does not accept it (mute state itself is not restored).
    """
    result = subprocess.run(
        ["wpctl", "get-volume", str(serial)],
        check=True,
        capture_output=True,
        text=True,
    )
    parts = result.stdout.split()
    if len(parts) < 2:
        raise RuntimeError(f"unexpected `wpctl get-volume` output: {result.stdout!r}")
    return parts[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    args = parser.parse_args()

    graph_source = PwDumpGraphSource()
    launcher = SubprocessLauncher()
    linker = PwLinkLinker()

    recorder = Recorder(
        RecorderSpec(track="probe", target=None, autoconnect=False), launcher
    )
    recorder.start()
    time.sleep(1.0)

    try:
        snapshot = graph_source.snapshot()
        node = snapshot.node_by_name(recorder.node_name)
        if node is None:
            print("ERROR: capture node never appeared", file=sys.stderr)
            return 1
        inputs = snapshot.ports_of(node.id, "in")
        if not inputs:
            print("ERROR: capture node has no input ports", file=sys.stderr)
            return 1

        candidates = [
            s for s in snapshot.by_class(PLAYBACK_STREAM) if s.matches(args.app)
        ]
        if not candidates:
            print(
                f"ERROR: no playback stream matches --app {args.app!r}",
                file=sys.stderr,
            )
            others = [s.label for s in snapshot.by_class(PLAYBACK_STREAM)]
            if others:
                print("Currently playing streams:", file=sys.stderr)
                for label in others:
                    print(f"  - {label}", file=sys.stderr)
            else:
                print("(no playback streams are active at all)", file=sys.stderr)
            return 1

        stream = candidates[0]
        if len(candidates) > 1:
            print(
                f"WARNING: {len(candidates)} streams matched --app {args.app!r}; "
                f"using {stream.label} (serial={stream.serial})",
                file=sys.stderr,
            )
            for other in candidates[1:]:
                print(
                    f"  (ignored) {other.label} (serial={other.serial})",
                    file=sys.stderr,
                )

        out_ports = snapshot.ports_of(stream.id, "out")
        link_failures = 0
        for i, out_port in enumerate(out_ports):
            result = linker.link(out_port.id, inputs[min(i, len(inputs) - 1)].id)
            if result is LinkResult.FAILED:
                link_failures += 1
        if link_failures:
            print(
                f"ERROR: {link_failures}/{len(out_ports)} ports failed to link "
                "-- the tap is only partially connected, RMS would be "
                "silently wrong",
                file=sys.stderr,
            )
            return 1

        original_volume = get_volume(stream.serial)
        print(
            f"tapping {stream.label} (serial={stream.serial}), "
            f"original volume={original_volume}"
        )
        print(
            "if this hangs or is interrupted, recover by hand with:\n"
            f"  wpctl set-volume {stream.serial} {original_volume}\n"
            "  pkill -f pw-record"
        )
        time.sleep(1.0)

        loud, loud_n, loud_elapsed = measure(recorder, 5.0)
        if not recorder_alive_or_report(recorder, loud_n, "full volume"):
            return 1
        print(
            f"RMS at full volume: {loud:.1f}  ({loud_n} blocks, "
            f"{loud_elapsed:.1f}s measured)"
        )

        try:
            subprocess.run(
                ["wpctl", "set-volume", str(stream.serial), "0"], check=True
            )
            time.sleep(1.0)
            quiet, quiet_n, quiet_elapsed = measure(recorder, 5.0)
        finally:
            # This restore must happen no matter what went wrong above --
            # a hang or a Ctrl-C must never leave the user's call muted.
            restore = subprocess.run(
                ["wpctl", "set-volume", str(stream.serial), original_volume],
                check=False,
            )
            if restore.returncode != 0:
                print(
                    "ERROR: failed to restore volume automatically. Run by "
                    f"hand: wpctl set-volume {stream.serial} {original_volume}",
                    file=sys.stderr,
                )

        if not recorder_alive_or_report(recorder, quiet_n, "zero volume"):
            return 1
        print(
            f"RMS at zero volume: {quiet:.1f}  ({quiet_n} blocks, "
            f"{quiet_elapsed:.1f}s measured)"
        )

        ratio = quiet / loud if loud else 0.0
        verdict = "POST-volume (duck node REQUIRED)" if ratio < 0.1 else "PRE-volume"
        print(f"\nratio={ratio:.3f} -> tap is {verdict}")
        return 0
    finally:
        # pw-record runs detached (SubprocessLauncher uses
        # start_new_session=True), so it survives a Ctrl-C as an orphan
        # unless we stop it here on every exit path.
        try:
            recorder.stop()
        except Exception as exc:
            print(f"WARNING: recorder.stop() raised during cleanup: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
