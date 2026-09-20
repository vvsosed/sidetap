import queue
import threading
import time

from sidetap.metrics import Health, Metrics
from sidetap.pipeline import DeadAirWatch, DirectionConfig, DirectionPipeline
from sidetap.playout import STARVE_LIMIT_TICKS, Playout, earcon
from sidetap.segment import FinalsOnlySegmenter
from sidetap.types import TTS_BYTES_PER_S, AsrResult, Direction, Latency
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
    """The listener heard 200ms of this sentence before it cut off, and that
    latency must reach the record - it is what distinguishes this case from
    the starvation-refusal one below, where nothing was ever heard at all.

    Advancing the clock before the first chunk is what makes that
    distinction checkable: with a clock that never advances, a record with
    no latency and a record with a real one are indistinguishable, and the
    `and first_ms is None` guard on the starvation-refusal branch could be
    dropped without any test noticing - the record would still exist,
    still be truncated, and still fail to carry the latency it should not
    have had in the first place.
    """
    clock = FakeClock(start=0.0)
    records = []

    class FailingSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            clock.advance(0.2)
            yield b"\x01\x02" * 8000
            raise RuntimeError("stream died")

    playout = Playout(Direction.IN, FakeAudioSink())
    pipeline = _pipeline(
        synthesizer=FailingSynthesizer(),
        playout=playout,
        on_record=records.append,
        clock=clock,
    )
    pipeline.handle(_final(t_end=0.0))

    assert playout.backlog_s() > 0.0        # the first chunk is still spoken
    assert len(records) == 1
    assert records[0].truncated is True
    assert records[0].latency.tts_ms == 200.0


def test_a_synthesis_failure_before_any_audio_records_nothing():
    from sidetap.cost import Rates

    records = []
    metrics = Metrics()

    class FailingSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            raise RuntimeError("stream died")
            yield b""                        # pragma: no cover - generator marker

    playout = Playout(Direction.IN, FakeAudioSink())
    pipeline = _pipeline(
        synthesizer=FailingSynthesizer(),
        playout=playout,
        on_record=records.append,
        metrics=metrics,
        rates=Rates(mt_per_million_chars=1_000_000.0, tts_per_million_chars=1_000_000.0),
    )
    pipeline.handle(_final(t_end=1.0))

    assert playout.backlog_s() == 0.0
    assert records == []
    # Only the 6-character source text is billed, at $1/char - nothing was
    # ever produced, so the synthesis request must not be charged. This is
    # exactly what `if produced:` guards, and a bare `if True:` there used
    # to sail straight through this test undetected.
    assert metrics.snapshot().cost_usd == 6.0


# --- playout refusing mid-stream --------------------------------------------
#
# append() returning False - the `refused` path - has two causes: bypass
# flushing the queue (dropped) and playout abandoning a stalled utterance at
# the starvation bound (closed via _advance_locked, not via an exception).
# Neither is reachable by raising in the fake synthesizer, so these drive the
# real Playout instance directly, the same way the production consumer of
# these APIs (the playout thread and Session.set_bypass) would.


def test_a_refusal_mid_stream_does_not_paint_tts_health_ok():
    """Only a generator that runs to completion is evidence TTS is healthy.

    The first utterance fails outright (tts -> FAILED). The second succeeds
    partway, then bypass flushes it mid-stream - a refusal, not an exception.
    That must not repaint the health marker green: the TUI's tts indicator
    exists to show the pipeline failing, and a refusal is exactly a case
    where the listener did not hear the whole sentence.
    """
    metrics = Metrics()
    playout = Playout(Direction.IN, FakeAudioSink())

    class StatefulSynthesizer:
        def __init__(self):
            self.calls = 0

        def synthesize(self, text, voice, speaking_rate=1.0):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
                yield b""  # pragma: no cover - generator marker
            yield b"\x01\x02" * 8000
            playout.set_suppressed(True)  # bypass flushes the queued handle
            yield b"\x03\x04" * 8000

    pipeline = _pipeline(
        synthesizer=StatefulSynthesizer(), playout=playout, metrics=metrics
    )
    pipeline.handle(_final("first", t_end=1.0))
    pipeline.handle(_final("second", t_end=2.0))

    assert metrics.snapshot().directions[Direction.IN].tts is Health.FAILED


def test_a_playout_side_truncation_is_recorded_though_the_producer_finished_cleanly():
    """finish()'s own truncated flag must survive into the Record.

    Playout gives up on a queued utterance that sits under the 400ms start
    threshold for STARVE_LIMIT_TICKS - closing it in place as truncated, with
    no exception anywhere in the producer. The producer's own `truncated`
    local stays False the whole time, so only re-reading it off the handle
    after finish() reports what actually happened to the listener.
    """
    records = []
    playout = Playout(Direction.IN, FakeAudioSink())

    class StallingSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            yield b"\x01\x02" * 8000  # 0.33s of audio: under the 0.4s start buffer
            for _ in range(STARVE_LIMIT_TICKS):
                playout.tick()  # playout gives up on the still-queued head
            yield b"\x03\x04" * 8000

    pipeline = _pipeline(
        synthesizer=StallingSynthesizer(), playout=playout, on_record=records.append
    )
    pipeline.handle(_final(t_end=1.0))

    assert len(records) == 1
    assert records[0].truncated is True


def test_a_bypass_flushed_utterance_still_gets_a_transcript_row_and_a_bill():
    """dropped is a marker on the row, not a reason to omit it - or the bill.

    Returning silently on a bypass flush loses the SOURCE line too: the
    bilingual transcript would read as if the remote party never said
    anything during that stretch. And Chirp 3 HD sends the whole input
    string before its first chunk comes back, so the request was already
    billed regardless of what playout did with the audio afterward.

    (Supersedes this suite's earlier version of this test, which asserted
    the opposite - that bypass produced no record and no cost. Quality
    review found that was itself the bug: CLAUDE.md's own drop handler in
    run.py exists specifically to record a drop rather than lose the row.)
    """
    from sidetap.cost import Rates

    records = []
    metrics = Metrics()
    playout = Playout(Direction.IN, FakeAudioSink())

    class FlushingSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            yield b"\x01\x02" * 8000
            playout.set_suppressed(True)
            yield b"\x03\x04" * 8000

    pipeline = _pipeline(
        translator=FakeTranslator({"привет": "hello"}),
        synthesizer=FlushingSynthesizer(),
        playout=playout,
        metrics=metrics,
        on_record=records.append,
        rates=Rates(mt_per_million_chars=1_000_000.0, tts_per_million_chars=1_000_000.0),
    )
    pipeline.handle(_final("привет"))

    assert len(records) == 1
    assert records[0].dropped is True
    assert records[0].unit.text == "привет"
    assert records[0].target_text == "hello"
    # 6 source characters + 5 target characters, at $1 each: bypass still
    # bills the synthesis request that had already gone out.
    assert metrics.snapshot().cost_usd == 11.0


# --- defects found by quality review, reproduced and fixed here ------------


def test_synthesize_raising_before_any_iteration_still_closes_the_utterance():
    """begin() must always be paired with finish(), even when synthesize()
    itself raises before yielding - not just when a generator raises during
    iteration.

    ports.py types Synthesizer as a bare Iterator, which permits a
    non-generator implementation whose call can raise immediately, unlike
    every fake elsewhere in this file (all generator functions, whose body
    cannot execute - let alone raise - before the first iteration). Missing
    this orphans the utterance open in the queue: blocking the queue head
    and switching the lag cap off for this direction until the 2s starvation
    bound eventually closes it.
    """

    class RaisesAtCallTime:
        def synthesize(self, text, voice, speaking_rate=1.0):
            raise RuntimeError("boom")

    playout = Playout(Direction.IN, FakeAudioSink())
    pipeline = _pipeline(synthesizer=RaisesAtCallTime(), playout=playout)
    pipeline.handle(_final(t_end=1.0))  # must not raise, and must not orphan

    assert playout.backlog_s() == 0.0


def test_a_chunk_refused_on_arrival_produces_a_truncated_row_with_no_latency_and_leaves_dead_air_armed():
    """first_ms means playout ACCEPTED a chunk, not that the synthesizer
    produced one.

    Reproduces the review's scenario exactly: the starvation bound closes an
    empty, still-queued utterance after STARVE_LIMIT_TICKS, and only then
    does the one and only chunk arrive - too late, refused on item.closed.
    Nothing ever reached playout, so nothing was heard: first_ms must stay
    unset and dead air must stay armed rather than being told the direction
    spoke. But the sentence was said, and was billed, so it still needs a
    transcript row - just one with no latency to report, since there is
    nothing playout ever queued to time.

    The translator advances the clock so the latency assertion is
    falsifiable: with a clock that never moves, `Latency(asr_ms=0, mt_ms=0)`
    equals `Latency()` regardless of whether the code actually withheld a
    real one, and the assertion would pin nothing. It also gives the
    translation a distinct value from the source text, so the record can be
    checked against both - a target_text that silently became the source
    text (round 1's begin(unit, unit.text) mutation, reachable here too)
    would otherwise pass unnoticed.
    """
    clock = FakeClock()
    watch = DeadAirWatch(clock, threshold_s=6.0)
    watch.heard_speech()

    class TickingTranslator(FakeTranslator):
        def translate(self, text, src, tgt):
            clock.advance(0.3)
            return super().translate(text, src, tgt)

    records = []
    playout = Playout(Direction.IN, FakeAudioSink())

    class DelayedSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            for _ in range(STARVE_LIMIT_TICKS):
                playout.tick()  # the still-empty queued head starves and closes
            yield b"\x01\x02" * 8000  # arrives too late: refused on item.closed

    pipeline = _pipeline(
        translator=TickingTranslator({"привет": "hello"}),
        synthesizer=DelayedSynthesizer(),
        playout=playout,
        clock=clock,
        dead_air=watch,
        on_record=records.append,
    )
    pipeline.handle(_final("привет", t_end=0.0))

    assert playout.backlog_s() == 0.0
    assert len(records) == 1
    assert records[0].truncated is True
    assert records[0].dropped is False
    assert records[0].unit.text == "привет"
    assert records[0].target_text == "hello"
    assert records[0].latency == Latency()
    clock.advance(7.0)
    assert watch.alarming() is True


def test_a_lag_cap_drop_reports_the_translation_not_the_source():
    """begin() must queue the TARGET text, not the unit's source text.

    Playout only ever sees what begin() hands it, and a lag-cap drop reports
    that text straight to the transcript via run.py's drop handler. Filing
    the source text there would print the remote party's own sentence back
    as if it were the translation.
    """
    dropped = []
    playout = Playout(
        Direction.IN, FakeAudioSink(), on_dropped=dropped.append, lag_cap_s=0.3
    )

    class FixedChunkSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            yield b"\x01\x02" * 10000  # 20000 bytes ~= 0.417s: over the 0.3s cap alone

    translator = FakeTranslator({"первый": "first", "второй": "second"})
    pipeline = _pipeline(
        translator=translator, synthesizer=FixedChunkSynthesizer(), playout=playout
    )
    pipeline.handle(_final("первый", t_end=1.0))
    pipeline.handle(_final("второй", t_end=2.0))

    assert len(dropped) == 1
    assert dropped[0].text == "first"


def test_both_refusal_causes_bill_the_same():
    """Bypass-flush and starvation-close must cost the same.

    Chirp 3 HD sends the whole input string before its first chunk comes
    back, so Google bills the request the moment it goes out, regardless of
    which refusal follows. Billing one cause and not the other would
    understate - or overstate - the call's real cost depending on which
    kind of refusal happened to occur.
    """
    from sidetap.cost import Rates

    rates = Rates(mt_per_million_chars=1_000_000.0, tts_per_million_chars=1_000_000.0)

    bypass_metrics = Metrics()
    bypass_playout = Playout(Direction.IN, FakeAudioSink())

    class FlushingSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            yield b"\x01\x02" * 8000
            bypass_playout.set_suppressed(True)
            yield b"\x03\x04" * 8000

    bypass_pipeline = _pipeline(
        translator=FakeTranslator({"привет": "hello"}),
        synthesizer=FlushingSynthesizer(),
        playout=bypass_playout,
        metrics=bypass_metrics,
        rates=rates,
    )
    bypass_pipeline.handle(_final("привет"))

    starve_metrics = Metrics()
    starve_playout = Playout(Direction.IN, FakeAudioSink())

    class DelayedSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            for _ in range(STARVE_LIMIT_TICKS):
                starve_playout.tick()
            yield b"\x01\x02" * 8000

    starve_pipeline = _pipeline(
        translator=FakeTranslator({"привет": "hello"}),
        synthesizer=DelayedSynthesizer(),
        playout=starve_playout,
        metrics=starve_metrics,
        rates=rates,
    )
    starve_pipeline.handle(_final("привет"))

    assert bypass_metrics.snapshot().cost_usd == 11.0
    assert starve_metrics.snapshot().cost_usd == 11.0


def test_a_refusal_stops_pulling_more_chunks_from_the_synthesizer():
    """break, not continue: once playout refuses a chunk, the loop must stop
    pulling more out of the iterator rather than keep draining it - that is
    exactly the audio nobody will hear that this guard exists to stop paying
    for.

    Uses a plain iterator with no close() (the Synthesizer port is typed as
    a bare Iterator, which need not have one - unlike every generator-backed
    fake elsewhere in this file). With a closeable generator, closing it
    would itself end iteration regardless of break vs. continue, masking
    this exact mutation.
    """

    class SuppressingIterator:
        def __init__(self, playout):
            self._playout = playout
            self._remaining = 5
            self.pulled = 0

        def __iter__(self):
            return self

        def __next__(self):
            if self._remaining <= 0:
                raise StopIteration
            self._remaining -= 1
            self.pulled += 1
            if self.pulled == 2:
                self._playout.set_suppressed(True)
            return b"\x01\x02" * 8000

    playout = Playout(Direction.IN, FakeAudioSink())
    iterator = SuppressingIterator(playout)

    class OnceSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            return iterator

    pipeline = _pipeline(synthesizer=OnceSynthesizer(), playout=playout)
    pipeline.handle(_final(t_end=1.0))

    assert iterator.pulled == 2


def test_a_refusal_closes_the_synthesis_stream():
    """The gRPC stream must be closed explicitly on refusal, not left to
    garbage collection - it is what ends a request Google is still billing
    and holding open."""
    closed = []
    playout = Playout(Direction.IN, FakeAudioSink())

    class ClosableAfterOne:
        def __init__(self):
            self._chunks = iter([b"\x01\x02" * 8000, b"\x03\x04" * 8000])
            self._pulled = 0

        def __iter__(self):
            return self

        def __next__(self):
            chunk = next(self._chunks)
            self._pulled += 1
            if self._pulled == 1:
                playout.set_suppressed(True)
            return chunk

        def close(self):
            closed.append(True)

    class OnceSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            return ClosableAfterOne()

    pipeline = _pipeline(synthesizer=OnceSynthesizer(), playout=playout)
    pipeline.handle(_final(t_end=1.0))

    assert closed == [True]


def test_a_successful_synthesis_recovers_tts_health_after_a_prior_failure():
    """DirectionState.tts defaults to OK, so only a FAILED -> OK sequence can
    tell a real recovery from a health update that was never made at all."""
    metrics = Metrics()
    playout = Playout(Direction.IN, FakeAudioSink())

    class OnceFailingSynthesizer:
        def __init__(self):
            self.calls = 0

        def synthesize(self, text, voice, speaking_rate=1.0):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
                yield b""  # pragma: no cover - generator marker
            yield b"\x01\x02" * 8000

    pipeline = _pipeline(
        synthesizer=OnceFailingSynthesizer(), playout=playout, metrics=metrics
    )
    pipeline.handle(_final("first", t_end=1.0))
    assert metrics.snapshot().directions[Direction.IN].tts is Health.FAILED

    pipeline.handle(_final("second", t_end=2.0))
    assert metrics.snapshot().directions[Direction.IN].tts is Health.OK


def test_translation_time_does_not_leak_into_tts_ms():
    """The synthesis section's own `started` must be re-taken after
    translation, not inherited from before it - or slow translation shows up
    as synthesis latency."""
    clock = FakeClock(start=0.0)

    class TickingTranslator(FakeTranslator):
        def translate(self, text, src, tgt):
            clock.advance(0.5)
            return super().translate(text, src, tgt)

    class TickingSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            clock.advance(0.2)
            yield b"\x01\x02" * 8000

    metrics = Metrics()
    pipeline = _pipeline(
        translator=TickingTranslator({"привет": "hello"}),
        synthesizer=TickingSynthesizer(),
        metrics=metrics,
        clock=clock,
    )
    pipeline.handle(_final("привет", t_end=0.0))

    latency = metrics.snapshot().directions[Direction.IN].latency
    assert latency.mt_ms == 500.0
    assert latency.tts_ms == 200.0


def test_speaking_disarms_the_dead_air_alarm():
    """dead_air.spoke() must actually run on a clean, accepted utterance -
    it is the only thing that disarms the earcon once armed."""
    clock = FakeClock()
    watch = DeadAirWatch(clock, threshold_s=6.0)
    watch.heard_speech()
    clock.advance(7.0)
    assert watch.alarming() is True  # armed and past threshold, before speaking

    pipeline = _pipeline(clock=clock, dead_air=watch)
    pipeline.handle(_final(t_end=clock.monotonic()))

    assert watch.alarming() is False


# --- the empty-chunk skip ----------------------------------------------------
#
# `if not chunk: continue` is unreachable with the real ChirpSynthesizer -
# Chirp 3 HD never yields an empty chunk - but the Synthesizer port's type
# does not forbid one, and three different mutations of that one line survive
# every test above: never skipping it, breaking instead of skipping it, and
# billing it as if it were real audio.


def test_an_empty_chunk_is_skipped_without_stopping_the_real_chunk_behind_it():
    """An empty chunk must be skipped outright: not billed, not treated as
    the request having produced anything, and not allowed to end the loop
    before the real audio behind it ever gets a chance to arrive."""
    clock = FakeClock(start=0.0)

    class EmptyThenRealSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            yield b""            # must be skipped, not counted, not billed
            clock.advance(0.2)   # only the real chunk's wait should show up
            yield b"\x01\x02" * 8000

    metrics = Metrics()
    pipeline = _pipeline(
        synthesizer=EmptyThenRealSynthesizer(), metrics=metrics, clock=clock
    )
    pipeline.handle(_final("привет", t_end=0.0))

    latency = metrics.snapshot().directions[Direction.IN].latency
    assert latency.tts_ms == 200.0


def test_a_synthesis_that_yields_only_empty_chunks_is_not_billed():
    """Skipping an empty chunk must happen before `produced` is set for it -
    a synthesis that never yields anything real must not be billed as if it
    had."""
    from sidetap.cost import Rates

    metrics = Metrics()

    class OnlyEmptyChunksSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            yield b""
            yield b""

    pipeline = _pipeline(
        synthesizer=OnlyEmptyChunksSynthesizer(),
        metrics=metrics,
        rates=Rates(mt_per_million_chars=1_000_000.0, tts_per_million_chars=1_000_000.0),
    )
    pipeline.handle(_final("привет"))

    # Only the 6-character source text is billed, at $1/char - two empty
    # chunks are not a synthesis that produced anything.
    assert metrics.snapshot().cost_usd == 6.0


# --- other pre-existing lines this file did not defend -----------------------


def test_a_successful_utterance_updates_the_queue_depth_metric():
    """set_queue_s must actually run - it is the TUI's only view of playout
    backlog for this direction."""
    playout = Playout(Direction.IN, FakeAudioSink())
    metrics = Metrics()
    pipeline = _pipeline(playout=playout, metrics=metrics)
    pipeline.handle(_final(t_end=1.0))

    backlog = playout.backlog_s()
    assert backlog > 0.0
    assert metrics.snapshot().directions[Direction.IN].queue_s == backlog


def test_speaking_rate_is_passed_through_to_the_synthesizer():
    """The two directions' useful speaking rates are inverses (ports.py), so
    dropping this argument would make one direction silently wrong rather
    than loudly broken."""
    synthesizer = FakeSynthesizer()
    config = DirectionConfig(
        direction=Direction.IN,
        source_lang="ru-RU",
        target_lang="en-US",
        voice="en-US-Chirp3-HD-Charon",
        speaking_rate=1.3,
    )
    pipeline = _pipeline(config=config, synthesizer=synthesizer)
    pipeline.handle(_final(t_end=1.0))

    assert synthesizer.rates == [1.3]
