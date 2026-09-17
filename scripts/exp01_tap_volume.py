"""Experiment 1: is an additive pw-link tap pre- or post-volume?

If post-volume, ducking the application's own stream volume would also
silence the recogniser, and routing.py must own a separate duck node.
If pre-volume, routing.py can collapse to a single wpctl call.

Usage, with an application playing audio:

    uv run python scripts/exp01_tap_volume.py --app zoom
"""

from __future__ import annotations

import argparse
import array
import math
import subprocess
import time

from sidetap.adapters import PwDumpGraphSource, PwLinkLinker, SubprocessLauncher
from sidetap.graph import PLAYBACK_STREAM
from sidetap.recorder import Recorder, RecorderSpec


def rms(pcm: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(pcm)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def measure(recorder: Recorder, seconds: float) -> float:
    """Mean RMS over `seconds` of captured blocks."""
    deadline = time.monotonic() + seconds
    values = []
    for block in recorder.blocks():
        values.append(rms(block))
        if time.monotonic() > deadline:
            break
    return sum(values) / len(values) if values else 0.0


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

    snapshot = graph_source.snapshot()
    node = snapshot.node_by_name(recorder.node_name)
    assert node is not None, "capture node never appeared"
    inputs = snapshot.ports_of(node.id, "in")

    stream = next(
        s for s in snapshot.by_class(PLAYBACK_STREAM) if s.matches(args.app)
    )
    for i, out_port in enumerate(snapshot.ports_of(stream.id, "out")):
        linker.link(out_port.id, inputs[min(i, len(inputs) - 1)].id)

    print(f"tapping {stream.label} (serial={stream.serial})")
    time.sleep(1.0)

    loud = measure(recorder, 5.0)
    print(f"RMS at full volume: {loud:.1f}")

    subprocess.run(["wpctl", "set-volume", str(stream.serial), "0"], check=True)
    time.sleep(1.0)
    quiet = measure(recorder, 5.0)
    subprocess.run(["wpctl", "set-volume", str(stream.serial), "1.0"], check=True)
    recorder.stop()

    print(f"RMS at zero volume: {quiet:.1f}")
    ratio = quiet / loud if loud else 0.0
    verdict = "POST-volume (duck node REQUIRED)" if ratio < 0.1 else "PRE-volume"
    print(f"\nratio={ratio:.3f} -> tap is {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
