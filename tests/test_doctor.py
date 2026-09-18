from pathlib import Path

from sidetap.doctor import (
    check_pipewire_version,
    Check,
    check_credentials,
    check_linking,
    check_tools,
    check_vad,
    check_virtmic,
    install_virtmic_config,
    render_report,
)
from sidetap.routing import VIRTMIC_SINK, VIRTMIC_SOURCE


def test_a_check_renders_with_a_marker():
    report = render_report([Check("pipewire", True, "0.3.85"), Check("wpctl", False, "not found")])
    assert "pipewire" in report
    assert "0.3.85" in report
    assert "not found" in report


def test_the_report_says_overall_pass_only_when_everything_passed():
    assert "all checks passed" in render_report([Check("a", True, "")]).lower()
    assert "all checks passed" not in render_report([Check("a", False, "")]).lower()


def test_missing_tools_are_named_individually(monkeypatch):
    monkeypatch.setattr("sidetap.doctor.shutil.which", lambda name: None if name == "wpctl" else "/usr/bin/" + name)
    checks = check_tools()
    failed = [c for c in checks if not c.ok]
    assert len(failed) == 1
    assert failed[0].name == "wpctl"


def test_all_tools_present_passes(monkeypatch):
    monkeypatch.setattr("sidetap.doctor.shutil.which", lambda name: "/usr/bin/" + name)
    assert all(c.ok for c in check_tools())


def test_virtmic_check_wants_both_halves(idle_graph):
    # A sink with no source means the loopback half-loaded; the messenger sees
    # no microphone at all.
    check = check_virtmic(idle_graph)
    assert check.ok is False
    assert VIRTMIC_SOURCE in check.detail or VIRTMIC_SINK in check.detail


def test_virtmic_check_passes_when_both_nodes_exist(idle_graph):
    from dataclasses import replace

    from sidetap.graph import SINK, SOURCE, PwNode

    nodes = idle_graph.nodes + (
        PwNode(id=9001, serial=9001, name=VIRTMIC_SINK, description="", media_class=SINK),
        PwNode(id=9002, serial=9002, name=VIRTMIC_SOURCE, description="", media_class=SOURCE),
    )
    assert check_virtmic(replace(idle_graph, nodes=nodes)).ok is True


def test_linking_check_fails_when_pw_link_cannot_reach_a_session(monkeypatch):
    """Presence on PATH is not the same as being able to link.

    The runtime symptom is one warning six seconds in and then permanent
    silence from the remote direction, so this has to fail at setup instead.
    """

    class Result:
        returncode = 1
        stderr = "failed to connect"

    monkeypatch.setattr("sidetap.doctor.shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("sidetap.doctor.subprocess.run", lambda *a, **k: Result())
    check = check_linking()
    assert check.ok is False
    assert "PipeWire" in check.detail


def test_linking_check_passes_when_pw_link_lists(monkeypatch):
    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr("sidetap.doctor.shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("sidetap.doctor.subprocess.run", lambda *a, **k: Result())
    assert check_linking().ok is True


def test_vad_check_passes_when_webrtcvad_imports():
    # A hard dependency, so this should pass in any working environment.
    assert check_vad().ok is True


def test_vad_check_fails_loudly_when_webrtcvad_is_missing(monkeypatch):
    """The failure is otherwise silent and doubles the API bill.

    vad.py degrades to a None detector that allows everything through, so
    both directions stream their silence to a metered API all call long.
    """
    import builtins

    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "webrtcvad":
            raise ImportError("no module named webrtcvad")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    check = check_vad()
    assert check.ok is False
    assert "uv sync" in check.detail


def test_credentials_accept_application_default_credentials(monkeypatch):
    """`gcloud auth application-default login` sets no environment variable.

    It is how a desktop user normally authenticates. Demanding
    GOOGLE_APPLICATION_CREDENTIALS reported FAIL on a working machine and then
    skipped the three API checks that are the reason to run doctor at all.
    """
    monkeypatch.setattr("google.auth.default", lambda *a, **k: (object(), "adc-proj"))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    assert check_credentials().ok is True


def test_credentials_fail_when_no_project_is_named(monkeypatch):
    """ADC's own project is whatever gcloud last pointed at.

    Using it silently means a confusing 403, or billing the wrong project.
    """
    monkeypatch.setattr("google.auth.default", lambda *a, **k: (object(), "other"))
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    check = check_credentials()
    assert check.ok is False
    assert "other" in check.detail, "name the project it declined to assume"


def test_credentials_say_so_when_adc_points_somewhere_else(monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda *a, **k: (object(), "other"))
    check = check_credentials(project_override="proj")
    assert check.ok is True
    assert "other" in check.detail


def test_credentials_fail_when_there_are_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("could not automatically determine credentials")

    monkeypatch.setattr("google.auth.default", boom)
    check = check_credentials(project_override="proj")
    assert check.ok is False
    assert "gcloud auth" in check.detail


def test_a_missing_quota_project_is_not_reported_as_a_disabled_api():
    """The same 403, and the wording of one reads like the other.

    Observed live: re-running the ADC login drops the quota project, and
    Translation and Text-to-Speech then fail with a message a reader skims as
    "API not enabled", sending them to re-enable an API that was never off.
    """
    from google.api_core import exceptions as gexc

    from sidetap.doctor import _explain

    detail = _explain(
        gexc.PermissionDenied(
            "The translate.googleapis.com API requires a quota project"
        )
    )
    assert "set-quota-project" in detail
    assert "NOT a disabled API" in detail


def test_the_report_does_not_claim_success_when_nothing_ran():
    assert "all checks passed" not in render_report([]).lower()


def test_install_writes_the_config_when_absent(tmp_path):
    path = tmp_path / "90-sidetap-mic.conf"
    assert install_virtmic_config(path) is True
    assert VIRTMIC_SOURCE in path.read_text()


def test_install_does_not_clobber_an_existing_file(tmp_path):
    path = tmp_path / "90-sidetap-mic.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# hand-edited")
    assert install_virtmic_config(path) is False
    assert path.read_text() == "# hand-edited"


def test_install_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "a" / "b" / "90-sidetap-mic.conf"
    assert install_virtmic_config(path) is True
    assert path.exists()


def test_pw_cli_is_one_of_the_required_tools():
    """check_pipewire_version shells out to it.

    Left out of the list, a machine missing only pw-cli is told PipeWire is
    version 0.0.0 and to upgrade - which is not the problem it has.
    """
    from sidetap.doctor import REQUIRED_TOOLS

    assert "pw-cli" in REQUIRED_TOOLS


def test_an_unreadable_version_is_not_reported_as_version_zero(monkeypatch):
    monkeypatch.setattr("sidetap.doctor.installed_pw_version", lambda: (0, 0, 0))
    check = check_pipewire_version()
    assert check.ok is False
    assert "0.0.0" not in check.detail
    assert "pw-cli" in check.detail


def test_a_real_version_below_the_minimum_still_says_so(monkeypatch):
    monkeypatch.setattr("sidetap.doctor.installed_pw_version", lambda: (0, 3, 40))
    check = check_pipewire_version()
    assert check.ok is False
    assert "0.3.40" in check.detail
