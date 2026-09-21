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

# Private on purpose: Textual exports Footer but not the per-key widget it
# builds, and a footer key is the only place a toggle's state can be shown
# where the user already looks for it. tests/test_tui.py asserts the import
# and the resulting colour, so a Textual release that moves this fails the
# suite rather than silently leaving the key unlit.
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

    /* An engaged toggle. $warning, not $error: .pane.alarm owns $error for
       "something is wrong", and bypass and mute are things the user did on
       purpose.

       Both component classes have to be named, and NOT because of the
       background - FooterKey's own background does reach them. It is the
       foreground: $footer-key-foreground is itself amber in the default
       theme, so a rule that set only the background paints the key letter
       #ffa62b on a #fea62b fill and the letter vanishes. gruvbox and nord
       are nearly as bad. $text re-resolves against the new background, which
       is what keeps the key readable on every built-in theme. */
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
        # Deferred rather than called straight away: on_mount can fire before
        # compose()'s children have finished mounting, and an immediate call
        # here intermittently raced query_one() against the still-mounting
        # DOM. call_after_refresh runs once the screen has settled.
        self.call_after_refresh(self.refresh_from_metrics)
        self.set_interval(1 / REFRESH_HZ, self.refresh_from_metrics)

    def refresh_from_metrics(self) -> None:
        # The polling interval keeps ticking until Textual gets around to
        # cancelling it, which happens slightly after the app stops running
        # during shutdown - so a tick can still land here mid-teardown, after
        # screens have started being pruned but before the timer is stopped.
        # Guarding on is_running (rather than letting query_one raise) is
        # what makes that race harmless instead of an occasional NoMatches
        # crashing whatever test happens to be tearing down at that instant.
        if not self.is_running:
            return
        snapshot = self._metrics.snapshot()
        for direction, state in snapshot.directions.items():
            suffix = direction.value
            self.query_one(f"#interim-{suffix}", Static).update(state.interim)
            self.query_one(f"#source-{suffix}", Static).update(state.final)
            self.query_one(f"#target-{suffix}", Static).update(state.translation)
            # The two alarms are named, not merely coloured. They point at
            # opposite ends of the pipeline - NO AUDIO means nothing is
            # arriving to work on, DEAD AIR means an utterance finished and
            # nothing came out the far end - and a user who cannot tell them
            # apart cannot act on either.
            if state.no_audio:
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
            pane.set_class(state.dead_air or state.no_audio, "alarm")

        # The model matters because a sticky downgrade to NMT is otherwise
        # invisible - the health dots stay green, quality just quietly drops.
        model = snapshot.mt_model.rsplit("/", 1)[-1] or "—"
        state = "BYPASSED  " if snapshot.bypassed else ""
        self.sub_title = f"{state}mt:{model}  est. ${snapshot.cost_usd:.2f}"

        self._paint_toggles(
            {"bypass": snapshot.bypassed, "mute": snapshot.muted_out}
        )

    def _paint_toggles(self, engaged: dict[str, bool]) -> None:
        """Light the footer key of a toggle that is currently on.

        Re-applied every tick rather than once per keypress, because Footer
        rebuilds its FooterKey children from scratch whenever screen bindings
        change (bindings_changed -> recompose) and would drop a class set
        once. Polling is also what keeps the key honest: it shows what Metrics
        says, not what this app believes it asked for.

        Keyed on the binding's action, so keys with no toggle state - flush,
        quit, Textual's own command palette - are skipped by the lookup
        rather than by a list here that could fall out of date.
        """
        for key in self.query(FooterKey):
            state = engaged.get(key.action)
            if state is not None:
                key.set_class(state, "-engaged")

    def action_bypass(self) -> None:
        """Toggle against Metrics, not against a flag kept here.

        A local mirror is a second copy of the truth that nothing reconciles:
        if set_bypass raises partway, or anything else ever changes the
        session's state, the mirror and the snapshot disagree and the next
        press does the opposite of what the screen shows.
        """
        if self._session is not None:
            self._session.set_bypass(not self._metrics.snapshot().bypassed)

    def action_mute(self) -> None:
        """Stop sending your translated voice, without leaving the call.

        Through set_suppressed, never by assigning `suppressed` directly: the
        setter also throws the backlog away, and a queue built up while muted
        is a translation of a conversation that has already moved on. On
        unmute it would arrive as a voice recapping the last minute.

        Through the session rather than the playout directly: bypass
        suppresses that same playout, so `not playout.suppressed` read the
        wrong question and un-suppressed OUT mid-bypass. The session holds
        mute as its own flag and derives suppression from both.
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
