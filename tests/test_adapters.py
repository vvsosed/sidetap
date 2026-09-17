import pytest

import subprocess
import tempfile

from sidetap.adapters import (
    STDERR_TAIL_BYTES,
    MissingToolError,
    PopenProcess,
    PwLinkLinker,
    SystemClock,
    classify_link_output,
    parse_pw_version,
    require_tool,
)
from sidetap.ports import LinkResult


def test_link_success():
    assert classify_link_output(0, "") is LinkResult.LINKED


def test_file_exists_means_already_linked():
    # pw-link says this when the pair is already connected. Benign.
    assert classify_link_output(1, "failed to link ports: File exists") is (
        LinkResult.ALREADY_LINKED
    )


def test_other_errors_are_failures():
    assert classify_link_output(1, "No such port") is LinkResult.FAILED


def test_parses_the_pipewire_version():
    assert parse_pw_version("pw-cli\nCompiled with libpipewire 1.0.5\n") == (1, 0, 5)


def test_unparseable_version_is_zero():
    assert parse_pw_version("something unexpected") == (0, 0, 0)


def test_require_tool_returns_the_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    assert require_tool("pw-dump") == "/usr/bin/pw-dump"


def test_require_tool_explains_how_to_install(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)

    with pytest.raises(MissingToolError) as excinfo:
        require_tool("pw-record")

    message = str(excinfo.value)
    assert "pw-record" in message
    assert "pacman" in message  # install hints for the user's distro


def test_system_clock_satisfies_the_port():
    from sidetap.ports import Clock

    assert isinstance(SystemClock(), Clock)


def test_stderr_text_returns_the_tail_of_a_long_log():
    # A temp file rather than a pipe, so pw-record can log all meeting without
    # filling a 64 KB buffer and deadlocking. We keep the end of the log,
    # which is where the failure is.
    with tempfile.TemporaryFile() as handle:
        handle.write(b"x" * STDERR_TAIL_BYTES)
        handle.write(b"the actual error\n")

        text = PopenProcess(process=None, stderr_file=handle).stderr_text()

    assert "the actual error" in text
    assert len(text) <= STDERR_TAIL_BYTES


def test_stderr_text_is_empty_without_a_file():
    assert PopenProcess(process=None).stderr_text() == ""


def test_a_link_timeout_is_a_failure(monkeypatch):
    # link() runs inside AppTap's poll loop, so a hang would stall the
    # watcher for the rest of the meeting.
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def explode(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pw-link", timeout=5)

    monkeypatch.setattr("subprocess.run", explode)

    assert PwLinkLinker().link(60, 700) is LinkResult.FAILED


from sidetap.adapters import (
    PopenWriter,
    PwCatSink,
    PwLoopbackFactory,
    WpctlVolumeControl,
    classify_link_output,
    loopback_argv,
    pwcat_argv,
)
from sidetap.ports import LinkResult, LoopbackSpec
from tests.conftest import FakeLauncher


def test_pwcat_argv_asks_pipewire_to_resample():
    argv = pwcat_argv(target=77, rate=24_000)
    assert argv[0] == "pw-cat"
    assert "--playback" in argv
    assert argv[argv.index("--rate") + 1] == "24000"
    assert argv[argv.index("--channels") + 1] == "1"
    assert argv[argv.index("--format") + 1] == "s16"
    assert argv[argv.index("--target") + 1] == "77"
    # Raw PCM on stdin, not a file.
    assert "--raw" in argv
    assert argv[-1] == "-"


def test_pwcat_argv_without_a_target_lets_wireplumber_choose():
    assert "--target" not in pwcat_argv(target=None, rate=24_000)


def test_pwcat_sink_writes_and_flushes_through_the_launcher():
    launcher = FakeLauncher()
    sink = PwCatSink(launcher, target=5, rate=24_000)
    sink.write(b"\x01\x02")
    assert launcher.writer_calls[0][0] == "pw-cat"
    assert launcher.writers[0].written == b"\x01\x02"


def test_pwcat_sink_close_terminates_the_process():
    launcher = FakeLauncher()
    sink = PwCatSink(launcher, target=5, rate=24_000)
    sink.write(b"\x01")
    sink.close()
    assert launcher.writers[0].terminated


def test_pwcat_sink_survives_a_dead_pipe():
    """A terminated pw-cat must not take the playout thread down with it."""
    launcher = FakeLauncher()
    sink = PwCatSink(launcher, target=5, rate=24_000)
    sink.write(b"\x01")
    launcher.writers[0].stdin.close()
    sink.write(b"\x02")  # must not raise
    assert sink.failed


def test_loopback_argv_renders_both_property_maps():
    spec = LoopbackSpec(
        capture_props=(("node.name", "sidetap_duck"), ("media.class", "Audio/Sink")),
        playback_props=(("node.name", "out"),),
    )
    argv = loopback_argv(spec)
    assert argv[0] == "pw-loopback"
    capture = argv[argv.index("--capture-props") + 1]
    assert 'node.name="sidetap_duck"' in capture
    assert 'media.class="Audio/Sink"' in capture
    assert 'node.name="out"' in argv[argv.index("--playback-props") + 1]


def test_loopback_factory_spawns_through_the_launcher():
    launcher = FakeLauncher()
    factory = PwLoopbackFactory(launcher)
    spec = LoopbackSpec(capture_props=(("node.name", "d"),), playback_props=())
    process = factory.create(spec)
    assert launcher.writer_calls[0][0] == "pw-loopback"
    assert process is launcher.writers[0]


def test_wpctl_volume_formats_the_fraction(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr("sidetap.adapters.subprocess.run", fake_run)
    monkeypatch.setattr("sidetap.adapters.require_tool", lambda name: name)

    assert WpctlVolumeControl().set_volume(42, 0.0) is True
    assert calls[0] == ["wpctl", "set-volume", "42", "0.00"]


def test_wpctl_volume_reports_failure_rather_than_raising(monkeypatch):
    def fake_run(argv, **kwargs):
        class Result:
            returncode = 1
            stderr = "no such node"

        return Result()

    monkeypatch.setattr("sidetap.adapters.subprocess.run", fake_run)
    monkeypatch.setattr("sidetap.adapters.require_tool", lambda name: name)

    # Ducking runs inside the playout loop. A raise here would kill playout for
    # the rest of the call over a transient graph state.
    assert WpctlVolumeControl().set_volume(42, 1.0) is False


def test_unlink_classifies_a_missing_link_as_already_gone():
    # pw-link -d on a link that is not there is success for our purposes:
    # the desired end state holds.
    assert classify_link_output(1, "No such link") is LinkResult.ALREADY_LINKED
