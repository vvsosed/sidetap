import threading

from sidetap.metrics import Health, Metrics
from sidetap.types import Direction, Latency


def test_a_fresh_snapshot_has_both_directions():
    snapshot = Metrics().snapshot()
    assert set(snapshot.directions) == {Direction.IN, Direction.OUT}


def test_interim_text_is_replaced_not_appended():
    metrics = Metrics()
    metrics.set_interim(Direction.IN, "прив")
    metrics.set_interim(Direction.IN, "привет")
    assert metrics.snapshot().directions[Direction.IN].interim == "привет"


def test_a_final_clears_the_interim():
    metrics = Metrics()
    metrics.set_interim(Direction.IN, "прив")
    metrics.set_final(Direction.IN, "привет", "hello", Latency(mt_ms=50.0))
    state = metrics.snapshot().directions[Direction.IN]
    assert state.interim == ""
    assert state.final == "привет"
    assert state.translation == "hello"
    assert state.latency.mt_ms == 50.0


def test_directions_do_not_bleed_into_each_other():
    metrics = Metrics()
    metrics.set_interim(Direction.IN, "theirs")
    assert metrics.snapshot().directions[Direction.OUT].interim == ""


def test_capture_drops_are_tracked_separately_from_playout_drops():
    """Different queues, different causes, and both must be visible.

    Playout drops come from the lag cap; capture drops come from an outage
    stalling the recogniser. Conflating them would hide meetscribe's bug
    behind a counter that looks healthy.
    """
    metrics = Metrics()
    metrics.add_dropped(Direction.IN, 2)
    metrics.set_capture_dropped(Direction.IN, 400)
    state = metrics.snapshot().directions[Direction.IN]
    assert state.dropped == 2
    assert state.capture_dropped == 400


def test_capture_dropped_is_absolute_not_cumulative():
    # DroppingQueue.dropped is already a running total, so this is a set,
    # not an add - adding would square the count on every poll.
    metrics = Metrics()
    metrics.set_capture_dropped(Direction.OUT, 10)
    metrics.set_capture_dropped(Direction.OUT, 12)
    assert metrics.snapshot().directions[Direction.OUT].capture_dropped == 12


def test_dropped_counts_accumulate():
    metrics = Metrics()
    metrics.add_dropped(Direction.OUT, 2)
    metrics.add_dropped(Direction.OUT, 3)
    assert metrics.snapshot().directions[Direction.OUT].dropped == 5


def test_health_starts_ok_and_can_degrade():
    metrics = Metrics()
    assert metrics.snapshot().directions[Direction.IN].asr is Health.OK
    metrics.set_health(Direction.IN, asr=Health.FAILED)
    state = metrics.snapshot().directions[Direction.IN]
    assert state.asr is Health.FAILED
    # Untouched stages keep their value.
    assert state.mt is Health.OK


def test_the_snapshot_is_a_copy():
    """The TUI renders from a snapshot while threads keep writing.

    If snapshot() aliased live state, a render could see an utterance half
    updated - a new final beside the previous translation.
    """
    metrics = Metrics()
    metrics.set_interim(Direction.IN, "before")
    snapshot = metrics.snapshot()
    metrics.set_interim(Direction.IN, "after")
    assert snapshot.directions[Direction.IN].interim == "before"


def test_dead_air_is_reported_per_direction():
    metrics = Metrics()
    metrics.set_dead_air(Direction.OUT, True)
    assert metrics.snapshot().directions[Direction.OUT].dead_air is True
    assert metrics.snapshot().directions[Direction.IN].dead_air is False


def test_concurrent_writers_do_not_lose_counts():
    metrics = Metrics()

    def bump():
        for _ in range(200):
            metrics.add_dropped(Direction.IN, 1)

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert metrics.snapshot().directions[Direction.IN].dropped == 800


def test_the_translation_model_in_use_is_recorded():
    """Task 16's on_downgrade needs somewhere to land.

    Without this the sticky NMT downgrade is invisible: the next successful
    call sets mt=Health.OK, so the pane goes green while quality has dropped
    for the rest of the session.
    """
    metrics = Metrics()
    assert metrics.snapshot().mt_model == ""
    metrics.set_mt_model("general/translation-llm")
    assert metrics.snapshot().mt_model == "general/translation-llm"
    metrics.set_mt_model("general/nmt")
    assert metrics.snapshot().mt_model == "general/nmt"


def test_cost_accumulates_across_directions():
    metrics = Metrics()
    metrics.add_cost(0.01)
    metrics.add_cost(0.02)
    assert metrics.snapshot().cost_usd == 0.03


def test_no_audio_is_tracked_separately_from_dead_air():
    """They are different failures with different causes.

    dead_air means an utterance finished and nothing came out; no_audio means
    nothing ever went in. Collapsing them would point the user at the wrong
    half of the pipeline.
    """
    metrics = Metrics()
    metrics.set_no_audio(Direction.IN, True)
    snapshot = metrics.snapshot()
    assert snapshot.directions[Direction.IN].no_audio is True
    assert snapshot.directions[Direction.IN].dead_air is False
    assert snapshot.directions[Direction.OUT].no_audio is False
