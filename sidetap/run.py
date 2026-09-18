"""Build a session, run it, and give the audio graph back."""

from __future__ import annotations

import logging
import queue
import signal
import threading
import time

from .adapters import PwCatSink, PwLoopbackFactory, WpctlVolumeControl
from .asr import AsrConfig, RecognitionWorker, build_recognizer_factory, project_from_environment
from .capture import CaptureConfig, CaptureError, PipeWireCapture
from .metrics import Health, Metrics
from .pipeline import DeadAirWatch, DirectionConfig, DirectionPipeline
from .playout import DuckControl, Playout, earcon
from .routing import JOURNAL_PATH, VIRTMIC_SINK, Router
from .segment import FinalsOnlySegmenter
from .transcript import BilingualTranscript
from .translate import TranslateConfig, build_translator
from .tts import TtsConfig, build_synthesizer
from .types import LAG_CAP_S, NO_AUDIO_S, TTS_RATE, Direction, Record
from .vad import SilenceGate, webrtc_detector

log = logging.getLogger(__name__)

SHUTDOWN_JOIN_S = 3.0

# Chirp 3 HD ships 30 shared voice names per locale. One neutral default each
# for the languages this has actually been run against; anything else must be
# named explicitly rather than guessed at, because an invalid voice is a fatal
# InvalidArgument at the first utterance.
DEFAULT_VOICES = {
    "en-US": "en-US-Chirp3-HD-Charon",
    "en-GB": "en-GB-Chirp3-HD-Charon",
    "ru-RU": "ru-RU-Chirp3-HD-Kore",
    "uk-UA": "uk-UA-Chirp3-HD-Kore",
    "de-DE": "de-DE-Chirp3-HD-Kore",
    "es-ES": "es-ES-Chirp3-HD-Kore",
    "fr-FR": "fr-FR-Chirp3-HD-Kore",
    "pl-PL": "pl-PL-Chirp3-HD-Kore",
}


def default_voice(language_code: str) -> str:
    try:
        return DEFAULT_VOICES[language_code]
    except KeyError:
        raise RuntimeError(
            f"no default Chirp 3 HD voice for {language_code!r}. Pass --voice-in "
            "or --voice-out explicitly; see the Chirp 3 HD voice list."
        ) from None


def build_direction_configs(args) -> dict[Direction, DirectionConfig]:
    return {
        Direction.IN: DirectionConfig(
            direction=Direction.IN,
            source_lang=args.their_lang,
            target_lang=args.my_lang,
            voice=args.voice_in or default_voice(args.my_lang),
        ),
        Direction.OUT: DirectionConfig(
            direction=Direction.OUT,
            source_lang=args.my_lang,
            target_lang=args.their_lang,
            voice=args.voice_out or default_voice(args.their_lang),
        ),
    }


class Session:
    def __init__(
        self,
        args,
        graph,
        launcher,
        linker,
        clock,
        recognizer_factory=None,
        translator=None,
        synthesizer=None,
        volume=None,
        journal_path=None,
    ):
        self._args = args
        self._graph = graph
        self._launcher = launcher
        self._linker = linker
        self._clock = clock
        self._make_recognizer = recognizer_factory or build_recognizer_factory
        self._translator = translator
        self._synthesizer = synthesizer
        # Injected like every other port, and built here so that a test never
        # constructs one by accident: WpctlVolumeControl shells out to wpctl
        # against the developer's own machine.
        self._volume = volume or WpctlVolumeControl()
        # Overridable for the same reason doctor.py's install_virtmic_config
        # takes a path: Router's real default lives under the developer's own
        # home directory, and every test that touches a Router - including
        # this one - must not write there. Production leaves this at
        # routing.JOURNAL_PATH so `doctor --repair` still knows where to look.
        self._journal_path = journal_path if journal_path is not None else JOURNAL_PATH

        self.metrics = Metrics()
        # Session-wide: set by Ctrl-C, or once every direction has died.
        self.stop = threading.Event()
        # Per-direction: a fatal recognition error kills one direction without
        # dropping a live call the other is still interpreting.
        self.direction_stop = {d: threading.Event() for d in Direction}
        self.playouts: dict[Direction, Playout] = {}
        self.sinks: dict[Direction, PwCatSink] = {}
        self.pipelines: dict[Direction, DirectionPipeline] = {}
        self.router_restored = False
        self._threads: list[threading.Thread] = []
        self._shutdown_done = False
        self._bypassed = False
        self._real_mic_links: list[tuple[int, int]] = []
        # None until setup() gets far enough to create them. setup() can
        # raise before any of these exist - the virtual-mic check runs
        # BEFORE Router is even constructed, on purpose, so that a fatal
        # check never has anything to undo - and shutdown() has to survive
        # being called in that state rather than raising an AttributeError
        # that would bury the original failure.
        self.router = None
        self.transcript = None
        self.capture = None

    def setup(self) -> None:
        args = self._args
        project = args.project or project_from_environment()
        configs = build_direction_configs(args)
        self._configs = configs

        # BEFORE the router touches anything. This check is a pure snapshot
        # read, and engage() is the first irreversible act of the session: it
        # rewires the app into the duck and spawns a loopback. Checking after
        # would mean the one machine this fires on - a first run, before
        # `doctor --install` - gets its call audio rewired and then an
        # exception, with setup() half-done and nobody left to restore it.
        snapshot = self._graph.snapshot()
        virtmic = snapshot.node_by_name(VIRTMIC_SINK)
        if virtmic is None:
            # NOT a fallback. pw-cat with no --target autoconnects to the
            # default sink, so the outbound translation would come out of the
            # user's own speakers while the remote party heard silence - and
            # nothing would say so. Fail here instead.
            raise CaptureError(
                f"{VIRTMIC_SINK} does not exist, so the translation sent to "
                "the other party has nowhere to go. Run: sidetap doctor "
                "--install, then systemctl --user restart pipewire "
                "pipewire-pulse"
            )

        self.router = Router(
            graph=self._graph,
            linker=self._linker,
            unlinker=self._linker,
            loopbacks=PwLoopbackFactory(self._launcher),
            journal_path=self._journal_path,
        )
        self.router.repair()
        self.router.engage(app_pattern=args.app)

        self.transcript = BilingualTranscript(args.out)

        # on_downgrade has a destination, which is the whole point of it
        # existing: a sticky fallback to NMT is otherwise invisible, because
        # the next successful call sets mt=Health.OK and the pane goes green.
        self.metrics.set_mt_model(args.mt_model)
        translator = self._translator or build_translator(
            TranslateConfig(
                project_id=project, region=args.mt_region, model=args.mt_model
            ),
            on_downgrade=self.metrics.set_mt_model,
        )
        synthesizer = self._synthesizer or build_synthesizer(
            TtsConfig(region=args.tts_region)
        )

        default_sink = snapshot.node_by_name(snapshot.default_sink or "")
        targets = {
            Direction.IN: default_sink.serial if default_sink else None,
            Direction.OUT: virtmic.serial,
        }

        self._dead_air = DeadAirWatch(self._clock)
        lag_cap = args.lag_cap if args.lag_cap is not None else LAG_CAP_S
        # One origin for both directions. Taken inside the loop below, the two
        # tracks would be stamped against origins milliseconds apart, and a
        # bilingual transcript exists so the two columns line up.
        session_t0 = self._clock.monotonic()

        for direction, config in configs.items():
            duck = (
                DuckControl(self._volume, self.router.duck_id)
                if direction is Direction.IN and self.router.duck_id is not None
                else None
            )
            sink = PwCatSink(self._launcher, target=targets[direction], rate=TTS_RATE)
            self.sinks[direction] = sink
            playout = Playout(
                direction,
                sink,
                duck=duck,
                lag_cap_s=lag_cap,
                on_dropped=self._make_drop_handler(direction),
            )
            self.playouts[direction] = playout
            self.pipelines[direction] = DirectionPipeline(
                config=config,
                segmenter=FinalsOnlySegmenter(),
                translator=translator,
                synthesizer=synthesizer,
                playout=playout,
                metrics=self.metrics,
                clock=self._clock,
                session_t0=session_t0,
                on_record=self.transcript.write,
                dead_air=self._dead_air if direction is Direction.OUT else None,
            )

        self.capture = PipeWireCapture(
            config=CaptureConfig(
                mic=args.mic,
                remote=None,
                app=args.app,
                mic_enabled=True,
                remote_enabled=True,
                latency=args.latency,
            ),
            graph=self._graph,
            launcher=self._launcher,
            linker=self._linker,
            clock=self._clock,
        )

        # Warm the synthesis connection. Measured: the first streaming call of
        # a session takes ~543 ms against a ~267 ms warm median, so without
        # this the FIRST utterance of every call pays ~280 ms extra - and that
        # is the utterance where the user decides whether this works at all.
        # Failure here is not fatal; it is an optimisation, not a dependency.
        try:
            list(synthesizer.synthesize(".", configs[Direction.OUT].voice))
        except Exception as exc:
            log.debug("TTS warm-up failed, first utterance will be slower: %s", exc)

        self._asr_config = AsrConfig(
            project_id=project,
            region=args.region,
            model=args.model,
            phrases=tuple(args.phrases or ()),
        )

    # NOTE: build_recognizer_factory's SpeechClient is never explicitly closed.
    # meetscribe does the same and relies on process teardown, so this is not a
    # regression - but sidetap is the first of the two to have a long-lived
    # Session object that could own it. If a future change makes sessions
    # restartable within one process, close the client here.

    def _make_drop_handler(self, direction: Direction):
        def handler(item) -> None:
            # Counted AND written. meetscribe's equivalent bug was dropping
            # audio with no marker anywhere, so a lost stretch just read as if
            # nobody had been talking.
            self.metrics.add_dropped(direction, 1)
            self.transcript.write(
                Record(unit=item.unit, target_text=item.text, dropped=True)
            )

        return handler

    def start(self) -> None:
        self.capture.start()
        # The routing watcher, for the same reason AppTap has one: a stream
        # that restarts mid-call would otherwise be autoconnected straight to
        # the speakers, unducked and unjournalled.
        self._spawn(self.router.run, (self.stop,), "routing-watch")
        self._spawn(self._poll_capture_health, (self.stop,), "capture-health")
        results: dict[Direction, queue.Queue] = {d: queue.Queue() for d in Direction}

        for direction, pipeline in self.pipelines.items():
            base = self._asr_config
            recognizer_config = AsrConfig(
                project_id=base.project_id,
                region=base.region,
                model=base.model,
                # Each direction listens in the language its own speaker uses,
                # which is what makes language detection unnecessary.
                language_code=self._configs[direction].source_lang,
                phrases=base.phrases,
            )
            worker = RecognitionWorker(
                direction=direction,
                recognizer_factory=self._make_recognizer(recognizer_config, direction),
                # A fresh detector per direction, deliberately: webrtcvad adapts
                # to the noise floor across calls, so a shared instance would
                # let one side's loudness skew the other's classification.
                gate=SilenceGate(webrtc_detector()),
                clock=self._clock,
                on_fatal=self._on_direction_fatal,
            )
            self._spawn(
                worker.run,
                (
                    self.capture.queues[direction.track],
                    results[direction],
                    self.direction_stop[direction],
                ),
                f"asr-{direction.value}",
            )
            self._spawn(
                pipeline.consume, (results[direction], self.stop), f"mt-{direction.value}"
            )
            self._spawn(
                self.playouts[direction].run, (self.stop,), f"playout-{direction.value}"
            )

    def _on_direction_fatal(self, direction: Direction, exc: BaseException) -> None:
        """One direction's recognition died of a configuration error.

        Mark it failed and keep the call up. The existing fail-safes cover
        the user: a dead IN direction still leaves the remote party audible,
        because the duck only closes while speech is being written; a dead OUT
        direction trips DeadAirWatch. So they are told loudly rather than
        having the call dropped out from under them.

        Only when BOTH directions are gone is there nothing left to do.
        """
        self.metrics.set_health(direction, asr=Health.FAILED)
        log.error(
            "%s direction is dead: %s. The call continues one-way; Ctrl-C and "
            "check --%s-lang.",
            direction.value,
            exc,
            "their" if direction is Direction.IN else "my",
        )
        if all(event.is_set() for event in self.direction_stop.values()):
            log.error("both directions are dead; stopping")
            self.stop.set()

    def _poll_capture_health(self, stop: threading.Event) -> None:
        """Two capture failures, both invisible from anywhere else.

        Drops: DroppingQueue only logs. meetscribe's known bug was exactly
        that - a network outage overflowed this queue, the drops were logged,
        and the output carried no marker, so a lost stretch read as nobody
        talking. The lag cap's on_dropped covers the PLAYOUT queue, a
        different queue with a different cause, so it does not help here.

        No audio at all: an unlinked PipeWire capture node delivers ZERO BYTES
        rather than silence. The silence gate has nothing to gate, recognition
        sits idle waiting, and every pane stays green while that direction is
        deaf. The arrival counter failing to advance is the only evidence
        anywhere in the process that this has happened.
        """
        seen = {d: (0, self._clock.monotonic()) for d in Direction}
        while not stop.is_set():
            now = self._clock.monotonic()
            for direction in Direction:
                track_queue = self.capture.queues.get(direction.track)
                if track_queue is None:
                    continue
                self.metrics.set_capture_dropped(direction, track_queue.dropped)

                count, since = seen[direction]
                if track_queue.accepted != count:
                    seen[direction] = (track_queue.accepted, now)
                    self.metrics.set_no_audio(direction, False)
                elif self._armed(direction) and now - since > NO_AUDIO_S:
                    self.metrics.set_no_audio(direction, True)
            stop.wait(1.0)

    def _armed(self, direction: Direction) -> bool:
        """Should silence on this track count as a fault yet?

        The microphone is always capturing, so OUT going quiet is always a
        fault. IN only becomes meaningful once the router has actually rewired
        a stream: before that the application simply is not playing anything,
        which is the ordinary state of having started sidetap before the call.
        Alarming on that would train the user to ignore the one warning that
        matters.
        """
        return direction is Direction.OUT or self.router.has_routed

    def _spawn(self, target, args, name) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def set_bypass(self, value: bool) -> None:
        """Three things at once, or it does not work.

        Un-duck, link the real microphone through, and suppress playout on both
        directions. Without the third, translated speech talks over the
        unmediated conversation bypass exists to step out of. Recognition keeps
        running so the transcript stays continuous.
        """
        for playout in self.playouts.values():
            playout.set_suppressed(value)
        if value:
            # Directly, not by setting a flag for tick() to notice. Bypass is
            # what the user reaches for when the interpretation is making the
            # call worse, and a playout thread whose sink has died never ticks
            # again - which would leave the duck shut and turn the one escape
            # hatch into permanent silence.
            for playout in self.playouts.values():
                if playout.duck is not None:
                    playout.duck.open()
        self.metrics.set_bypassed(value)
        self._link_real_mic(value)
        self._bypassed = value

    def _link_real_mic(self, connected: bool) -> None:
        """Wire the user's real microphone straight into the virtual mic.

        The unlink path replays what was actually linked rather than
        recomputing it from a fresh snapshot. The default source can change
        mid-call - plugging in a headset does it - and recomputing would then
        unlink a pair that was never linked while leaving the real one in
        place. That leak outlives the process, because the virtual mic is a
        permanent node, and nothing records it in the journal, so
        `doctor --repair` cannot find it either.
        """
        if not connected:
            for out_id, in_id in self._real_mic_links:
                self._linker.unlink(out_id, in_id)
            self._real_mic_links.clear()
            return
        if self._real_mic_links:
            return
        snapshot = self._graph.snapshot()
        virtmic = snapshot.node_by_name(VIRTMIC_SINK)
        mic = snapshot.node_by_name(snapshot.default_source or "")
        if virtmic is None or mic is None:
            log.warning(
                "bypass could not find the default microphone, so the other "
                "party will hear nothing from you until you toggle it back"
            )
            return
        outputs = snapshot.ports_of(mic.id, "out")
        inputs = snapshot.ports_of(virtmic.id, "in")
        for index, out_port in enumerate(outputs):
            if not inputs:
                break
            # Same index-pairing limit as routing.engage(): correct for
            # stereo and mono only. See that comment for why.
            in_port = inputs[min(index, len(inputs) - 1)]
            self._linker.link(out_port.id, in_port.id)
            self._real_mic_links.append((out_port.id, in_port.id))

    def alarm_dead_air(self) -> None:
        """Straight to the sink, bypassing the queue.

        An alarm that waited behind the backlog it is warning about would
        arrive after the moment it mattered.
        """
        self.sinks[Direction.IN].write(earcon())

    def _join_workers(self) -> None:
        """ONE shared deadline, not one per thread.

        There are eight of these by the time everything is wired up. A fresh
        timeout each would let shutdown take eight times as long before
        `router.restore()` runs - and restore is what gives the user their
        call audio back. capture.py makes the same argument for its own three
        threads; this is the same reasoning at a larger scale.
        """
        deadline = time.monotonic() + SHUTDOWN_JOIN_S
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        still_running = [t.name for t in self._threads if t.is_alive()]
        if still_running:
            log.warning(
                "threads still running at shutdown: %s - restoring the graph "
                "anyway", ", ".join(still_running)
            )

    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self.stop.set()
        for event in self.direction_stop.values():
            event.set()
        if self._bypassed:
            # Quitting while bypassed must not leave the real microphone wired
            # into the virtual mic. It is not in the journal, the virtual mic
            # outlives the process, and the next call would carry the user's
            # raw voice alongside every translation.
            self._link_real_mic(False)
        try:
            if self.capture is not None:
                self.capture.stop.set()
                self.capture.shutdown()
            self._join_workers()
        except Exception:
            # Whatever went wrong, the graph still has to go back. A
            # half-restored graph leaves the user with no call audio and
            # nothing on screen explaining why.
            log.exception("error during shutdown; restoring the graph anyway")
        finally:
            if self.router is not None:
                # None only when setup() failed before Router was even
                # constructed - the virtual-mic check, deliberately the first
                # thing setup() does, fails before anything is mutated, so
                # there is nothing here to restore.
                try:
                    self.router.restore()
                    self.router_restored = True
                except Exception:
                    log.exception(
                        "could not restore the audio graph. Run: sidetap doctor --repair"
                    )
            if self.transcript is not None:
                self.transcript.close()


def run_session(args, *, graph, launcher, linker, clock, recognizer_factory=None,
                translator=None, synthesizer=None) -> int:
    session = Session(
        args, graph, launcher, linker, clock, recognizer_factory, translator, synthesizer
    )

    def handle_signal(*_):
        session.stop.set()

    # Installed BEFORE setup(), because setup() is where the graph is mutated
    # and it is not instant: it makes a network round-trip to warm TTS. A
    # Ctrl-C in that window would otherwise raise straight out of run_session
    # with the app already rewired into the duck.
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        session.setup()
    except BaseException:
        # setup() is not atomic - engage() lands well before the last sink is
        # spawned. Anything failing after it (a dead pw-cat, KeyboardInterrupt,
        # a bad voice name) would otherwise leave the user's call audio routed
        # into a duck node with the process gone. repair() on the next run
        # fixes it, but "sidetap broke my audio and exited" is not a first-run
        # experience worth shipping.
        session.shutdown()
        raise

    session.start()

    try:
        if args.no_tui:
            _run_headless(session)
        else:
            from .tui import run_tui

            run_tui(session)
    finally:
        session.shutdown()

    print(f"\nSaved:\n  {session.transcript.jsonl_path}\n  {session.transcript.md_path}")
    return 0


def _run_headless(session: Session) -> None:
    alarmed = False
    while not session.stop.is_set():
        session.stop.wait(0.5)
        snapshot = session.metrics.snapshot()
        out = snapshot.directions[Direction.OUT]
        if out.dead_air and not alarmed:
            log.error("DEAD AIR: you are speaking and nothing is reaching the call")
            session.alarm_dead_air()
            alarmed = True
        elif not out.dead_air:
            alarmed = False
