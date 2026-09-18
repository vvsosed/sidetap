import argparse
import threading
import time
from pathlib import Path

from sidetap.run import Session, default_voice, build_direction_configs
from sidetap.types import Direction
from tests.conftest import (
    FakeAudioSink,
    FakeClock,
    FakeGraphSource,
    FakeLauncher,
    FakeLinker,
    FakeSynthesizer,
    FakeTranslator,
    FakeVolumeControl,
)


def _args(**kwargs):
    base = dict(
        app="zoom", mic=None, latency="100ms",
        their_lang="ru-RU", my_lang="en-US", voice_in="", voice_out="",
        project="proj", region="europe-west3", mt_region="global",
        mt_model="general/translation-llm", tts_region="eu", model="chirp_3",
        phrases=None, out=Path("transcripts"), lag_cap=None, no_tui=True, verbose=False,
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_default_voice_is_a_chirp3_hd_voice_for_the_language():
    voice = default_voice("ru-RU")
    assert voice.startswith("ru-RU-Chirp3-HD-")


def test_an_explicit_voice_wins():
    configs = build_direction_configs(_args(voice_in="en-US-Chirp3-HD-Puck"))
    assert configs[Direction.IN].voice == "en-US-Chirp3-HD-Puck"


def test_the_in_direction_listens_in_their_language_and_speaks_in_yours():
    configs = build_direction_configs(_args())
    inbound = configs[Direction.IN]
    assert inbound.source_lang == "ru-RU"
    assert inbound.target_lang == "en-US"
    assert inbound.voice.startswith("en-US-")


def test_the_out_direction_is_the_mirror():
    configs = build_direction_configs(_args())
    outbound = configs[Direction.OUT]
    assert outbound.source_lang == "en-US"
    assert outbound.target_lang == "ru-RU"
    assert outbound.voice.startswith("ru-RU-")


def test_a_language_with_no_known_voice_is_a_clear_error():
    import pytest

    with pytest.raises(RuntimeError, match="no default Chirp 3 HD voice"):
        default_voice("xx-XX")


# routing_graph throughout, not the zoom fixture: setup() now refuses to
# start without sidetap_tts_sink, and only this fixture has it. It carries the
# same ZOOM VoiceEngine stream, so engage(app_pattern="zoom") still matches.
#
# journal_path is pinned under tmp_path rather than left at Router's real
# default (~/.local/state/sidetap/routing-journal.json). Session.setup()
# below runs Router.engage() against the routing fixture, which routes a
# real link and journals it - against the default path that would write to
# the machine actually running this suite, not a fixture. Every other Router
# test in tests/test_routing.py makes the same substitution.
def _session(tmp_path, routing_graph, **kwargs):
    linker = FakeLinker()
    defaults = dict(
        args=_args(out=tmp_path, **kwargs),
        graph=FakeGraphSource(routing_graph),
        launcher=FakeLauncher(),
        linker=linker,
        clock=FakeClock(),
        recognizer_factory=lambda config, direction: (lambda timeline: None),
        translator=FakeTranslator(),
        synthesizer=FakeSynthesizer(),
        volume=FakeVolumeControl(),
        journal_path=tmp_path / "routing-journal.json",
    )
    return Session(**defaults)


def test_a_missing_virtual_mic_is_fatal(tmp_path, routing_graph):
    """Silently falling back would send the translation to the user's speakers.

    They would hear their own translated voice, the remote party would hear
    nothing, and no error would appear anywhere.
    """
    import pytest
    from dataclasses import replace

    from sidetap.capture import CaptureError
    from sidetap.routing import VIRTMIC_SINK

    without = replace(
        routing_graph,
        nodes=tuple(n for n in routing_graph.nodes if n.name != VIRTMIC_SINK),
    )
    session = _session(tmp_path, without)
    with pytest.raises(CaptureError, match="doctor"):
        session.setup()


def test_the_graph_is_restored_on_shutdown(tmp_path, routing_graph):
    session = _session(tmp_path, routing_graph)
    session.setup()
    session.shutdown()
    assert session.router_restored is True


def test_the_graph_is_restored_even_if_a_worker_join_fails(tmp_path, routing_graph):
    """A half-restored graph leaves the user with no call audio at all."""
    session = _session(tmp_path, routing_graph)
    session.setup()

    def explode():
        raise RuntimeError("join blew up")

    session._join_workers = explode
    session.shutdown()
    assert session.router_restored is True


def test_shutdown_is_idempotent(tmp_path, routing_graph):
    session = _session(tmp_path, routing_graph)
    session.setup()
    session.shutdown()
    session.shutdown()
    assert session.router_restored is True


def test_the_transcript_is_closed_on_shutdown(tmp_path, routing_graph):
    session = _session(tmp_path, routing_graph)
    session.setup()
    session.shutdown()
    assert session.transcript.md_path.exists()


def test_bypass_opens_the_duck_and_links_the_real_mic(tmp_path, routing_graph):
    session = _session(tmp_path, routing_graph)
    session.setup()
    session.set_bypass(True)
    # All three together, or translated speech talks over the unmediated
    # conversation bypass exists to step out of.
    assert session.metrics.snapshot().bypassed is True
    assert session.playouts[Direction.IN].suppressed is True
    assert session.playouts[Direction.OUT].suppressed is True
    session.shutdown()


def test_bypass_toggles_back(tmp_path, routing_graph):
    session = _session(tmp_path, routing_graph)
    session.setup()
    session.set_bypass(True)
    session.set_bypass(False)
    assert session.metrics.snapshot().bypassed is False
    assert session.playouts[Direction.IN].suppressed is False
    session.shutdown()


def test_one_direction_dying_does_not_drop_the_call(tmp_path, routing_graph):
    """A bad --their-lang must not kill an OUT direction that is fine."""
    from google.api_core import exceptions as gexc

    from sidetap.metrics import Health

    session = _session(tmp_path, routing_graph)
    session.setup()
    session.direction_stop[Direction.IN].set()
    session._on_direction_fatal(Direction.IN, gexc.InvalidArgument("bad lang"))

    assert session.metrics.snapshot().directions[Direction.IN].asr is Health.FAILED
    assert session.stop.is_set() is False, "the call was dropped"
    session.shutdown()


def test_the_session_stops_once_every_direction_is_dead(tmp_path, routing_graph):
    from google.api_core import exceptions as gexc

    session = _session(tmp_path, routing_graph)
    session.setup()
    for direction in Direction:
        session.direction_stop[direction].set()
        session._on_direction_fatal(direction, gexc.InvalidArgument("bad lang"))

    assert session.stop.is_set() is True
    session.shutdown()


def test_quitting_while_bypassed_unlinks_the_real_mic(tmp_path, routing_graph):
    """The virtual mic outlives the process and the journal never saw this link.

    Left behind, the user's raw voice reaches every later call alongside the
    translation, and `doctor --repair` cannot find it to undo it.
    """
    session = _session(tmp_path, routing_graph)
    session.setup()
    before = set(session._linker.links)
    session.set_bypass(True)
    added = set(session._linker.links) - before
    assert added, "bypass linked nothing, so this test would prove nothing"
    session.shutdown()
    for pair in added:
        assert pair in session._linker.unlinks, (
            f"{pair} was left wired into the virtual mic"
        )


def test_bypass_opens_the_duck_without_waiting_for_a_tick(tmp_path, routing_graph):
    """A playout thread whose sink has died never ticks again.

    If bypass only set a flag for tick() to read, the duck would stay shut and
    the one escape hatch from a bad interpretation would be silence.
    """
    session = _session(tmp_path, routing_graph)
    session.setup()
    duck = session.playouts[Direction.IN].duck
    assert duck is not None, "the routing fixture is meant to produce a duck"
    duck.close()
    session.set_bypass(True)
    assert duck.is_open is True
    session.shutdown()


def test_a_deaf_track_is_reported_rather_than_looking_healthy(tmp_path, routing_graph):
    """An unlinked capture node delivers zero bytes, not silence.

    So the gate has nothing to gate and recognition waits quietly - every pane
    stays green while that direction hears nothing at all.
    """
    from sidetap.types import NO_AUDIO_S

    session = _session(tmp_path, routing_graph)
    session.setup()
    stop = threading.Event()
    thread = threading.Thread(
        target=session._poll_capture_health, args=(stop,), daemon=True
    )
    thread.start()
    session._clock.advance(NO_AUDIO_S + 2.0)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if session.metrics.snapshot().directions[Direction.OUT].no_audio:
            break
    stop.set()
    thread.join(timeout=2.0)
    assert session.metrics.snapshot().directions[Direction.OUT].no_audio is True
    session.shutdown()


def test_a_quiet_inbound_track_is_not_a_fault_before_the_call_starts(
    tmp_path, routing_graph
):
    """Starting sidetap before the call is an ordinary thing to do.

    Alarming on it would train the user to ignore the one warning that counts.
    """
    session = _session(tmp_path, routing_graph)
    session.setup()
    session.router._routed.clear()
    assert session._armed(Direction.IN) is False
    assert session._armed(Direction.OUT) is True
    session.shutdown()


def test_recognition_keeps_running_while_bypassed(tmp_path, routing_graph):
    """So the transcript stays continuous and toggling back restarts nothing."""
    session = _session(tmp_path, routing_graph)
    session.setup()
    session.set_bypass(True)
    assert session.stop.is_set() is False
    session.shutdown()
