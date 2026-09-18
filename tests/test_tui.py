import pytest

from textual.widgets import Static

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


async def test_muting_clears_the_backlog_rather_than_deferring_it():
    """Assigning `suppressed` directly would skip the flush in the setter."""
    from sidetap.playout import Playout
    from sidetap.types import TTS_RATE, Translated, Unit
    from tests.conftest import FakeAudioSink

    class Session:
        def __init__(self, playout):
            self.playouts = {Direction.OUT: playout}

    playout = Playout(Direction.OUT, FakeAudioSink())
    playout.submit(
        Translated(
            unit=Unit(direction=Direction.OUT, text="hi", t_start=0.0, t_end=0.0),
            text="ciao",
            pcm=b"\x00" * (TTS_RATE * 2 * 3),  # 3 seconds
        )
    )
    assert playout.backlog_s() == 3.0
    app = SidetapApp(metrics=Metrics(), session=Session(playout))
    async with app.run_test() as pilot:
        await pilot.press("m")
        await pilot.pause()
    assert playout.suppressed is True
    assert playout.backlog_s() == 0.0, "mute deferred the backlog instead of dropping it"


async def test_dead_air_is_visible():
    metrics = Metrics()
    metrics.set_dead_air(Direction.OUT, True)
    app = SidetapApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        app.refresh_from_metrics()
        await pilot.pause()
        assert app.query_one("#pane-out").has_class("alarm")


class RecordingSession:
    def __init__(self):
        self.bypassed = None
        self.flushed = 0
        self.stop = _Event()

    def set_bypass(self, value):
        self.bypassed = value

    def alarm_dead_air(self):
        pass


class _Event:
    def __init__(self):
        self._set = False

    def set(self):
        self._set = True

    def is_set(self):
        return self._set


async def test_b_toggles_bypass():
    session = RecordingSession()
    app = SidetapApp(metrics=Metrics(), session=session)
    async with app.run_test() as pilot:
        await pilot.press("b")
        assert session.bypassed is True
        await pilot.press("b")
        assert session.bypassed is False


async def test_q_stops_the_session():
    session = RecordingSession()
    app = SidetapApp(metrics=Metrics(), session=session)
    async with app.run_test() as pilot:
        await pilot.press("q")
        assert session.stop.is_set()
