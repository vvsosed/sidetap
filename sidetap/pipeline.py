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
    # Per direction: the two translate opposite ways, so their useful rates
    # are inverses. See the Synthesizer port.
    speaking_rate: float = 1.0


class DeadAirWatch:
    """Fires when you have spoken and nothing has reached the other party.

    Deliberately narrow: it watches the gap between a finished OUT utterance
    and audio actually being queued for the virtual mic, catching translation,
    synthesis and sink failures. Dead recognition is already surfaced by
    RecognitionWorker's health, which keeps this out of the audio path.

    Not wired to IN: a stalled IN pipeline is not silent. The duck opens and
    you hear the remote party untranslated, a faster signal than any alarm.
    """

    def __init__(self, clock: Clock, threshold_s: float = DEAD_AIR_S):
        self._clock = clock
        self._threshold_s = threshold_s
        self._pending_since: float | None = None

    def heard_speech(self) -> None:
        # Only the first unanswered result starts the timer; resetting it on
        # each would let a steady stream of failures never alarm.
        if self._pending_since is None:
            self._pending_since = self._clock.monotonic()

    def spoke(self) -> None:
        self._pending_since = None

    def alarming(self) -> bool:
        # Read once: the health poll calls this while the pipeline thread may
        # be clearing it.
        pending_since = self._pending_since
        if pending_since is None:
            return False
        return (self._clock.monotonic() - pending_since) > self._threshold_s


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

    def check_dead_air(self) -> None:
        """Publish the dead-air state. Also called by Session's health poll,
        because consume() cannot while a stage is hung inside handle()."""
        if self._dead_air is not None:
            alarming = self._dead_air.alarming()
            self._metrics.set_dead_air(self._config.direction, alarming)

    def handle(self, result: AsrResult) -> None:
        direction = self._config.direction

        # Interims feed the TUI's "they are talking" line. Finals clear it via
        # Metrics.set_final once translation succeeds.
        if not result.is_final:
            self._metrics.set_interim(direction, result.text)

        # EVERY result goes to the segmenter, interims included: the finality
        # filter belongs to the segmenter, and LocalAgreement-2 works by
        # comparing consecutive interims.
        #
        # One reading per result, shared by all its units; inside the loop it
        # would fold _speak's own time into later units' asr_ms.
        arrived = self._clock.monotonic() - self._session_t0

        for unit in self._segmenter.feed(result):
            if self._dead_air is not None:
                self._dead_air.heard_speech()
            # Per unit, not per result: a committed prefix is confirmed only
            # through the older hypothesis's position, so result.t_end would
            # understate its age by seconds. Identical under
            # FinalsOnlySegmenter, where unit.t_end is result.t_end.
            #
            # Rounded to 0.1 ms to hide float noise (10.2 - 10.0 != 0.2).
            asr_ms = round(max(0.0, (arrived - unit.t_end) * 1000), 1)
            self._speak(unit, asr_ms)

        if result.is_final:
            # End the duck hold with the sentence, even when the final
            # committed nothing new; otherwise it would sit until the
            # starvation bound.
            self._playout.expect_continuation(False)

    def _speak(self, unit: Unit, asr_ms: float) -> None:
        direction = self._config.direction

        started = self._clock.monotonic()
        try:
            target_text = self._translator.translate(
                unit.text, self._config.source_lang, self._config.target_lang
            )
            self._metrics.set_health(direction, mt=Health.OK)
        except Exception as exc:
            # Survivable: the direction keeps running and a later success
            # clears the health flag. Failed calls are not billed.
            log.error("translation failed (%s): %s", direction.value, exc)
            self._metrics.set_health(direction, mt=Health.FAILED)
            return
        mt_ms = round((self._clock.monotonic() - started) * 1000, 1)
        self._metrics.add_cost(self._rates.translation_usd(len(unit.text)))

        if not target_text.strip():
            return

        started = self._clock.monotonic()
        handle = self._playout.begin(unit, target_text)
        first_ms: float | None = None  # time to the first chunk PLAYOUT ACCEPTED
        produced = False  # the synthesizer yielded at least one chunk: billed
        truncated = False
        refused = False
        try:
            # Inside the try: an Iterator-typed synthesizer may raise at call
            # time, and every raise must reach finally, or the opened
            # utterance stays open and blocks the queue head.
            chunks = self._synthesizer.synthesize(
                target_text, self._config.voice, self._config.speaking_rate
            )
            for chunk in chunks:
                if not chunk:
                    continue
                produced = True
                if not self._playout.append(handle, chunk):
                    # Playout will take no more: it was flushed (bypass, mute
                    # or the drop-backlog hotkey) or given up at the
                    # starvation bound. Stop paying for audio nobody will hear
                    # and end the gRPC stream. close() is optional on an
                    # Iterator, hence the check.
                    closer = getattr(chunks, "close", None)
                    if closer is not None:
                        closer()
                    refused = True
                    break
                if first_ms is None:
                    # Only once playout has accepted a chunk: a refused chunk
                    # was never heard and must not count as latency, a
                    # transcript row or dead_air.spoke().
                    first_ms = round((self._clock.monotonic() - started) * 1000, 1)
        except Exception as exc:
            log.error("synthesis failed (%s): %s", direction.value, exc)
            self._metrics.set_health(direction, tts=Health.FAILED)
            truncated = True
        else:
            if not refused:
                # Only a generator that ran to completion shows TTS is
                # healthy; a refusal after a stall or flush is no evidence of
                # health.
                self._metrics.set_health(direction, tts=Health.OK)
        finally:
            # Pairs with begin() on every path. An utterance left open blocks
            # the queue head and disables the lag cap until the starvation
            # bound closes it.
            self._playout.finish(handle, truncated=truncated)

        # Read back from the handle: playout sets truncated itself when it
        # abandons a stalled utterance, which the producer never saw.
        truncated = handle.truncated

        if produced:
            # Billed once any audio is produced: Chirp 3 HD takes the whole
            # input before the first chunk, so the request is billed whatever
            # happens afterwards.
            self._metrics.add_cost(self._rates.synthesis_usd(len(target_text)))

        if handle.dropped:
            # Flushed (bypass, mute or the drop-backlog hotkey). Record it as
            # dropped: losing the row would lose the source line too.
            if self._on_record is not None:
                self._on_record(
                    Record(unit=unit, target_text=target_text, dropped=True)
                )
            return

        if produced and handle.truncated and first_ms is None:
            # Playout gave up at the starvation bound before accepting
            # anything, so nothing was heard - but it was said and billed,
            # so keep the row, as on the dropped path above.
            #
            # No latency: render_markdown tells this case apart from a
            # cut-short utterance by `latency.tts_ms == 0.0`.
            #
            # `handle.truncated` is implied by the other two conditions but
            # spelled out in case a future playout path closes without it.
            # `first_ms is None` excludes a synthesis that failed after audio
            # was accepted; that gets the full record below, with latency.
            if self._on_record is not None:
                self._on_record(
                    Record(unit=unit, target_text=target_text, truncated=True)
                )
            return

        if first_ms is None:
            # Nothing playout accepted, so nothing was heard: no latency, no
            # record, and dead-air stays armed.
            return

        if unit.continues:
            # Armed only once playout has accepted audio, so a direction
            # whose translator is down cannot keep re-arming the hold.
            #
            # Only ever armed here; handle() clears it on a final. A segmenter
            # emitting a non-continuing unit from an interim would leave the
            # hold standing.
            self._playout.expect_continuation(True)

        tts_total_ms = round((self._clock.monotonic() - started) * 1000, 1)

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

        Stopping as soon as `stop` flips would drop the trailing finals
        RecognitionWorker emits on its way out after Ctrl-C.
        """
        while True:
            try:
                result = results_q.get(timeout=0.25)
            except queue_module.Empty:
                if stop.is_set():
                    return
                self.check_dead_air()
                continue
            try:
                self.handle(result)
            except Exception:
                # A malformed result must not take the direction down for the
                # rest of the call.
                log.exception(
                    "pipeline error (%s)", self._config.direction.value
                )
