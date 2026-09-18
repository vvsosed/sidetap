"""Check the environment before a call, not during one.

One of three APIs not being enabled should fail in a second at setup, rather
than two minutes into a conversation with the other party waiting.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .adapters import MIN_PW_VERSION, installed_pw_version
from .graph import PwGraph
from .routing import VIRTMIC_CONFIG, VIRTMIC_CONFIG_PATH, VIRTMIC_SINK, VIRTMIC_SOURCE

# pw-cli belongs here even though sidetap never uses it at runtime:
# check_pipewire_version() shells out to it, and without it in this list a
# machine missing only pw-cli is told PipeWire is version 0.0.0 and to
# upgrade - sending the user after a problem they do not have.
REQUIRED_TOOLS = (
    "pw-dump",
    "pw-record",
    "pw-link",
    "pw-cat",
    "pw-loopback",
    "pw-cli",
    "wpctl",
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def check_tools() -> list[Check]:
    checks = []
    for tool in REQUIRED_TOOLS:
        path = shutil.which(tool)
        checks.append(Check(tool, path is not None, path or "not found"))
    return checks


def check_pipewire_version() -> Check:
    version = installed_pw_version()
    rendered = ".".join(str(p) for p in version)
    minimum = ".".join(str(p) for p in MIN_PW_VERSION)
    if version == (0, 0, 0):
        # Not a real version. installed_pw_version() returns this sentinel
        # when pw-cli is missing or will not run, and reporting it as though
        # PipeWire were ancient points the user at an upgrade rather than at
        # the actual problem.
        return Check(
            "pipewire",
            False,
            "could not read the version - is pw-cli installed and is PipeWire "
            "running for this user?",
        )
    return Check(
        "pipewire",
        version >= MIN_PW_VERSION,
        rendered if version >= MIN_PW_VERSION else f"{rendered}, need >= {minimum}",
    )


def check_virtmic(graph: PwGraph) -> Check:
    sink = graph.node_by_name(VIRTMIC_SINK)
    source = graph.node_by_name(VIRTMIC_SOURCE)
    if sink is not None and source is not None:
        return Check("virtual mic", True, f"{VIRTMIC_SINK} + {VIRTMIC_SOURCE}")

    missing = [
        name
        for name, node in ((VIRTMIC_SINK, sink), (VIRTMIC_SOURCE, source))
        if node is None
    ]
    return Check(
        "virtual mic",
        False,
        f"missing {', '.join(missing)} - run `sidetap doctor --install` then "
        "`systemctl --user restart pipewire pipewire-pulse`",
    )


def check_linking() -> Check:
    """Does pw-link actually reach a running PipeWire session?

    check_tools() only proves the binary is on PATH. A pw-link that exists but
    cannot talk to a session - no session running, wrong XDG_RUNTIME_DIR, a
    sandbox - fails at runtime as ONE warning about six seconds in, then
    debug-level forever, while the IN direction silently never produces a
    translation for the rest of the call. `pw-link -l` needs a live session, so
    it is a cheap functional probe.
    """
    try:
        result = subprocess.run(
            [shutil.which("pw-link") or "pw-link", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("linking", False, f"pw-link unusable: {type(exc).__name__}: {exc}")
    if result.returncode != 0:
        return Check(
            "linking",
            False,
            f"pw-link -l failed ({(result.stderr or '').strip()}) - is PipeWire "
            "running for this user? Without it the remote direction stays deaf.",
        )
    return Check("linking", True, "pw-link reaches the session")


def check_vad() -> Check:
    """Is the silence gate actually going to gate?

    webrtcvad-wheels is a hard dependency, so a failed import means a broken
    environment - and the failure is silent: vad.py degrades to a None
    detector that allows everything, so BOTH directions stream 100% of their
    audio to a metered API for the length of the call. That is the cost
    surprise doctor exists to catch in a second at setup rather than two
    minutes into a conversation.
    """
    try:
        import webrtcvad  # noqa: F401
    except ImportError as exc:
        return Check(
            "silence gate",
            False,
            f"webrtcvad unavailable ({exc}) - silence would be streamed too, "
            "roughly doubling cost on both directions. Run: uv sync",
        )
    return Check("silence gate", True, "webrtcvad available")


def check_credentials(project_override: str | None = None) -> Check:
    """Credentials from ANY Application Default Credentials source.

    Requiring GOOGLE_APPLICATION_CREDENTIALS was wrong: `gcloud auth
    application-default login` is how a desktop user normally authenticates
    and it sets no such variable, so doctor reported FAIL on a working machine
    and then skipped the three API checks that actually matter. Asking
    google.auth for credentials is the same question the SDK will ask.

    The project is NOT taken from ADC even when ADC offers one. ADC's project
    is whatever gcloud was last pointed at, which is routinely not the project
    with these three APIs enabled - silently using it means every call fails
    with a confusing 403, or succeeds and bills the wrong project.
    """
    try:
        import google.auth

        credentials, adc_project = google.auth.default()
    except Exception as exc:
        return Check(
            "credentials",
            False,
            f"no usable credentials ({type(exc).__name__}) - run "
            "`gcloud auth application-default login`, or point "
            "GOOGLE_APPLICATION_CREDENTIALS at a service account key",
        )

    project = project_override or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        hint = f" (ADC offers {adc_project!r}, which may not be the right one)" if adc_project else ""
        return Check(
            "credentials",
            False,
            f"no project - pass --project or set GOOGLE_CLOUD_PROJECT{hint}",
        )

    detail = f"project={project}"
    if adc_project and adc_project != project:
        # Worth saying out loud: quota and billing attribution follow this,
        # and a mismatch is the difference between a working call and a 403
        # that names a project the user never typed.
        detail += f" (ADC defaults to {adc_project})"
    return Check("credentials", True, detail)


def check_apis(
    project_id: str, asr_region: str, mt_region: str, tts_region: str
) -> list[Check]:
    """One real, cheap RPC per API. Imported lazily.

    Constructing a client proves nothing: it resolves credentials and builds a
    channel without contacting the service, so a project with the API DISABLED
    constructs a client quite happily and then fails on the first real call -
    two minutes into a conversation, which is the exact failure this command
    exists to move to setup. Each probe below is a list/metadata call: free,
    under ~1.5 s measured, and returns PermissionDenied with SERVICE_DISABLED
    when the API is off.

    Each API gets ITS OWN region. They are genuinely different settings -
    Cloud Translation rejects europe-west3 outright (see
    docs/experiments/03-translation-llm.md), and passing one region to all
    three turned a working setup into a failing check.
    """
    checks = []

    try:
        from google.api_core.client_options import ClientOptions
        from google.cloud.speech_v2 import SpeechClient

        from .asr import speech_endpoint

        client = SpeechClient(
            client_options=ClientOptions(api_endpoint=speech_endpoint(asr_region))
        )
        client.list_recognizers(
            parent=f"projects/{project_id}/locations/{asr_region}"
        )
        checks.append(Check("speech-to-text", True, asr_region))
    except Exception as exc:
        checks.append(
            Check("speech-to-text", False, f"{asr_region}: {_explain(exc)}")
        )

    try:
        from google.cloud import translate

        client = translate.TranslationServiceClient()
        client.get_supported_languages(
            parent=f"projects/{project_id}/locations/{mt_region}"
        )
        checks.append(Check("translation", True, mt_region))
    except Exception as exc:
        checks.append(Check("translation", False, f"{mt_region}: {_explain(exc)}"))

    try:
        from google.api_core.client_options import ClientOptions
        from google.cloud import texttospeech

        from .tts import tts_endpoint

        client = texttospeech.TextToSpeechClient(
            client_options=ClientOptions(api_endpoint=tts_endpoint(tts_region))
        )
        client.list_voices()
        checks.append(Check("text-to-speech", True, tts_region))
    except Exception as exc:
        checks.append(
            Check("text-to-speech", False, f"{tts_region}: {_explain(exc)}")
        )

    return checks


def _explain(exc: Exception) -> str:
    """Turn a Google error into the sentence that names the fix.

    "403 Cloud Speech-to-Text API has not been used in project 123 before or
    it is disabled" is already the answer, but it arrives wrapped in a stack
    of SDK types; and the console URL buried in its metadata is the one thing
    the user actually needs to click.
    """
    from google.api_core import exceptions as gexc

    if isinstance(exc, gexc.PermissionDenied):
        # Two very different failures arrive as the same 403, and the message
        # for one reads like the other. A missing ADC quota project says "the
        # API requires a quota project", which a reader skims as "the API is
        # not enabled" and then spends an afternoon re-enabling an API that
        # was never off. Observed live: re-running `gcloud auth
        # application-default login` drops the quota project, after which
        # Translation and Text-to-Speech both 403 while Speech-to-Text keeps
        # working - because that one is called with an explicit parent.
        if "quota project" in exc.message:
            return (
                f"{exc.message} - this is NOT a disabled API. Run: gcloud auth "
                "application-default set-quota-project <your-project>"
            )
        return f"{exc.message} - enable it, then re-run `sidetap doctor`"
    if isinstance(exc, gexc.Unauthenticated):
        return f"{exc.message} - run `gcloud auth application-default login`"
    if isinstance(exc, gexc.InvalidArgument):
        return f"{exc.message} - check the region for this API"
    return f"{type(exc).__name__}: {exc}"


def install_virtmic_config(path: Path = VIRTMIC_CONFIG_PATH) -> bool:
    """Write the config if absent. Returns True if it wrote one.

    Never clobbers: the user may have tuned the rate or the description, and
    silently reverting that would be worse than doing nothing.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(VIRTMIC_CONFIG, encoding="utf-8")
    return True


def render_report(checks: list[Check]) -> str:
    if not checks:
        return "  No checks ran."
    width = max((len(c.name) for c in checks), default=0)
    lines = [f"  {'OK ' if c.ok else 'FAIL'}  {c.name:<{width}}  {c.detail}" for c in checks]
    if all(c.ok for c in checks):
        lines.append("")
        lines.append("  All checks passed.")
    return "\n".join(lines)
