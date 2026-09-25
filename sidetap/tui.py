"""Textual dashboard.

Textual owns the main thread's event loop and POLLS Metrics.snapshot() on an
interval. Nothing in the pipeline calls into this module - that one-way
dependency is what makes --no-tui and the headless test suite the same code
path rather than a second implementation.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Static

# Private: Textual exports Footer but not its per-key widget, and the footer
# key is where a toggle's state belongs. tests/test_tui.py fails if a Textual
# release moves it.
from textual.widgets._footer import FooterKey

from .metrics import Health, Metrics
from .types import Direction, Latency

REFRESH_HZ = 10

MARKERS = {Health.OK: "●", Health.RETRYING: "◐", Health.FAILED: "○"}
TITLES = {Direction.IN: "THEM → you", Direction.OUT: "YOU → them"}


def health_marker(health: Health) -> str:
    return MARKERS[health]


def format_lag(seconds: float) -> str:
    return f"{seconds:.1f}s"


def format_latency(latency: Latency) -> str:
    if latency.total_ms == 0:
        return "—"
    return (
        f"{latency.total_ms:.0f}ms "
        f"(asr {latency.asr_ms:.0f} / mt {latency.mt_ms:.0f} / tts {latency.tts_ms:.0f})"
    )


class SidetapApp(App):
    CSS = """
    Screen { layout: vertical; }
    .pane { border: round $primary; padding: 1 2; height: 1fr; }
    .pane.alarm { border: heavy $error; }
    .title { text-style: bold; }
    .interim { color: $text-muted; }
    .target { text-style: bold; }
    .stats { color: $text-muted; }

    /* An engaged toggle. $warning, not $error, which .pane.alarm uses for
       faults; bypass and mute are deliberate.

       Both component classes need an explicit colour: the default
       $footer-key-foreground is amber and would vanish on a $warning fill.
       $text re-resolves against the new background, keeping the key readable
       on every built-in theme. */
    FooterKey.-engaged {
        background: $warning;
        .footer-key--key { background: $warning; color: $text; text-style: bold; }
        .footer-key--description { background: $warning; color: $text; text-style: bold; }
    }
    """

    BINDINGS = [
        ("b", "bypass", "Bypass"),
        ("m", "mute", "Mute out"),
        ("f", "flush", "Drop backlog"),
        ("q", "quit_session", "Quit"),
    ]

    def __init__(self, metrics: Metrics, session):
        super().__init__()
        self._metrics = metrics
        self._session = session

    def compose(self) -> ComposeResult:
        for direction in Direction:
            suffix = direction.value
            yield Vertical(
                Static(TITLES[direction], classes="title", id=f"title-{suffix}"),
                Static("", classes="interim", id=f"interim-{suffix}"),
                Static("", id=f"source-{suffix}"),
                Static("", classes="target", id=f"target-{suffix}"),
                Static("", classes="stats", id=f"stats-{suffix}"),
                classes="pane",
                id=f"pane-{suffix}",
            )
        yield Footer()

    def on_mount(self) -> None:
        # Deferred: on_mount can fire before compose()'s children finish
        # mounting, and query_one() would race the DOM.
        self.call_after_refresh(self.refresh_from_metrics)
        self.set_interval(1 / REFRESH_HZ, self.refresh_from_metrics)

    def refresh_from_metrics(self) -> None:
        # The interval can still fire mid-teardown, after screens are pruned
        # but before the timer stops; return rather than raise NoMatches.
        if not self.is_running:
            return
        if self._session is not None and self._session.stop.is_set():
            # A signal, or both directions dead. Nothing else would end the
            # app, and the graph is only restored once it does.
            self.exit()
            return
        snapshot = self._metrics.snapshot()
        for direction, state in snapshot.directions.items():
            suffix = direction.value
            self.query_one(f"#interim-{suffix}", Static).update(state.interim)
            self.query_one(f"#source-{suffix}", Static).update(state.final)
            self.query_one(f"#target-{suffix}", Static).update(state.translation)
            # Named, not just coloured: NO AUDIO means nothing is arriving,
            # DEAD AIR means an utterance produced nothing, and the user has to
            # tell them apart to act.
            if state.playback_failed:
                alarm = "  PLAYBACK FAILED"
            elif state.no_audio:
                alarm = "  NO AUDIO ARRIVING"
            elif state.dead_air:
                alarm = "  DEAD AIR"
            else:
                alarm = ""
            self.query_one(f"#stats-{suffix}", Static).update(
                f"asr {health_marker(state.asr)}  mt {health_marker(state.mt)}  "
                f"tts {health_marker(state.tts)}   lag {format_lag(state.queue_s)}   "
                f"dropped {state.dropped}/{state.capture_dropped}   "
                f"{format_latency(state.latency)}{alarm}"
            )
            pane = self.query_one(f"#pane-{suffix}")
            pane.set_class(
                state.dead_air or state.no_audio or state.playback_failed, "alarm"
            )

        # Shown because a sticky downgrade to NMT is otherwise invisible: the
        # health dots stay green while quality drops.
        model = snapshot.mt_model.rsplit("/", 1)[-1] or "—"
        state = "BYPASSED  " if snapshot.bypassed else ""
        self.sub_title = f"{state}mt:{model}  est. ${snapshot.cost_usd:.2f}"

        self._paint_toggles(
            {"bypass": snapshot.bypassed, "mute": snapshot.muted_out}
        )

    def _paint_toggles(self, engaged: dict[str, bool]) -> None:
        """Light the footer key of a toggle that is currently on.

        Re-applied every tick, because Footer rebuilds its FooterKey children
        when bindings change and would drop a class set once. Polling also
        shows what Metrics says, not what this app asked for.

        Keyed on the binding's action, so keys with no toggle state are
        skipped by the lookup rather than by a list that could go stale.
        """
        for key in self.query(FooterKey):
            state = engaged.get(key.action)
            if state is not None:
                key.set_class(state, "-engaged")

    def action_bypass(self) -> None:
        """Toggle against Metrics, not a local flag.

        A local mirror would drift if set_bypass raised partway, and the next
        press would do the opposite of what the screen shows.
        """
        if self._session is not None:
            self._session.set_bypass(not self._metrics.snapshot().bypassed)

    def action_mute(self) -> None:
        """Stop sending your translated voice without leaving the call.

        Goes through the session, which holds mute as its own flag: bypass
        suppresses the same playout, so toggling `playout.suppressed` would
        un-suppress OUT mid-bypass. The session's path also flushes the
        backlog, which is stale by the time you unmute.
        """
        if self._session is not None:
            self._session.set_mute_out(not self._metrics.snapshot().muted_out)

    def action_flush(self) -> None:
        if self._session is not None:
            for playout in self._session.playouts.values():
                playout.flush()

    def action_quit_session(self) -> None:
        if self._session is not None:
            self._session.stop.set()
        self.exit()


def run_tui(session) -> None:
    SidetapApp(metrics=session.metrics, session=session).run()
