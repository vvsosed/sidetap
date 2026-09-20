"""One direction of the interpreter, wired end to end.

Instantiated twice. IN reads the remote track and speaks into your headphones;
OUT reads the mic and speaks into the virtual mic's sink. Nothing here knows
which is which beyond its config.
"""

from __future__ import annotations

import logging
import queue as queue_module
import threading
from dataclasses import dataclass
from typing import Callable

from .cost import Rates
from .metrics import Health, Metrics
from .playout import Playout
from .ports import Clock, Segmenter, Synthesizer, Translator
from .types import (
    DEAD_AIR_S,
    AsrResult,
    Direction,
    Latency,
    Record,
    Unit,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DirectionConfig:
    direction: Direction
    source_lang: str
    target_lang: str
    voice: str
    # Per direction, because the two translate opposite ways and their useful
    # rates are inverses. See the Synthesizer port.
    speaking_rate: float = 1.0


class DeadAirWatch:
    """Fires when you have spoken and nothing has reached the other party.

    Deliberately narrow: it watches the gap between a finished OUT utterance
    and audio actually being queued for the virtual mic, which catches
    translation failure, synthesis failure and a dead sink. Recognition being
    dead is a different failure, already surfaced by Metrics.set_health from
    RecognitionWorker - keeping that out of here is what stops this reaching
    into the ported audio path.
    """

    def __init__(self, clock: Clock, threshold_s: float = DEAD_AIR_S):
        self._clock = clock
        self._threshold_s = threshold_s
        self._pending_since: float | None = None

    def heard_speech(self) -> None:
        # Only the first unanswered result starts the timer; resetting it on
        # each one would let a steady stream of failures never alarm.
        if self._pending_since is None:
            self._pending_since = self._clock.monotonic()

    def spoke(self) -> None:
        self._pending_since = None

    def alarming(self) -> bool:
        if self._pending_since is None:
            return False
        return (self._clock.monotonic() - self._pending_since) > self._threshold_s


class DirectionPipeline:
    def __init__(
        self,
        config: DirectionConfig,
        segmenter: Segmenter,
        translator: Translator,
        synthesizer: Synthesizer,
        playout: Playout,
        metrics: Metrics,
        clock: Clock,
        session_t0: float = 0.0,
        on_record: Callable[[Record], None] | None = None,
        dead_air: DeadAirWatch | None = None,
        rates: "Rates | None" = None,
    ):
        self._config = config
        self._segmenter = segmenter
        self._translator = translator
        self._synthesizer = synthesizer
        self._playout = playout
        self._metrics = metrics
        self._clock = clock
        self._session_t0 = session_t0
        self._on_record = on_record
        self._dead_air = dead_air
        self._rates = rates or Rates()

    @property
    def direction(self) -> Direction:
        return self._config.direction

    def handle(self, result: AsrResult) -> None:
        direction = self._config.direction

        # Interims feed the TUI - the user's only "they are talking right
        # now" signal. Finals get their `interim` field cleared by
        # Metrics.set_final instead, once translation succeeds.
        if not result.is_final:
            self._metrics.set_interim(direction, result.text)

        # EVERY result goes to the segmenter, interims included. The finality
        # filter lives in the segmenter, NOT here - FinalsOnlySegmenter drops
        # interims itself. This matters: LocalAgreement-2, the entire reason
        # this seam exists, works by comparing consecutive INTERIM hypotheses.
        # Returning early on interims would make the seam decorative, because
        # no future segmenter could ever see the input its algorithm needs.
        arrived = self._clock.monotonic() - self._session_t0
        # Rounded to a tenth of a ms: these are wall-clock subtractions, so
        # raw floats carry binary rounding noise (10.2 - 10.0 == 0.19999...
        # not 0.2) that a millisecond-scale metric has no business showing.
        asr_ms = round(max(0.0, (arrived - result.t_end) * 1000), 1)

        for unit in self._segmenter.feed(result):
            if self._dead_air is not None:
                self._dead_air.heard_speech()
            self._speak(unit, asr_ms)

    def _speak(self, unit: Unit, asr_ms: float) -> None:
        direction = self._config.direction

        started = self._clock.monotonic()
        try:
            target_text = self._translator.translate(
                unit.text, self._config.source_lang, self._config.target_lang
            )
            self._metrics.set_health(direction, mt=Health.OK)
        except Exception as exc:
            # Survivable: the direction keeps running, and a later success
            # clears this health flag. There is no billing call on this path
            # (or on the synthesis failure path below) - only a successful
            # call produces text/audio worth paying for.
            log.error("translation failed (%s): %s", direction.value, exc)
            self._metrics.set_health(direction, mt=Health.FAILED)
            return
        mt_ms = round((self._clock.monotonic() - started) * 1000, 1)
        self._metrics.add_cost(self._rates.translation_usd(len(unit.text)))

        if not target_text.strip():
            return

        started = self._clock.monotonic()
        handle = self._playout.begin(unit, target_text)
        chunks = self._synthesizer.synthesize(
            target_text, self._config.voice, self._config.speaking_rate
        )
        first_ms: float | None = None
        truncated = False
        refused = False
        try:
            for chunk in chunks:
                if not chunk:
                    continue
                if first_ms is None:
                    first_ms = round((self._clock.monotonic() - started) * 1000, 1)
                if not self._playout.append(handle, chunk):
                    # Playout will not take any more: either bypass flushed
                    # the queue, or playout gave the utterance up at the
                    # starvation bound. Either way, stop paying for audio
                    # nobody will hear, and end the gRPC stream rather than
                    # leave it to garbage collection. The Synthesizer port is
                    # typed as an Iterator, which need not have close(), so
                    # this is a capability check and not an assumption.
                    closer = getattr(chunks, "close", None)
                    if closer is not None:
                        closer()
                    refused = True
                    break
        except Exception as exc:
            log.error("synthesis failed (%s): %s", direction.value, exc)
            self._metrics.set_health(direction, tts=Health.FAILED)
            truncated = True
        else:
            if not refused:
                # Only a generator that ran to completion says TTS is well.
                # Reaching here after a refusal means playout gave up on this
                # utterance at the starvation bound, or bypass threw it away -
                # neither is evidence of health, and painting the TUI green
                # right after a stall cost the listener half a sentence is the
                # opposite of what that indicator is for.
                self._metrics.set_health(direction, tts=Health.OK)

        self._playout.finish(handle, truncated=truncated)

        # After finish(), the handle is the single source of truth. Playout
        # sets truncated itself when it abandons an utterance at the
        # starvation bound, and finish() ORs rather than overwrites, so this
        # picks up a cut-off the producer never saw. Reading the local
        # variable instead would silently report a sentence the listener
        # heard cut in half as a clean completion.
        truncated = handle.truncated

        if handle.dropped:
            # Bypass threw the queue away. The parties are talking to each
            # other unmediated, so a transcript row for a translation nobody
            # is listening to would misrepresent the call.
            return

        if first_ms is None:
            # Failed before producing anything, or produced nothing at all.
            # Byte-for-byte the pre-streaming behaviour: nothing queued, no
            # record, no cost.
            return

        tts_total_ms = round((self._clock.monotonic() - started) * 1000, 1)
        # Charged even when truncated: the request went out and produced
        # audio, and the listener heard it.
        self._metrics.add_cost(self._rates.synthesis_usd(len(target_text)))

        latency = Latency(
            asr_ms=asr_ms, mt_ms=mt_ms, tts_ms=first_ms, tts_total_ms=tts_total_ms
        )
        self._metrics.set_final(direction, unit.text, target_text, latency)
        self._metrics.set_queue_s(direction, self._playout.backlog_s())
        if self._dead_air is not None:
            self._dead_air.spoke()
            self._metrics.set_dead_air(direction, False)

        if self._on_record is not None:
            self._on_record(
                Record(
                    unit=unit,
                    target_text=target_text,
                    latency=latency,
                    truncated=truncated,
                )
            )

    def consume(self, results_q: queue_module.Queue, stop: threading.Event) -> None:
        """Drain `results_q` until it is empty AND `stop` is set.

        Checking `stop` before trying to get an item (rather than after) would
        drop results still sitting in the queue at the moment `stop` flips -
        exactly the trailing finals RecognitionWorker emits on its way out
        after Ctrl-C, per the comment in asr.py. So this only gives up once a
        get() has timed out with nothing waiting AND `stop` is set.
        """
        while True:
            try:
                result = results_q.get(timeout=0.25)
            except queue_module.Empty:
                if stop.is_set():
                    return
                if self._dead_air is not None and self._dead_air.alarming():
                    self._metrics.set_dead_air(self._config.direction, True)
                continue
            try:
                self.handle(result)
            except Exception:
                # A malformed result must not take the direction down for the
                # rest of the call.
                log.exception(
                    "pipeline error (%s)", self._config.direction.value
                )
