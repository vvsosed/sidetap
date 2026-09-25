"""Build a session, run it, and give the audio graph back."""

from __future__ import annotations

import logging
import queue
import signal
import threading
import time

from .adapters import PwCatSink, PwLoopbackFactory, WpctlVolumeControl
from .asr import AsrConfig, RecognitionWorker, build_recognizer_factory, project_from_environment
from .capture import CaptureConfig, CaptureError, PipeWireCapture, resolve_mic
from .cost import Rates
from .metrics import Health, Metrics
from .pipeline import DeadAirWatch, DirectionConfig, DirectionPipeline
from .playout import DuckControl, Playout, earcon
from .ports import LinkResult
from .routing import JOURNAL_PATH, VIRTMIC_SINK, Router
from .segment import FinalsOnlySegmenter, LocalAgreementSegmenter
from .transcript import BilingualTranscript
from .translate import TranslateConfig, build_translator
from .tts import TtsConfig, build_synthesizer
from .types import LAG_CAP_S, NO_AUDIO_S, TTS_RATE, Direction, Record
from .vad import SilenceGate, webrtc_detector

log = logging.getLogger(__name__)

SHUTDOWN_JOIN_S = 3.0

# Every signal that asks the process to end. SIGHUP is closing the terminal:
# left at its default it kills Python without running a single finally, so the
# graph is never restored and the call stays routed into a duck that may be at
# 0%.
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)

# One male and one female voice per language this has been run against. Any
# other language must name its voice explicitly, because an invalid voice is a
# fatal InvalidArgument at the first utterance.
#
# Charon and Kore because they exist in every locale: ru-RU has only 8 Chirp 3
# HD voices where others have 30 (docs/experiments/05-voice-gender.md).
#
# "default" applies with no --voice-*-gender. It is deliberately not uniform:
# in an en-US <-> ru-RU call you hear a male voice and they hear a female one,
# which makes the two directions easy to tell apart.
VOICES = {
    "en-US": {"male": "en-US-Chirp3-HD-Charon",
              "female": "en-US-Chirp3-HD-Kore", "default": "male"},
    "en-GB": {"male": "en-GB-Chirp3-HD-Charon",
              "female": "en-GB-Chirp3-HD-Kore", "default": "male"},
    "ru-RU": {"male": "ru-RU-Chirp3-HD-Charon",
              "female": "ru-RU-Chirp3-HD-Kore", "default": "female"},
    "uk-UA": {"male": "uk-UA-Chirp3-HD-Charon",
              "female": "uk-UA-Chirp3-HD-Kore", "default": "female"},
    "de-DE": {"male": "de-DE-Chirp3-HD-Charon",
              "female": "de-DE-Chirp3-HD-Kore", "default": "female"},
    "es-ES": {"male": "es-ES-Chirp3-HD-Charon",
              "female": "es-ES-Chirp3-HD-Kore", "default": "female"},
    "fr-FR": {"male": "fr-FR-Chirp3-HD-Charon",
              "female": "fr-FR-Chirp3-HD-Kore", "default": "female"},
    "pl-PL": {"male": "pl-PL-Chirp3-HD-Charon",
              "female": "pl-PL-Chirp3-HD-Kore", "default": "female"},
}


def default_voice(language_code: str, gender: str | None = None) -> str:
    """The voice for a language, optionally forced to a gender.

    gender=None picks the language's default.
    """
    try:
        entry = VOICES[language_code]
    except KeyError:
        raise RuntimeError(
            f"no default Chirp 3 HD voice for {language_code!r}. Pass --voice-in "
            "or --voice-out explicitly; see the Chirp 3 HD voice list."
        ) from None
    return entry[gender or entry["default"]]


def build_direction_configs(args) -> dict[Direction, DirectionConfig]:
    return {
        Direction.IN: DirectionConfig(
            direction=Direction.IN,
            source_lang=args.their_lang,
            target_lang=args.my_lang,
            voice=args.voice_in or default_voice(args.my_lang, _gender(args, "in")),
            speaking_rate=_rate(args, "in"),
        ),
        Direction.OUT: DirectionConfig(
            direction=Direction.OUT,
            source_lang=args.my_lang,
            target_lang=args.their_lang,
            voice=args.voice_out
            or default_voice(args.their_lang, _gender(args, "out")),
            speaking_rate=_rate(args, "out"),
        ),
    }


def _rate(args, side: str) -> float:
    return getattr(args, f"speaking_rate_{side}", 1.0)


def _gender(args, side: str) -> str | None:
    return getattr(args, f"voice_{side}_gender", None)


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
        session_name=None,
    ):
        self._args = args
        self._graph = graph
        self._launcher = launcher
        self._linker = linker
        self._clock = clock
        self._make_recognizer = recognizer_factory or build_recognizer_factory
        self._translator = translator
        self._synthesizer = synthesizer
        # Injectable, because WpctlVolumeControl runs wpctl against the real
        # machine.
        self._volume = volume or WpctlVolumeControl()
        # Injectable so tests never write the real journal under the user's
        # home. Production keeps routing.JOURNAL_PATH, where `doctor --repair`
        # looks.
        self._journal_path = journal_path if journal_path is not None else JOURNAL_PATH
        # Shared with the log file so a run's three artifacts sort together.
        self._session_name = session_name

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
        # shutdown() is reachable from the signal handler, the TUI and
        # run_session's finally, and set_bypass can race it over the same
        # links.
        self._lifecycle_lock = threading.RLock()
        self._bypassed = False
        # Tracked here, not read off the OUT playout, which bypass also
        # suppresses: read from there, "muted" and "bypassed" look the same and
        # leaving bypass would unmute.
        self._muted_out = False
        self._real_mic_links: list[tuple[int, int]] = []
        # None until setup() creates them. setup() can fail before Router
        # exists - the virtual-mic check runs first so a failure has nothing to
        # undo - and shutdown() must cope rather than bury the original error.
        self.router = None
        self.transcript = None
        self.capture = None

    def setup(self) -> None:
        args = self._args
        project = args.project or project_from_environment()
        configs = build_direction_configs(args)
        self._configs = configs

        # Before the router touches anything: engage() is the first
        # irreversible step, and failing after it would leave the call rewired
        # with setup() half done.
        snapshot = self._graph.snapshot()
        virtmic = snapshot.node_by_name(VIRTMIC_SINK)
        if virtmic is None:
            # Not a fallback: pw-cat with no --target plays to the default
            # sink, so the outbound translation would come out of the user's
            # speakers while the remote party heard silence.
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

        self.transcript = BilingualTranscript(args.out, session=self._session_name)

        # One cost estimate for the whole call, shared by both pipelines and
        # both recognition workers.
        self.rates = Rates()

        # The NMT fallback is sticky and otherwise invisible, because the next
        # successful call turns the mt marker green again.
        self.metrics.set_mt_model(args.mt_model)
        translator = self._translator or build_translator(
            TranslateConfig(
                project_id=project, region=args.mt_region, model=args.mt_model
            ),
            on_downgrade=self.metrics.set_mt_model,
        )
        synthesizer = self._synthesizer or build_synthesizer(
            # No rate here: it is passed per utterance, because the two
            # directions differ.
            TtsConfig(region=args.tts_region)
        )

        default_sink = snapshot.node_by_name(snapshot.default_sink or "")
        targets = {
            Direction.IN: default_sink.serial if default_sink else None,
            Direction.OUT: virtmic.serial,
        }

        self._dead_air = DeadAirWatch(self._clock)
        lag_cap = args.lag_cap if args.lag_cap is not None else LAG_CAP_S
        # One origin for both directions, so the transcript's columns line up.
        session_t0 = self._clock.monotonic()

        for direction, config in configs.items():
            # No `duck_id is not None` check: engage() returns before
            # pw-loopback registers the duck, so the id is still None here and
            # arrives on a later poll. The callable lets it arrive late.
            duck = (
                DuckControl(self._volume, lambda: self.router.duck_id)
                if direction is Direction.IN
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
                # One per direction: LocalAgreementSegmenter holds
                # per-utterance state.
                segmenter=(
                    FinalsOnlySegmenter()
                    if getattr(args, "no_early_commit", False)
                    else LocalAgreementSegmenter()
                ),
                translator=translator,
                synthesizer=synthesizer,
                playout=playout,
                metrics=self.metrics,
                clock=self._clock,
                session_t0=session_t0,
                on_record=self.transcript.write,
                dead_air=self._dead_air if direction is Direction.OUT else None,
                rates=self.rates,
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

        # Warm the synthesis connection: the first streaming call takes
        # ~543 ms against a ~267 ms warm median, and the first utterance is
        # when the user judges whether this works. Best effort only.
        try:
            list(
                synthesizer.synthesize(
                    ".",
                    configs[Direction.OUT].voice,
                    configs[Direction.OUT].speaking_rate,
                )
            )
        except Exception as exc:
            log.debug("TTS warm-up failed, first utterance will be slower: %s", exc)

        self._asr_config = AsrConfig(
            project_id=project,
            region=args.region,
            model=args.model,
            phrases=tuple(args.phrases or ()),
        )

    # NOTE: build_recognizer_factory's SpeechClient is never closed explicitly;
    # process teardown does it. Close it here if sessions ever become
    # restartable within one process.

    def _make_drop_handler(self, direction: Direction):
        def handler(item) -> None:
            # Counted and written, so a lost stretch is marked rather than
            # reading as silence.
            self.metrics.add_dropped(direction, 1)
            self.transcript.write(
                Record(unit=item.unit, target_text=item.text, dropped=True)
            )

        return handler

    def start(self) -> None:
        self.capture.start()
        # A stream that restarts mid-call would otherwise be autoconnected to
        # the speakers, unducked and unjournalled.
        self._spawn(self.router.run, (self.stop,), "routing-watch")
        self._spawn(self._poll_health, (self.stop,), "health")
        results: dict[Direction, queue.Queue] = {d: queue.Queue() for d in Direction}

        for direction, pipeline in self.pipelines.items():
            base = self._asr_config
            recognizer_config = AsrConfig(
                project_id=base.project_id,
                region=base.region,
                model=base.model,
                # Each direction listens in its own speaker's language, so
                # nothing needs language detection.
                language_code=self._configs[direction].source_lang,
                phrases=base.phrases,
            )
            worker = RecognitionWorker(
                direction=direction,
                recognizer_factory=self._make_recognizer(recognizer_config, direction),
                # A fresh detector per direction: webrtcvad adapts to the noise
                # floor, and a shared one would let one side skew the other.
                gate=SilenceGate(webrtc_detector()),
                clock=self._clock,
                on_fatal=self._on_direction_fatal,
                on_audio_sent=lambda seconds: self.metrics.add_cost(
                    self.rates.recognition_usd(seconds)
                ),
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

        Mark it failed and keep the call up: a dead IN still leaves the remote
        party audible, because the duck only closes while speech is written,
        and a dead OUT trips DeadAirWatch. Stop only once both are dead.
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

    def _poll_health(self, stop: threading.Event) -> None:
        """Watch for failures nothing else can see.

        Drops: DroppingQueue only logs, so an overflow would leave a gap that
        reads as nobody talking. The lag cap's on_dropped covers the playout
        queue, not this one.

        No audio: an unlinked capture node delivers zero bytes, not silence,
        so every pane stays green while that direction is deaf. A stalled
        arrival counter is the only evidence.

        Dead playback: PwCatSink only logs, to a log the TUI hides.

        Dead air: consume() checks it only between results, so a stage hung
        inside handle() would otherwise never raise the alarm.
        """
        seen = {d: (0, self._clock.monotonic()) for d in Direction}
        while not stop.is_set():
            now = self._clock.monotonic()
            for direction, playout in self.playouts.items():
                self.metrics.set_playback_failed(direction, playout.sink_failed)
            for pipeline in self.pipelines.values():
                pipeline.check_dead_air()
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

        The mic always captures, so OUT going quiet is always a fault. IN
        counts only once the router has rewired a stream: starting sidetap
        before the call is normal, and alarming on it would teach the user to
        ignore the warning.
        """
        return direction is Direction.OUT or self.router.has_routed

    def _spawn(self, target, args, name) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def set_bypass(self, value: bool) -> None:
        """Un-duck, link the real microphone through, and suppress playout.

        All three, or it does not work: without suppression, translated speech
        talks over the unmediated call bypass exists to step out of.
        Recognition keeps running so the transcript stays continuous.
        """
        with self._lifecycle_lock:
            self._set_bypass_locked(value)

    def _set_bypass_locked(self, value: bool) -> None:
        self._apply_suppression_locked(value)
        if value:
            # Open the duck directly rather than via tick(): a playout thread
            # blocked in a sink write cannot tick, and bypass - the escape
            # hatch - would leave the duck shut.
            for playout in self.playouts.values():
                if playout.duck is not None:
                    playout.duck.open()
        self.metrics.set_bypassed(value)
        # Set before linking: _link_real_mic can raise partway, and a live link
        # with _bypassed False would be skipped by shutdown and, unjournalled,
        # never cleaned up. Claiming bypass early only costs a no-op cleanup.
        if value:
            self._bypassed = True
        try:
            self._link_real_mic(value)
        finally:
            if not value and not self._real_mic_links:
                self._bypassed = False

    def set_mute_out(self, value: bool) -> None:
        """Stop sending your translated voice without leaving the call.

        Recognition and translation keep running; only OUT playout is
        suppressed. While bypassed this changes what you return to, not the
        current state, since bypass already suppresses OUT.
        """
        with self._lifecycle_lock:
            # Flag and metric together, before acting, so the lit key and the
            # next keypress cannot disagree.
            self._muted_out = value
            self.metrics.set_muted_out(value)
            self._apply_suppression_locked(self._bypassed)

    def _apply_suppression_locked(self, bypassed: bool) -> None:
        """OUT is suppressed if either control says so; IN only by bypass.

        Takes `bypassed` rather than reading self._bypassed, which
        _set_bypass_locked writes only after this runs.

        Acts only on a real change, because set_suppressed() flushes and a
        flush cuts the current utterance short.
        """
        for direction, want in (
            (Direction.IN, bypassed),
            (Direction.OUT, bypassed or self._muted_out),
        ):
            playout = self.playouts[direction]
            if playout.suppressed != want:
                playout.set_suppressed(want)

    def _link_real_mic(self, connected: bool) -> None:
        """Wire the user's real microphone straight into the virtual mic.

        Unlinking replays what was linked rather than recomputing from a fresh
        snapshot: the default source can change mid-call (a headset is plugged
        in), and recomputing would leave the real link in place. That link
        outlives the process and is not journalled, so `doctor --repair`
        cannot find it.
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
        try:
            # The mic capture uses, and never the virtual mic itself: linking
            # that into its own sink would loop sidetap's output back in.
            mic = resolve_mic(snapshot, self._args.mic)
        except CaptureError as exc:
            log.warning("%s", exc)
            mic = None
        if virtmic is None or mic is None:
            log.warning(
                "bypass could not find your microphone, so the other party "
                "will hear nothing from you until you toggle it back"
            )
            return
        outputs = snapshot.ports_of(mic.id, "out")
        inputs = snapshot.ports_of(virtmic.id, "in")
        failed = False
        for index, out_port in enumerate(outputs):
            if not inputs:
                break
            # Index pairing, as in routing: correct for mono and stereo only.
            in_port = inputs[min(index, len(inputs) - 1)]
            pair = (out_port.id, in_port.id)
            # Recorded before the attempt, withdrawn only on definite failure.
            # Shutdown replays this list: unlinking a pair never linked is
            # harmless, missing a live one leaves the raw mic wired for good.
            self._real_mic_links.append(pair)
            # Checked: bypass has already suppressed both playouts, so a
            # silent failure means the remote party hears nothing while bypass
            # looks engaged.
            if self._linker.link(*pair) is LinkResult.FAILED:
                self._real_mic_links.remove(pair)
                failed = True
        if failed:
            log.error(
                "bypass could not connect your microphone to the virtual mic, "
                "so the other party is hearing silence. Toggle bypass off to "
                "restore the interpretation, or check `pw-link -l`."
            )

    def alarm_dead_air(self) -> None:
        """Write straight to the sink, bypassing the queue.

        An alarm queued behind the backlog it warns about would arrive late.
        """
        self.sinks[Direction.IN].write(earcon())

    def _join_workers(self) -> None:
        """Join the workers against ONE shared deadline, not one per thread.

        Eight separate timeouts would delay `router.restore()`, which is what
        gives the user their call audio back.
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
        with self._lifecycle_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        self.stop.set()
        for event in self.direction_stop.values():
            event.set()
        with self._lifecycle_lock:
            if self._bypassed or self._real_mic_links:
                # Never leave the real mic wired into the virtual mic: the
                # link is not journalled and the virtual mic outlives the
                # process. _real_mic_links is checked too, since a set_bypass
                # that raised partway can leave links behind either flag.
                try:
                    self._link_real_mic(False)
                except Exception:
                    log.exception(
                        "could not unlink the real microphone from the virtual "
                        "mic; run: pw-link -l and remove it by hand"
                    )
        try:
            if self.capture is not None:
                self.capture.stop.set()
                self.capture.shutdown()
            self._join_workers()
        except Exception:
            # Whatever went wrong, the graph still has to go back.
            log.exception("error during shutdown; restoring the graph anyway")
        finally:
            if self.router is not None:
                # None only if setup() failed at the virtual-mic check, before
                # anything was mutated.
                try:
                    self.router.restore()
                    self.router_restored = True
                except Exception:
                    log.exception(
                        "could not restore the audio graph. Run: sidetap doctor --repair"
                    )
            # Closed here, not in the playout threads' finally: those exist
            # only after start(), and pw-cat runs in its own session, so an
            # orphan would outlive this process.
            for sink in self.sinks.values():
                try:
                    sink.close()
                except Exception:
                    log.debug("could not close a playback sink", exc_info=True)
            if self.transcript is not None:
                self.transcript.close()


def run_session(args, *, graph, launcher, linker, clock, recognizer_factory=None,
                translator=None, synthesizer=None, session=None) -> int:
    session = Session(
        args, graph, launcher, linker, clock, recognizer_factory, translator,
        synthesizer, session_name=session,
    )

    def handle_signal(*_):
        session.stop.set()

    # Installed before setup(), which rewires the graph and then waits on a
    # network round-trip to warm TTS; a signal there should request a stop,
    # not raise mid-setup. Put back afterwards, for callers that live on.
    previous = {sig: signal.signal(sig, handle_signal) for sig in STOP_SIGNALS}
    try:
        return _run(session, args)
    finally:
        for sig, handler in previous.items():
            if handler is not None:
                signal.signal(sig, handler)


def _run(session: Session, args) -> int:
    try:
        session.setup()
    except BaseException:
        # setup() is not atomic: engage() rewires the graph well before the
        # last sink is spawned, so any later failure must restore it.
        session.shutdown()
        raise

    # start() shares the guard: it runs after engage() and can fail before any
    # thread exists (a capture-node timeout, a SpeechClient that fails to
    # build), which must not leave the call routed into the duck.
    try:
        # A stop requested during setup means quit, not start the pipeline.
        if not session.stop.is_set():
            session.start()

            if args.no_tui:
                _run_headless(session)
            else:
                from .tui import run_tui

                run_tui(session)
    finally:
        session.shutdown()

    saved = [session.transcript.jsonl_path, session.transcript.md_path]
    log_path = session.transcript.jsonl_path.with_suffix(".log")
    if log_path.exists():
        saved.append(log_path)
    print("\nSaved:")
    for path in saved:
        print(f"  {path}")
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
