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
        speaking_rate_in=1.0, speaking_rate_out=1.0,
        voice_in_gender=None, voice_out_gender=None,
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_default_voice_is_a_chirp3_hd_voice_for_the_language():
    voice = default_voice("ru-RU")
    assert voice.startswith("ru-RU-Chirp3-HD-")


def test_the_default_voices_are_exactly_what_they_were_before_gender_existed():
    """Pinned by name, deliberately unlike the startswith checks around it.

    Those exist so the table can change personality freely. Here not changing
    is the whole promise of the gender flag - it is additive, and every
    command line that worked before produces the same two voices. A prefix
    check cannot tell Charon from Kore.
    """
    from sidetap.run import VOICES

    assert {lang: default_voice(lang) for lang in VOICES} == {
        "en-US": "en-US-Chirp3-HD-Charon",
        "en-GB": "en-GB-Chirp3-HD-Charon",
        "ru-RU": "ru-RU-Chirp3-HD-Kore",
        "uk-UA": "uk-UA-Chirp3-HD-Kore",
        "de-DE": "de-DE-Chirp3-HD-Kore",
        "es-ES": "es-ES-Chirp3-HD-Kore",
        "fr-FR": "fr-FR-Chirp3-HD-Kore",
        "pl-PL": "pl-PL-Chirp3-HD-Kore",
    }


def test_every_language_offers_both_genders_and_names_its_default():
    """A half-filled row is a fatal InvalidArgument at the first utterance."""
    from sidetap.run import VOICES
    from sidetap.tts import GENDERS

    for lang, entry in VOICES.items():
        assert set(entry) == {*GENDERS, "default"}, lang
        assert entry["default"] in GENDERS, lang
        for gender in GENDERS:
            assert entry[gender].startswith(f"{lang}-Chirp3-HD-"), (lang, gender)


def test_a_gender_overrides_the_language_default():
    assert default_voice("ru-RU", "male") == "ru-RU-Chirp3-HD-Charon"
    assert default_voice("en-US", "female") == "en-US-Chirp3-HD-Kore"
    # And asking for the gender it already defaults to changes nothing.
    assert default_voice("ru-RU", "female") == default_voice("ru-RU")


def test_gender_applies_to_the_language_each_direction_speaks():
    """IN speaks my_lang, OUT speaks their_lang - the same crossover as voice."""
    configs = build_direction_configs(
        _args(voice_in_gender="female", voice_out_gender="male")
    )
    assert configs[Direction.IN].voice == "en-US-Chirp3-HD-Kore"
    assert configs[Direction.OUT].voice == "ru-RU-Chirp3-HD-Charon"


def test_one_direction_gendered_leaves_the_other_at_its_default():
    configs = build_direction_configs(_args(voice_in_gender="female"))
    assert configs[Direction.IN].voice == "en-US-Chirp3-HD-Kore"
    assert configs[Direction.OUT].voice == "ru-RU-Chirp3-HD-Kore"


def test_a_language_with_no_known_voice_fails_the_same_way_with_a_gender():
    import pytest

    with pytest.raises(RuntimeError, match="no default Chirp 3 HD voice"):
        default_voice("xx-XX", "male")


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


def test_mute_suppresses_only_the_outbound_direction(tmp_path, routing_graph):
    session = _session(tmp_path, routing_graph)
    session.setup()
    session.set_mute_out(True)
    assert session.playouts[Direction.OUT].suppressed is True
    # Mute is "stop sending my voice", not "stop the call". Suppressing IN
    # too would silence the person you are listening to.
    assert session.playouts[Direction.IN].suppressed is False
    assert session.metrics.snapshot().muted_out is True
    session.shutdown()


def test_muting_clears_the_backlog_rather_than_deferring_it(tmp_path, routing_graph):
    """A queue built before the mute is a conversation that has moved on.

    Session.set_mute_out has to reach Playout.set_suppressed, which flushes.
    Assigning `suppressed` directly would suppress and keep the backlog, and
    unmuting would then play a voice recapping the last minute.
    """
    from sidetap.types import TTS_RATE, Translated, Unit

    session = _session(tmp_path, routing_graph)
    session.setup()
    session.playouts[Direction.OUT].submit(
        Translated(
            unit=Unit(direction=Direction.OUT, text="hi", t_start=0.0, t_end=0.0),
            text="ciao",
            pcm=b"\x00" * (TTS_RATE * 2 * 3),  # 3 seconds
        )
    )
    assert session.playouts[Direction.OUT].backlog_s() == 3.0

    session.set_mute_out(True)
    assert session.playouts[Direction.OUT].suppressed is True
    assert session.playouts[Direction.OUT].backlog_s() == 0.0
    session.shutdown()


def test_mute_while_bypassed_does_not_un_suppress_the_outbound_playout(
    tmp_path, routing_graph
):
    """Bypass's third effect must survive the mute key.

    Before mute became a flag of its own, `m` read `not playout.suppressed` -
    which under bypass is `not True` - and switched OUT back on, putting
    translated speech over the unmediated conversation bypass exists to step
    out of.
    """
    session = _session(tmp_path, routing_graph)
    session.setup()
    session.set_bypass(True)
    session.set_mute_out(True)
    assert session.playouts[Direction.OUT].suppressed is True
    session.set_mute_out(False)
    assert session.playouts[Direction.OUT].suppressed is True
    session.shutdown()


def test_leaving_bypass_restores_mute_rather_than_clearing_it(
    tmp_path, routing_graph
):
    """Muting, bypassing and coming back used to leave you audible."""
    session = _session(tmp_path, routing_graph)
    session.setup()
    session.set_mute_out(True)
    session.set_bypass(True)
    session.set_bypass(False)
    assert session.playouts[Direction.OUT].suppressed is True
    assert session.metrics.snapshot().muted_out is True
    # IN was only ever suppressed by bypass, so it comes back.
    assert session.playouts[Direction.IN].suppressed is False
    session.shutdown()


def test_leaving_bypass_un_suppresses_when_not_muted(tmp_path, routing_graph):
    session = _session(tmp_path, routing_graph)
    session.setup()
    session.set_bypass(True)
    session.set_bypass(False)
    assert session.playouts[Direction.OUT].suppressed is False
    session.shutdown()


def test_an_unchanged_suppression_state_is_not_re_applied(tmp_path, routing_graph):
    """set_suppressed flushes, and flush cuts the utterance in progress short.

    Pressing `b` while already muted must not chop the sentence that is
    playing on the IN side, and re-asserting OUT's unchanged state must not
    chop anything either.
    """
    session = _session(tmp_path, routing_graph)
    session.setup()
    session.set_mute_out(True)

    calls = []
    out = session.playouts[Direction.OUT]
    original = out.set_suppressed
    out.set_suppressed = lambda value: (calls.append(value), original(value))[1]

    session.set_bypass(True)
    assert calls == [], "OUT was already suppressed by mute; nothing to re-apply"
    session.set_mute_out(True)
    assert calls == [], "setting mute to the value it already had re-flushed"
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


def _session_with(tmp_path, routing_graph, **over):
    """_session, but with individual collaborators replaceable."""
    linker = over.pop("linker", None) or FakeLinker()
    defaults = dict(
        args=_args(out=tmp_path),
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
    defaults.update(over)
    return Session(**defaults)


def test_a_failure_in_start_still_gives_the_audio_graph_back(tmp_path, routing_graph):
    """start() runs after engage() has already rewired the call.

    capture.start() can time out waiting for a node, and _make_recognizer
    builds a live SpeechClient per direction, so the second can fail after the
    first succeeded. Left outside the guard, every one of those exits with the
    user's call audio routed into a duck node and no process left to undo it.
    """
    import pytest

    from sidetap import run as run_module

    session = _session_with(tmp_path, routing_graph)

    def explode():
        raise RuntimeError("capture node never appeared")

    def fake_session(*a, **k):
        session.start = explode
        return session

    original = run_module.Session
    run_module.Session = fake_session
    try:
        with pytest.raises(RuntimeError, match="capture node never appeared"):
            run_module.run_session(
                _args(out=tmp_path),
                graph=FakeGraphSource(routing_graph),
                launcher=FakeLauncher(),
                linker=FakeLinker(),
                clock=FakeClock(),
            )
    finally:
        run_module.Session = original

    assert session.router_restored is True, "the call was left inside the duck"


def test_bypass_does_not_claim_success_when_the_mic_link_fails(
    tmp_path, routing_graph, caplog
):
    """Both playouts are already suppressed by this point.

    So a silently failed link means the other party hears nothing at all while
    the interface reports bypass as fully engaged.
    """
    import logging

    from sidetap.ports import LinkResult

    session = _session_with(tmp_path, routing_graph)
    session.setup()
    session._linker = FakeLinker(result=LinkResult.FAILED)
    with caplog.at_level(logging.ERROR):
        session.set_bypass(True)

    assert session._real_mic_links == [], "a failed link was recorded as live"
    assert "hearing silence" in caplog.text
    session.shutdown()


def test_a_bypass_that_raises_partway_still_gets_cleaned_up(tmp_path, routing_graph):
    """The links already made are live even though set_bypass never finished.

    They outlive the process, the virtual mic is permanent, and nothing
    journals them - so doctor --repair cannot find them either.
    """
    import pytest

    state = {"armed": False}

    class HalfBrokenLinker(FakeLinker):
        def link(self, src_port, dst_port):
            # Armed only across set_bypass, so router.restore()'s own
            # re-linking during shutdown still works - otherwise this test
            # would be about a broken restore rather than about the leak.
            # The link itself lands; what fails is everything after it, which
            # is the case a naive "record it once we know it worked" ordering
            # gets wrong.
            result = super().link(src_port, dst_port)
            if state["armed"]:
                raise RuntimeError("pw-link vanished")
            return result

    linker = HalfBrokenLinker()
    session = _session_with(tmp_path, routing_graph, linker=linker)
    session.setup()

    state["armed"] = True
    with pytest.raises(RuntimeError):
        session.set_bypass(True)
    state["armed"] = False

    live = list(session._real_mic_links)
    assert live, "this test proves nothing unless a link really was made"
    assert session._bypassed is True, "bypass must be claimed before it can leak"

    session.shutdown()
    for pair in live:
        assert pair in linker.unlinks, f"{pair} was left wired into the virtual mic"


def test_a_sink_spawned_before_a_failed_setup_is_not_left_running(
    tmp_path, routing_graph
):
    """The playout threads' own finally cannot help here.

    Those threads only exist once start() has succeeded. The launcher uses
    start_new_session=True, so an orphaned pw-cat survives this process
    entirely and accumulates on every failed launch.
    """
    import pytest

    from sidetap import run as run_module

    launcher = FakeLauncher()
    session = _session_with(tmp_path, routing_graph, launcher=launcher)

    def boom(*a, **k):
        raise run_module.CaptureError("no default microphone")

    original = run_module.PipeWireCapture
    run_module.PipeWireCapture = boom
    try:
        with pytest.raises(run_module.CaptureError):
            session.setup()
    finally:
        run_module.PipeWireCapture = original

    assert session.sinks, "this test proves nothing unless a sink was built"
    session.shutdown()
    playback = [w for w in launcher.writers if "--playback" in launcher.writer_calls[
        launcher.writers.index(w)
    ]]
    assert playback, "no pw-cat playback process was spawned"
    assert all(w.terminated for w in playback), "a pw-cat was left orphaned"


def test_each_direction_gets_its_own_speaking_rate():
    """The two translate opposite ways, so their useful rates are inverses.

    If the target is 1.23x the length of the source one way, it is 0.81x the
    other; one shared value makes the second direction needlessly fast.
    """
    configs = build_direction_configs(
        _args(speaking_rate_in=1.3, speaking_rate_out=0.95)
    )
    assert configs[Direction.IN].speaking_rate == 1.3
    assert configs[Direction.OUT].speaking_rate == 0.95


def test_both_directions_default_to_neutral():
    """No shared flag, for the same reason there is no --voice.

    The useful rates are inverses of each other, so a single value applied to
    both fixes one direction and makes the other needlessly fast.
    """
    configs = build_direction_configs(_args())
    assert configs[Direction.IN].speaking_rate == 1.0
    assert configs[Direction.OUT].speaking_rate == 1.0
