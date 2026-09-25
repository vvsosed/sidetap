import pytest

from textual.widgets import Static

# The same private import sidetap.tui makes. Named here too so this file
# shows the dependency it is guarding.
from textual.widgets._footer import FooterKey

from sidetap.metrics import Health, Metrics
from sidetap.tui import SidetapApp, format_lag, format_latency, health_marker
from sidetap.types import Direction, Latency


def test_health_markers_are_distinguishable_without_colour():
    markers = {health_marker(h) for h in Health}
    assert len(markers) == 3


def test_lag_is_shown_in_seconds():
    assert format_lag(0.0) == "0.0s"
    assert format_lag(12.34) == "12.3s"


def test_latency_shows_the_total_and_the_breakdown():
    text = format_latency(Latency(asr_ms=100.0, mt_ms=50.0, tts_ms=250.0))
    assert "400" in text
    assert "100" in text and "50" in text and "250" in text


def test_zero_latency_renders_a_placeholder():
    assert format_latency(Latency()) == "—"


async def test_the_app_renders_both_direction_panes():
    metrics = Metrics()
    app = SidetapApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#pane-in") is not None
        assert app.query_one("#pane-out") is not None


async def test_the_app_shows_a_final_after_a_refresh():
    metrics = Metrics()
    metrics.set_final(Direction.IN, "привет", "hello", Latency(mt_ms=50.0))
    app = SidetapApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        app.refresh_from_metrics()
        await pilot.pause()
        assert "привет" in app.query_one("#source-in").content
        assert "hello" in app.query_one("#target-in").content


async def test_the_two_alarms_are_named_not_just_coloured():
    """They point at opposite ends of the pipeline.

    NO AUDIO means nothing is arriving to work on; DEAD AIR means an utterance
    finished and nothing came out the far end. A user who cannot tell them
    apart cannot act on either.
    """
    metrics = Metrics()
    metrics.set_no_audio(Direction.IN, True)
    app = SidetapApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        rendered = str(app.query_one("#stats-in", Static).content)
        assert "NO AUDIO" in rendered
        assert "DEAD AIR" not in rendered


async def test_dead_air_is_visible():
    metrics = Metrics()
    metrics.set_dead_air(Direction.OUT, True)
    app = SidetapApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        app.refresh_from_metrics()
        await pilot.pause()
        assert app.query_one("#pane-out").has_class("alarm")


class RecordingSession:
    """Writes through to Metrics, because the TUI now reads state back.

    A stub that only recorded the argument would let a snapshot-driven toggle
    pass its first press and fail its second, which is the bug that made the
    old local mirror worth removing.
    """

    def __init__(self, metrics=None):
        self.metrics = metrics or Metrics()
        self.bypassed = None
        self.muted_out = None
        self.flushed = 0
        self.stop = _Event()

    def set_bypass(self, value):
        self.bypassed = value
        self.metrics.set_bypassed(value)

    def set_mute_out(self, value):
        self.muted_out = value
        self.metrics.set_muted_out(value)

    def alarm_dead_air(self):
        pass


class _Event:
    def __init__(self):
        self._set = False

    def set(self):
        self._set = True

    def is_set(self):
        return self._set


def _footer_key(app, action):
    return next(k for k in app.query(FooterKey) if k.action == action)


async def test_b_toggles_bypass():
    metrics = Metrics()
    session = RecordingSession(metrics)
    app = SidetapApp(metrics=metrics, session=session)
    async with app.run_test() as pilot:
        await pilot.press("b")
        assert session.bypassed is True
        await pilot.press("b")
        assert session.bypassed is False


async def test_m_toggles_mute_through_the_session():
    """Not by poking the playout: bypass suppresses that same playout."""
    metrics = Metrics()
    session = RecordingSession(metrics)
    app = SidetapApp(metrics=metrics, session=session)
    async with app.run_test() as pilot:
        await pilot.press("m")
        assert session.muted_out is True
        await pilot.press("m")
        assert session.muted_out is False


async def test_an_engaged_toggle_lights_its_footer_key():
    metrics = Metrics()
    app = SidetapApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not _footer_key(app, "bypass").has_class("-engaged")

        metrics.set_bypassed(True)
        metrics.set_muted_out(True)
        app.refresh_from_metrics()
        await pilot.pause()
        assert _footer_key(app, "bypass").has_class("-engaged")
        assert _footer_key(app, "mute").has_class("-engaged")

        metrics.set_bypassed(False)
        app.refresh_from_metrics()
        await pilot.pause()
        assert not _footer_key(app, "bypass").has_class("-engaged")
        assert _footer_key(app, "mute").has_class("-engaged"), "mute followed bypass"


async def test_keys_without_a_toggle_state_never_light():
    metrics = Metrics()
    metrics.set_bypassed(True)
    metrics.set_muted_out(True)
    app = SidetapApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.refresh_from_metrics()
        await pilot.pause()
        # Guard the guard: the Footer mounts its keys after the first pause,
        # so a refresh called before one paints nothing and every assertion
        # below would hold for the wrong reason.
        assert _footer_key(app, "bypass").has_class("-engaged")
        for action in ("flush", "quit_session"):
            assert not _footer_key(app, action).has_class("-engaged")


def _contrast(style):
    """Crude channel distance between a span's foreground and background."""
    fg, bg = style.color.triplet, style.bgcolor.triplet
    return sum(abs(a - b) for a, b in zip(fg, bg))


async def test_the_engaged_key_actually_changes_colour():
    """The class is not the point - the colour on screen is."""
    metrics = Metrics()
    app = SidetapApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        key = _footer_key(app, "bypass")
        idle = [span.style.bgcolor for span in key.render().spans]

        metrics.set_bypassed(True)
        app.refresh_from_metrics()
        await pilot.pause()
        engaged = [span.style.bgcolor for span in key.render().spans]

        assert engaged != idle
        assert len(set(engaged)) == 1, "the whole key should fill, not just part"


@pytest.mark.parametrize(
    "theme", ["textual-dark", "textual-light", "nord", "gruvbox", "monokai"]
)
async def test_the_engaged_key_stays_readable(theme):
    """Filling the background is only half of it.

    $footer-key-foreground is itself amber in the default theme, so a rule
    that set the background and left the component classes alone would paint
    the key letter #ffa62b on a #fea62b fill - present, correct, invisible.
    Parametrised because the failure is theme-dependent and textual-dark is
    the worst case, not the only one.
    """
    metrics = Metrics()
    metrics.set_bypassed(True)
    app = SidetapApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        app.theme = theme
        await pilot.pause()
        app.refresh_from_metrics()
        await pilot.pause()
        key = _footer_key(app, "bypass")
        # Without this the test still passes when nothing lights at all: the
        # Footer mounts its keys after the first pause, so a refresh before
        # one leaves the key idle - and an idle key is perfectly readable.
        assert key.has_class("-engaged")
        for span in key.render().spans:
            assert _contrast(span.style) > 60, f"unreadable under {theme}"


async def test_q_stops_the_session():
    session = RecordingSession()
    app = SidetapApp(metrics=Metrics(), session=session)
    async with app.run_test() as pilot:
        await pilot.press("q")
        assert session.stop.is_set()


async def test_the_app_exits_once_the_session_is_stopped():
    """A signal or both directions dying sets session.stop.

    The TUI never looked at it, so the process lived on with the graph still
    rewired until someone pressed q - in a terminal that may already be gone.
    """
    session = RecordingSession()
    app = SidetapApp(metrics=session.metrics, session=session)
    async with app.run_test() as pilot:
        session.stop.set()
        await pilot.pause(0.5)
        assert app._exit, "the app kept running after the session stopped"


async def test_a_dead_playback_sink_is_named_and_alarms_the_pane():
    metrics = Metrics()
    metrics.set_playback_failed(Direction.OUT, True)
    app = SidetapApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "PLAYBACK FAILED" in str(app.query_one("#stats-out", Static).content)
        assert app.query_one("#pane-out").has_class("alarm")
