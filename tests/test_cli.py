import pytest

from sidetap.cli import build_parser, describe_graph, main
from tests.conftest import FakeClock, FakeGraphSource, FakeLauncher, FakeLinker


def test_devices_lists_sinks_sources_and_streams(zoom_graph):
    text = describe_graph(zoom_graph)
    assert "OUTPUT DEVICES" in text
    assert "INPUT DEVICES" in text
    assert "APPLICATIONS CURRENTLY PLAYING AUDIO" in text


def test_devices_suggests_the_app_flag(zoom_graph):
    assert "--app" in describe_graph(zoom_graph)


def test_devices_explains_an_empty_stream_list(idle_graph):
    # The single most common first-run confusion: an application does not
    # appear in the graph until it actually starts a stream.
    text = describe_graph(idle_graph)
    assert "start your" in text.lower()


def test_devices_hides_monitor_sources(zoom_graph):
    text = describe_graph(zoom_graph)
    monitors = [line for line in text.splitlines() if line.strip().endswith(".monitor")]
    assert monitors == []


def test_devices_command_returns_zero(idle_graph, capsys):
    code = main(
        ["devices"],
        graph=FakeGraphSource(idle_graph),
        launcher=FakeLauncher(),
        linker=FakeLinker(),
        clock=FakeClock(),
    )
    assert code == 0
    assert "OUTPUT DEVICES" in capsys.readouterr().out


def test_doctor_command_returns_nonzero_when_a_check_fails(idle_graph, capsys, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr("sidetap.doctor.shutil.which", lambda name: None)
    # Hermetic, deliberately. Without these two the test shells out to the
    # real pw-link and reads the developer's own gcloud credentials, so it
    # would pass or fail based on the machine it runs on rather than the code.
    monkeypatch.setattr(
        "sidetap.doctor.subprocess.run",
        lambda *a, **k: type("R", (), {"returncode": 1, "stderr": "no session"})(),
    )
    monkeypatch.setattr("google.auth.default", lambda *a, **k: (object(), None))
    code = main(
        ["doctor", "--no-api-check"],
        graph=FakeGraphSource(idle_graph),
        launcher=FakeLauncher(),
        linker=FakeLinker(),
        clock=FakeClock(),
    )
    assert code == 1
    assert "FAIL" in capsys.readouterr().out


def test_run_requires_both_languages():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--app", "zoom", "--their-lang", "ru-RU"])


def test_run_accepts_a_full_invocation():
    args = build_parser().parse_args(
        [
            "run",
            "--app", "zoom",
            "--their-lang", "ru-RU",
            "--my-lang", "en-US",
            "--voice-in", "en-US-Chirp3-HD-Charon",
            "--voice-out", "ru-RU-Chirp3-HD-Kore",
            "--phrase", "Volodymyr",
            "--no-tui",
        ]
    )
    assert args.their_lang == "ru-RU"
    assert args.my_lang == "en-US"
    assert args.phrases == ["Volodymyr"]
    assert args.no_tui is True


def test_region_defaults_match_the_spec():
    args = build_parser().parse_args(
        ["run", "--app", "zoom", "--their-lang", "ru-RU", "--my-lang", "en-US"]
    )
    # Three different regions, deliberately: Translation is rejected outright
    # in europe-west3, and TTS has no Frankfurt single-region.
    assert args.region == "europe-west3"
    assert args.mt_region == "global"
    assert args.tts_region == "eu"


def test_translation_region_is_not_the_stt_region():
    """One shared --region flag would be rejected by Cloud Translation.

    Measured, not assumed: europe-west3 returns 400 "Must be 'us-central1' or
    'global'" for every model and every language pair.
    """
    args = build_parser().parse_args(
        ["run", "--app", "zoom", "--their-lang", "ru-RU", "--my-lang", "en-US"]
    )
    assert args.mt_region != args.region


def test_a_bad_pw_dump_reports_one_clean_line(capsys):
    import json

    class BrokenGraph:
        def snapshot(self):
            raise json.JSONDecodeError("bad", "", 0)

    code = main(["devices"], graph=BrokenGraph(), launcher=FakeLauncher(),
                linker=FakeLinker(), clock=FakeClock())
    assert code == 1
    assert "pw-dump" in capsys.readouterr().err


def test_an_unexpected_error_is_one_line_without_verbose(capsys):
    class ExplodingGraph:
        def snapshot(self):
            raise ValueError("kaboom")

    code = main(["devices"], graph=ExplodingGraph(), launcher=FakeLauncher(),
                linker=FakeLinker(), clock=FakeClock())
    assert code == 1
    err = capsys.readouterr().err
    assert "ValueError: kaboom" in err
    assert "Traceback" not in err


def test_verbose_reraises_for_a_real_traceback():
    class ExplodingGraph:
        def snapshot(self):
            raise ValueError("kaboom")

    with pytest.raises(ValueError):
        main(["devices", "-v"], graph=ExplodingGraph(), launcher=FakeLauncher(),
             linker=FakeLinker(), clock=FakeClock())
