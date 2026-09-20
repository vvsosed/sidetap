import queue
import threading
import time

from sidetap.metrics import Health, Metrics
from sidetap.pipeline import DeadAirWatch, DirectionConfig, DirectionPipeline
from sidetap.playout import Playout, earcon
from sidetap.segment import FinalsOnlySegmenter
from sidetap.types import TTS_BYTES_PER_S, AsrResult, Direction
from tests.conftest import FakeAudioSink, FakeClock, FakeSynthesizer, FakeTranslator


def _config(direction=Direction.IN):
    return DirectionConfig(
        direction=direction,
        source_lang="ru-RU",
        target_lang="en-US",
        voice="en-US-Chirp3-HD-Charon",
    )


def _pipeline(**kwargs):
    defaults = dict(
        config=_config(),
        segmenter=FinalsOnlySegmenter(),
        translator=FakeTranslator(),
        synthesizer=FakeSynthesizer(),
        playout=Playout(Direction.IN, FakeAudioSink()),
        metrics=Metrics(),
        clock=FakeClock(),
        session_t0=0.0,
    )
    defaults.update(kwargs)
    return DirectionPipeline(**defaults)


def _final(text="привет", t_end=1.0):
    return AsrResult(
        direction=Direction.IN, text=text, is_final=True, t_start=t_end, t_end=t_end
    )


def _interim(text="прив"):
    return AsrResult(
        direction=Direction.IN, text=text, is_final=False, t_start=0.5, t_end=0.5
    )


def test_interims_reach_the_segmenter():
    """The seam is decorative unless they do.

    LocalAgreement-2 commits the longest common prefix of two consecutive
    INTERIM hypotheses. If handle() filtered interims out before calling
    feed(), no such segmenter could ever be dropped in behind this Protocol —
    which is the whole justification for the seam existing.
    """

    class RecordingSegmenter:
        def __init__(self):
            self.seen = []

        def feed(self, result):
            self.seen.append(result)
            return []

    segmenter = RecordingSegmenter()
    pipeline = _pipeline(segmenter=segmenter)
    pipeline.handle(_interim("прив"))
    pipeline.handle(_final("привет"))

    assert [r.is_final for r in segmenter.seen] == [False, True]


def test_an_interim_updates_metrics_but_costs_nothing():
    translator = FakeTranslator()
    metrics = Metrics()
    pipeline = _pipeline(translator=translator, metrics=metrics)

    pipeline.handle(_interim("прив"))

    assert metrics.snapshot().directions[Direction.IN].interim == "прив"
    # Interims are the TUI's "they are talking" signal, nothing more. Paying to
    # translate one would be a bug worth a test.
    assert translator.calls == []


def test_a_final_is_translated_synthesised_and_queued():
    translator = FakeTranslator({"привет": "hello"})
    synthesizer = FakeSynthesizer()
    playout = Playout(Direction.IN, FakeAudioSink())
    pipeline = _pipeline(translator=translator, synthesizer=synthesizer, playout=playout)

    pipeline.handle(_final("привет"))

    assert translator.calls == [("привет", "ru-RU", "en-US")]
    assert synthesizer.calls == [("hello", "en-US-Chirp3-HD-Charon")]
    assert playout.backlog_s() > 0


def test_the_final_and_its_translation_reach_metrics():
    metrics = Metrics()
    pipeline = _pipeline(translator=FakeTranslator({"привет": "hello"}), metrics=metrics)
    pipeline.handle(_final("привет"))

    state = metrics.snapshot().directions[Direction.IN]
    assert state.final == "привет"
    assert state.translation == "hello"
    assert state.interim == ""


def test_latency_is_recorded_per_stage():
    clock = FakeClock(start=10.0)

    class TickingTranslator(FakeTranslator):
        def translate(self, text, src, tgt):
            clock.advance(0.2)
            return super().translate(text, src, tgt)

    metrics = Metrics()
    pipeline = _pipeline(translator=TickingTranslator(), metrics=metrics, clock=clock)
    pipeline.handle(_final(t_end=1.0))

    latency = metrics.snapshot().directions[Direction.IN].latency
    assert latency.mt_ms == 200.0
    # asr_ms is the gap between when the audio ended on the session timeline
    # and when its result arrived: 10.0 elapsed - 1.0 audio end = 9000 ms.
    assert latency.asr_ms == 9000.0


def test_a_translation_failure_is_reported_and_survivable():
    metrics = Metrics()
    pipeline = _pipeline(
        translator=FakeTranslator(error=RuntimeError("503")), metrics=metrics
    )
    pipeline.handle(_final())  # must not raise

    assert metrics.snapshot().directions[Direction.IN].mt is Health.FAILED


def test_recovery_clears_the_failed_health():
    metrics = Metrics()
    translator = FakeTranslator(error=RuntimeError("503"))
    pipeline = _pipeline(translator=translator, metrics=metrics)
    pipeline.handle(_final())
    translator.error = None
    pipeline.handle(_final())

    assert metrics.snapshot().directions[Direction.IN].mt is Health.OK


def test_a_synthesis_failure_is_reported_and_survivable():
    metrics = Metrics()
    pipeline = _pipeline(
        synthesizer=FakeSynthesizer(error=RuntimeError("boom")), metrics=metrics
    )
    pipeline.handle(_final())
    assert metrics.snapshot().directions[Direction.IN].tts is Health.FAILED


def test_an_empty_translation_is_not_synthesised():
    synthesizer = FakeSynthesizer()
    pipeline = _pipeline(
        translator=FakeTranslator({"привет": "   "}), synthesizer=synthesizer
    )
    pipeline.handle(_final("привет"))
    assert synthesizer.calls == []


def test_records_are_emitted_for_finals():
    records = []
    pipeline = _pipeline(
        translator=FakeTranslator({"привет": "hello"}), on_record=records.append
    )
    pipeline.handle(_final("привет"))
    assert len(records) == 1
    assert records[0].unit.text == "привет"
    assert records[0].target_text == "hello"
    assert records[0].dropped is False


def test_consume_drains_the_queue_until_stopped():
    results: queue.Queue = queue.Queue()
    results.put(_final("привет"))
    metrics = Metrics()
    pipeline = _pipeline(metrics=metrics)
    stop = threading.Event()

    thread = threading.Thread(target=pipeline.consume, args=(results, stop))
    thread.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if metrics.snapshot().directions[Direction.IN].final:
            break
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=2)

    assert metrics.snapshot().directions[Direction.IN].final == "привет"


# --- dead air ---------------------------------------------------------------


def test_dead_air_does_not_alarm_before_the_threshold():
    clock = FakeClock()
    watch = DeadAirWatch(clock, threshold_s=6.0)
    watch.heard_speech()
    clock.advance(5.0)
    assert watch.alarming() is False


def test_dead_air_alarms_past_the_threshold():
    clock = FakeClock()
    watch = DeadAirWatch(clock, threshold_s=6.0)
    watch.heard_speech()
    clock.advance(7.0)
    assert watch.alarming() is True


def test_speaking_clears_the_pending_alarm():
    clock = FakeClock()
    watch = DeadAirWatch(clock, threshold_s=6.0)
    watch.heard_speech()
    clock.advance(3.0)
    watch.spoke()
    clock.advance(10.0)
    assert watch.alarming() is False


def test_the_clock_starts_at_the_first_unanswered_result_only():
    """A second result while one is already pending must not reset the timer."""
    clock = FakeClock()
    watch = DeadAirWatch(clock, threshold_s=6.0)
    watch.heard_speech()
    clock.advance(4.0)
    watch.heard_speech()
    clock.advance(3.0)
    assert watch.alarming() is True


def test_earcon_is_audible_and_the_right_length():
    pcm = earcon(duration_s=0.2)
    assert len(pcm) == int(TTS_BYTES_PER_S * 0.2)
    assert pcm != b"\x00" * len(pcm)


# --- cost accounting ---------------------------------------------------------


def test_translation_and_synthesis_are_billed():
    from sidetap.cost import Rates

    metrics = Metrics()
    pipeline = _pipeline(
        translator=FakeTranslator({"привет": "hello"}),
        metrics=metrics,
        rates=Rates(mt_per_million_chars=1_000_000.0, tts_per_million_chars=1_000_000.0),
    )
    pipeline.handle(_final("привет"))
    # 6 source characters translated + 5 target characters synthesised, at
    # $1 per character.
    assert metrics.snapshot().cost_usd == 11.0


def test_a_failed_translation_bills_nothing():
    from sidetap.cost import Rates

    metrics = Metrics()
    pipeline = _pipeline(
        translator=FakeTranslator(error=RuntimeError("503")),
        metrics=metrics,
        rates=Rates(mt_per_million_chars=1_000_000.0),
    )
    pipeline.handle(_final("привет"))
    assert metrics.snapshot().cost_usd == 0.0


# --- streaming synthesis ------------------------------------------------


def test_audio_reaches_playout_before_synthesis_finishes():
    seen = []

    class SlowSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            yield b"\x01\x02" * 8000
            seen.append(playout.backlog_s())   # first chunk already queued
            yield b"\x03\x04" * 8000

    playout = Playout(Direction.IN, FakeAudioSink())
    pipeline = _pipeline(synthesizer=SlowSynthesizer(), playout=playout)
    pipeline.handle(_final(t_end=1.0))

    assert seen and seen[0] > 0.0


def test_tts_latency_is_time_to_the_first_chunk_not_the_whole_synthesis():
    clock = FakeClock(start=10.0)

    class TickingSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            clock.advance(0.2)          # time to first chunk
            yield b"\x01\x02" * 8000
            clock.advance(2.0)          # the rest of the synthesis
            yield b"\x03\x04" * 8000

    metrics = Metrics()
    pipeline = _pipeline(
        synthesizer=TickingSynthesizer(), metrics=metrics, clock=clock
    )
    pipeline.handle(_final(t_end=1.0))

    latency = metrics.snapshot().directions[Direction.IN].latency
    assert latency.tts_ms == 200.0
    assert latency.tts_total_ms == 2200.0


def test_a_synthesis_failure_part_way_through_keeps_what_was_spoken():
    records = []

    class FailingSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            yield b"\x01\x02" * 8000
            raise RuntimeError("stream died")

    playout = Playout(Direction.IN, FakeAudioSink())
    pipeline = _pipeline(
        synthesizer=FailingSynthesizer(), playout=playout, on_record=records.append
    )
    pipeline.handle(_final(t_end=1.0))

    assert playout.backlog_s() > 0.0        # the first chunk is still spoken
    assert len(records) == 1
    assert records[0].truncated is True


def test_a_synthesis_failure_before_any_audio_records_nothing():
    records = []

    class FailingSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            raise RuntimeError("stream died")
            yield b""                        # pragma: no cover - generator marker

    playout = Playout(Direction.IN, FakeAudioSink())
    pipeline = _pipeline(
        synthesizer=FailingSynthesizer(), playout=playout, on_record=records.append
    )
    pipeline.handle(_final(t_end=1.0))

    assert playout.backlog_s() == 0.0
    assert records == []
