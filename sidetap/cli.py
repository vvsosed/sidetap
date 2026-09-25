"""Command line entry point and wiring."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from .adapters import (
    MissingToolError,
    PwDumpGraphSource,
    PwLinkLinker,
    SubprocessLauncher,
    SystemClock,
)
from .capture import CaptureError
from .types import LAG_CAP_S
from .tts import GENDERS, MAX_SPEAKING_RATE, MIN_SPEAKING_RATE
from .graph import PLAYBACK_STREAM, SINK, SOURCE, PwGraph
from .ports import Clock, GraphSource, Linker, ProcessLauncher

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidetap",
        description="Real-time two-way voice interpretation for any call.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    devices = sub.add_parser(
        "devices", help="list sinks, sources and apps currently playing audio"
    )
    # Every subcommand takes -v: main() uses it to choose between one clean
    # line and a full traceback.
    devices.add_argument("-v", "--verbose", action="store_true")

    doctor = sub.add_parser("doctor", help="check the environment before a call")
    doctor.add_argument(
        "--install", action="store_true", help="write the virtual-mic config if absent"
    )
    doctor.add_argument(
        "--repair", action="store_true", help="restore the audio graph after a crash"
    )
    doctor.add_argument(
        "--no-api-check", action="store_true", help="skip the three cloud API checks"
    )
    doctor.add_argument("--project", help="GCP project id")
    # Three regions, because the three services accept different ones.
    # Defaults match `run`.
    doctor.add_argument("--region", default="europe-west4", help="Speech-to-Text region")
    doctor.add_argument("--mt-region", default="global", help="Translation region")
    doctor.add_argument("--tts-region", default="eu", help="Text-to-Speech region")
    # The languages you will run with: without them doctor only proves the API
    # is reachable, not that the model serves those locales.
    doctor.add_argument("--their-lang", metavar="BCP47", help="check this language too")
    doctor.add_argument("--my-lang", metavar="BCP47", help="check this language too")
    doctor.add_argument("--model", default="chirp_2", help="Speech-to-Text model")
    doctor.add_argument("-v", "--verbose", action="store_true")

    run = sub.add_parser("run", help="start interpreting")

    sources = run.add_argument_group("audio sources")
    sources.add_argument(
        "--app",
        metavar="NAME",
        required=True,
        help="tap this application's audio (e.g. zoom, viber). "
        "Run `sidetap devices` mid-call to see the options.",
    )
    sources.add_argument("--mic", help="microphone node name or substring")
    sources.add_argument(
        "--latency", default="100ms", help="PipeWire stream latency (default 100ms)"
    )

    langs = run.add_argument_group("languages")
    langs.add_argument(
        "--their-lang", required=True, metavar="BCP47",
        help="what the remote party speaks, e.g. ru-RU",
    )
    langs.add_argument(
        "--my-lang", required=True, metavar="BCP47",
        help="what you speak, e.g. en-US",
    )
    # A voice and a gender are mutually exclusive rather than one silently
    # winning. A warning would not help, since nothing logged is visible under
    # the TUI; argparse rejects the pair before any cloud call.
    voice_in = langs.add_mutually_exclusive_group()
    voice_in.add_argument(
        "--voice-in", default="", metavar="VOICE",
        help="Chirp 3 HD voice you hear, in --my-lang "
        "(default: picked from --my-lang)",
    )
    voice_in.add_argument(
        "--voice-in-gender", type=str.lower, choices=GENDERS, default=None,
        help="pick the voice YOU hear by gender instead of naming it",
    )
    voice_out = langs.add_mutually_exclusive_group()
    voice_out.add_argument(
        "--voice-out", default="", metavar="VOICE",
        help="Chirp 3 HD voice they hear, in --their-lang "
        "(default: picked from --their-lang)",
    )
    voice_out.add_argument(
        "--voice-out-gender", type=str.lower, choices=GENDERS, default=None,
        help="pick the voice THEY hear by gender instead of naming it",
    )

    cloud = run.add_argument_group("cloud")
    cloud.add_argument("--project", help="GCP project id")
    cloud.add_argument(
        "--region",
        default="europe-west4",
        help="Speech-to-Text region. NOT europe-west3: chirp_2 does not exist "
        "there, and the models that do (long, short) reject ru-RU.",
    )
    cloud.add_argument(
        "--mt-region",
        default="global",
        choices=("global", "us-central1"),
        help="Cloud Translation region. NOT the same as --region: Translation "
        "does not exist in europe-west3. us-central1 measured marginally "
        "faster from Europe.",
    )
    cloud.add_argument(
        "--mt-model",
        default="general/translation-llm",
        help="general/translation-llm (better on idiom) or general/nmt "
        "(~195 ms faster). Falls back to nmt automatically on error.",
    )
    # Per direction, with no shared flag: the directions translate opposite
    # ways, so their useful rates are inverses (a pair running 1.23x one way
    # runs about 0.81x the other).
    cloud.add_argument(
        "--speaking-rate-in",
        type=speaking_rate,
        default=1.0,
        metavar="RATE",
        help="how fast the voice YOU hear speaks (%s-%s, default 1.0). Raise "
        "it when the language you hear is wordier than the one it came from: "
        "Russian takes about 1.23x as long to say as the English behind it, "
        "so at 1.0 a continuous speaker builds a backlog that never drains, "
        "and around 1.3 it does." % (MIN_SPEAKING_RATE, MAX_SPEAKING_RATE),
    )
    cloud.add_argument(
        "--speaking-rate-out",
        type=speaking_rate,
        default=1.0,
        metavar="RATE",
        help="how fast the voice THEY hear speaks (%s-%s, default 1.0)"
        % (MIN_SPEAKING_RATE, MAX_SPEAKING_RATE),
    )
    cloud.add_argument(
        "--tts-region", default="eu",
        help="TTS region; Frankfurt has no TTS single-region, so this is the "
        "eu multi-region by default",
    )
    cloud.add_argument(
        "--model",
        default="chirp_2",
        help="Speech-to-Text model. chirp_3 is no longer generally available "
        "and returns 403 for every locale.",
    )
    cloud.add_argument(
        "--phrase", action="append", dest="phrases",
        help="boost a term (names, jargon); repeat as needed",
    )

    out = run.add_argument_group("output")
    out.add_argument("--out", type=Path, default=Path("transcripts"))
    # default=None, so run.py can tell "not passed" from a value and LAG_CAP_S
    # stays the single source; the help text is derived from it too.
    out.add_argument(
        "--lag-cap",
        type=float,
        default=None,
        metavar="SECONDS",
        help="seconds of un-spoken translation to allow before dropping the "
        f"oldest (default {LAG_CAP_S:g}). Raising it means hearing more while "
        "falling further behind; it does not stop the backlog growing.",
    )
    out.add_argument(
        "--no-early-commit",
        action="store_true",
        help="wait for a complete utterance before translating, instead of "
        "committing a stable prefix part-way through. Early committing only "
        "engages after roughly 11 s of continuous speech, so this changes "
        "nothing for ordinary conversation.",
    )
    out.add_argument("--no-tui", action="store_true", help="plain console logging")
    out.add_argument("-v", "--verbose", action="store_true")
    return parser


# Only a fallback, for when --out cannot be written to. A session's log
# normally lands beside its own transcript.
FALLBACK_LOG_PATH = Path.home() / ".local/state/sidetap/sidetap.log"
LOG_PATH = FALLBACK_LOG_PATH


def session_name() -> str:
    """The stem shared by a session's .log, .jsonl and .md.

    One name for all three so a run's artifacts sort together and a log line
    can be lined up against the utterance it explains. Sub-second resolution
    because two runs started in the same second would otherwise collide.
    """
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _configure_logging(args, level: int) -> tuple[Path | None, str | None]:
    """stderr, unless Textual is about to take the terminal away.

    Textual paints over the whole screen, destroying every line logged to
    stderr - including "this direction is now dead", the one line that
    explains why nothing is being translated.

    A `run` also writes a log file beside its transcript, named after the
    same session, so a call leaves one set of files that explain each other.
    Returns that path and the session name.
    """
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    # -v raises sidetap's verbosity, not the whole process's: a DEBUG root
    # would bury sidetap's lines under urllib3, asyncio and grpc chatter.
    # Third-party WARNING and above still come through.
    root.setLevel(logging.WARNING)
    logging.getLogger("sidetap").setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if args.cmd != "run":
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(stream)
        return None, None

    # Under the TUI a stderr handler writes into a terminal being painted
    # over. Without it, stderr is the live copy and the file the durable one.
    if getattr(args, "no_tui", False):
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(stream)

    session = session_name()
    for candidate in (Path(args.out) / f"{session}.log", FALLBACK_LOG_PATH):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(candidate, encoding="utf-8")
        except OSError:
            continue
        handler.setFormatter(fmt)
        root.addHandler(handler)
        return candidate, session

    if not root.handlers:
        # Never leave a run with nowhere to report a failure.
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        root.addHandler(stream)
    return None, session


def speaking_rate(value: str) -> float:
    """Reject an out-of-range rate at parse time, not at the first utterance.

    The service returns OutOfRange only once audio is already flowing, which
    on a live call means the translation simply never arrives.
    """
    try:
        rate = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    if not MIN_SPEAKING_RATE <= rate <= MAX_SPEAKING_RATE:
        raise argparse.ArgumentTypeError(
            f"speaking rate must be between {MIN_SPEAKING_RATE} and "
            f"{MAX_SPEAKING_RATE}; got {rate}"
        )
    return rate


def describe_graph(graph: PwGraph) -> str:
    """Human-readable graph summary. Run this while your call is live."""
    lines: list[str] = ["", "=== OUTPUT DEVICES (sinks) ==="]
    for node in graph.by_class(SINK):
        default = " [default]" if node.name == graph.default_sink else ""
        lines.append(f"  serial={node.serial:<6} {node.label}{default}")
        lines.append(f"      node.name = {node.name}")

    lines += ["", "=== INPUT DEVICES (sources / microphones) ==="]
    for node in graph.by_class(SOURCE):
        if node.name.endswith(".monitor"):
            continue
        default = " [default]" if node.name == graph.default_source else ""
        lines.append(f"  serial={node.serial:<6} {node.label}{default}")
        lines.append(f"      node.name = {node.name}")

    lines += ["", "=== APPLICATIONS CURRENTLY PLAYING AUDIO ==="]
    streams = graph.by_class(PLAYBACK_STREAM)
    if not streams:
        lines.append("  (none - start your Zoom/Viber call, then run this again)")
    for node in streams:
        lines.append(
            f"  serial={node.serial:<6} {node.app_name or '?'}"
            f"  binary={node.app_binary or '?'}  pid={node.pid}"
        )
        lines.append(f"      --app '{node.app_binary or node.app_name}'")
    lines.append("")
    return "\n".join(lines)


def _doctor(args, graph: GraphSource, launcher, linker, clock) -> int:
    from .doctor import (
        check_apis,
        check_credentials,
        check_linking,
        check_pipewire_version,
        check_tools,
        check_vad,
        check_virtmic,
        install_virtmic_config,
        render_report,
    )
    from .routing import Router

    if args.install:
        if install_virtmic_config():
            print(
                "Wrote the virtual-mic config. Apply it with:\n"
                "  systemctl --user restart pipewire pipewire-pulse\n"
            )
        else:
            print("Virtual-mic config already exists; left untouched.\n")

    if args.repair:
        from .adapters import PwLoopbackFactory

        repaired = Router(
            graph=graph,
            linker=linker,
            unlinker=linker,
            loopbacks=PwLoopbackFactory(launcher),
        ).repair()
        print("Repaired the audio graph.\n" if repaired else "Nothing to repair.\n")

    checks = [check_pipewire_version(), *check_tools()]
    checks.append(check_virtmic(graph.snapshot()))
    checks.append(check_linking())
    checks.append(check_vad())
    credentials = check_credentials(args.project)
    checks.append(credentials)
    if not args.no_api_check and credentials.ok:
        # check_credentials is only ok once one of these is set; the fallback
        # just avoids a KeyError crash.
        project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        languages = tuple(
            lang for lang in (args.their_lang, args.my_lang) if lang
        )
        checks.extend(
            check_apis(
                project,
                args.region,
                args.mt_region,
                args.tts_region,
                model=args.model,
                languages=languages,
            )
        )
        if not languages:
            print(
                "  note: pass --their-lang and --my-lang to check that the "
                "model actually serves them.\n"
            )

    print(render_report(checks))
    return 0 if all(c.ok for c in checks) else 1


def main(
    argv: list[str] | None = None,
    *,
    graph: GraphSource | None = None,
    launcher: ProcessLauncher | None = None,
    linker: Linker | None = None,
    clock: Clock | None = None,
    recognizer_factory=None,
    translator=None,
    synthesizer=None,
) -> int:
    args = build_parser().parse_args(argv)

    level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    log_path, session = _configure_logging(args, level)

    graph = graph or PwDumpGraphSource()
    launcher = launcher or SubprocessLauncher()
    linker = linker or PwLinkLinker()
    clock = clock or SystemClock()

    try:
        if args.cmd == "devices":
            print(describe_graph(graph.snapshot()))
            return 0
        if args.cmd == "doctor":
            return _doctor(args, graph, launcher, linker, clock)
        from .run import run_session

        if log_path is not None:
            print(f"Log: {log_path}")
        return run_session(
            args,
            graph=graph,
            launcher=launcher,
            linker=linker,
            clock=clock,
            session=session,
            recognizer_factory=recognizer_factory,
            translator=translator,
            synthesizer=synthesizer,
        )
    except KeyboardInterrupt:
        # Ctrl-C before the handler is installed, e.g. during auth.
        return 130
    except json.JSONDecodeError as exc:
        # Not a RuntimeError, so the clause below would miss it and the user
        # would get a traceback instead of one clean line.
        print(
            f"Could not parse pw-dump output ({exc}). Is PipeWire running? "
            "Check with: pw-dump | head",
            file=sys.stderr,
        )
        return 1
    except (CaptureError, MissingToolError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # Google auth and gRPC, a dead pw-dump, a full disk: none subclass
        # RuntimeError, and they are exactly what a first run hits. -v
        # re-raises for the real traceback.
        if getattr(args, "verbose", False):
            raise
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
