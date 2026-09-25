"""Check the environment before a call, not during one.

A disabled API should fail in a second at setup, not two minutes into a
conversation with the other party waiting.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Sequence
from pathlib import Path

from .adapters import MIN_PW_VERSION, installed_pw_version
from .graph import PwGraph
from .routing import VIRTMIC_CONFIG, VIRTMIC_CONFIG_PATH, VIRTMIC_SINK, VIRTMIC_SOURCE

# pw-cli is never used at runtime, but check_pipewire_version() needs it;
# without it here, a machine missing only pw-cli is told to upgrade PipeWire.
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
        # installed_pw_version()'s sentinel for "pw-cli missing or failed",
        # not an ancient PipeWire; say so rather than suggest an upgrade.
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


def check_virtmic(graph: PwGraph, config_path: Path = VIRTMIC_CONFIG_PATH) -> Check:
    """Are both halves of the virtual mic present in the live graph?

    The config file is consulted to tell "not installed" from "installed but
    not loaded": they need different actions, and telling a user who just ran
    `--install` to run it again makes the tool look broken.
    """
    sink = graph.node_by_name(VIRTMIC_SINK)
    source = graph.node_by_name(VIRTMIC_SOURCE)
    if sink is not None and source is not None:
        return Check("virtual mic", True, f"{VIRTMIC_SINK} + {VIRTMIC_SOURCE}")

    missing = [
        name
        for name, node in ((VIRTMIC_SINK, sink), (VIRTMIC_SOURCE, source))
        if node is None
    ]
    if config_path.exists():
        return Check(
            "virtual mic",
            False,
            f"config is written but not loaded yet - run: "
            f"systemctl --user restart pipewire pipewire-pulse",
        )
    return Check(
        "virtual mic",
        False,
        f"missing {', '.join(missing)} - run `sidetap doctor --install` then "
        "`systemctl --user restart pipewire pipewire-pulse`",
    )


def check_linking() -> Check:
    """Does pw-link actually reach a running PipeWire session?

    check_tools() only proves the binary is on PATH. A pw-link that cannot
    talk to a session (none running, wrong XDG_RUNTIME_DIR, a sandbox) fails
    at runtime almost silently, and the IN direction never translates
    anything. `pw-link -l` needs a live session, so it is a cheap probe.
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
    environment, and a silent one: vad.py falls back to a gate that allows
    everything, streaming all audio in both directions to a metered API.
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

    Asking google.auth is the same question the SDK will ask, so
    `gcloud auth application-default login`, which sets no
    GOOGLE_APPLICATION_CREDENTIALS, counts too.

    The project is NOT taken from ADC even when ADC offers one: ADC's project
    is whatever gcloud was last pointed at, which is routinely not the one
    with these APIs enabled. Using it would fail with a confusing 403, or
    succeed and bill the wrong project.
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
        # Worth saying: quota and billing follow this, and a mismatch means a
        # 403 naming a project the user never typed.
        detail += f" (ADC defaults to {adc_project})"
    return Check("credentials", True, detail)


def check_apis(
    project_id: str,
    asr_region: str,
    mt_region: str,
    tts_region: str,
    model: str = "chirp_2",
    languages: Sequence[str] = (),
) -> list[Check]:
    """One real, cheap RPC per API. Imported lazily.

    Constructing a client proves nothing: it builds a channel without
    contacting the service, so a DISABLED API only fails on the first real
    call. Each probe is a free list/metadata call (under ~1.5 s measured) that
    returns PermissionDenied with SERVICE_DISABLED when the API is off.

    Each API gets its own region, because they accept different ones (see
    docs/experiments/03-translation-llm.md).
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

    checks.extend(check_asr_model(project_id, asr_region, model, languages))

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


def check_asr_model(
    project_id: str, region: str, model: str, languages: Sequence[str] = ()
) -> list[Check]:
    """Will this exact model actually recognise these exact languages here?

    Reaching the API is a different question: `list_recognizers` succeeds
    even when the configured model is withdrawn or rejects the language, and
    then every call dies on its first block. `chirp_3` returns 403 everywhere,
    and `long` in europe-west3 accepts en-US but returns 400 for ru-RU, so
    each language is checked separately.

    Costs one short stream of silence per language, about two seconds.
    """
    from .asr import AsrConfig, build_recognizer_factory
    from .types import BLOCK_BYTES, Direction

    checks = []
    for language in languages:
        label = f"asr {model}/{language}"
        try:
            from .rotation import AudioTimeline

            config = AsrConfig(
                project_id=project_id,
                region=region,
                model=model,
                language_code=language,
            )
            recognizer = build_recognizer_factory(config, Direction.IN)(
                AudioTimeline(0.0)
            )
            for _ in recognizer.stream(
                (b"\x00" * BLOCK_BYTES for _ in range(5))
            ):
                pass
            checks.append(Check(label, True, region))
        except Exception as exc:
            checks.append(Check(label, False, f"{region}: {_explain(exc)}"))
    return checks


def _explain(exc: Exception) -> str:
    """Turn a Google error into the sentence that names the fix.

    The 403's message already names the problem, but it arrives wrapped in
    SDK types, and the console URL in its metadata is what the user needs.
    """
    from google.api_core import exceptions as gexc

    if isinstance(exc, gexc.PermissionDenied):
        # Two different failures arrive as the same 403. A missing ADC quota
        # project ("the API requires a quota project") reads like a disabled
        # API but needs a different fix. Re-running `gcloud auth
        # application-default login` drops the quota project, after which
        # Translation and Text-to-Speech 403 while Speech-to-Text, called with
        # an explicit parent, keeps working.
        if "generally available" in exc.message:
            # The model itself is withdrawn: enabling anything cannot fix it,
            # and the console page will look correct.
            return (
                f"{exc.message} - this model has been withdrawn, so enabling "
                "anything will not help. Use --model chirp_2 with "
                "--region europe-west4."
            )
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

    Never clobbers: the user may have tuned the rate or the description.
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
