"""The seam between recognition and translation.

A Segmenter turns AsrResults into Units - the things worth paying to translate
and speak. v1 ships FinalsOnlySegmenter, which waits for a complete utterance.

The alternative this boundary exists for is LocalAgreement-2: commit the
longest common prefix of two consecutive interim hypotheses, cut at clause
boundaries, and start speaking while the other party is still talking. It buys
roughly 0.5-1 s at the cost of more synthesis calls on shorter strings and
awkward output where EN/RU word order diverges mid-clause. The spec's decision
was to measure first - which is why every Unit carries a span the transcript
turns into a latency figure.
"""

from __future__ import annotations

import re

from .types import AsrResult, Direction, Unit

# Only the EDGES. Stripping punctuation throughout would collapse "1.2" and
# "12" to the same key, and two numbers that differ by a factor of ten would
# compare as agreed - committed, translated and spoken, with no way to take a
# spoken number back. Both revisions Experiment 6 actually observed were at a
# token's edge ("сверхурочно." to "сверхурочно,"), so the edges are all that
# needs to go.
_EDGE_PUNCTUATION = re.compile(r"^\W+|\W+$", re.UNICODE)


def _tokens(text: str) -> list[tuple[str, str]]:
    """Split into (surface, key) pairs. Only the key is ever compared.

    The surface keeps case and punctuation, because it is what reaches the
    translator. The key drops case entirely and punctuation only at the edges,
    because Experiment 6 observed both changing between two hypotheses whose
    words were otherwise identical - "Что" to "что", "сверхурочно." to
    "сверхурочно,". Comparing surfaces finds a common prefix of zero
    characters on the real capture. Interior punctuation stays, because it can
    be load-bearing - "1.2" and "12" are different numbers, and collapsing
    them to the same key would commit, translate and speak one before the
    recogniser had settled on which.

    A sign is the one case this does not catch: "-12" keys to "12", so a
    negative and a positive still agree. Left alone deliberately rather than
    special-cased, because a leading "-" is indistinguishable here from an
    ordinary edge dash, and conversational speech in either language tends to
    render a negative as a word ("минус пять", "minus five") rather than as a
    signed digit.

    A token whose surface is punctuation alone keeps an empty key. It still
    occupies a position, so a dash present in one hypothesis and absent from
    the next ends the agreement there rather than silently shifting it - which
    errs toward committing less. Two *different* punctuation-only tokens at
    the same position - "-" and "..." both key to "" - compare as agreeing,
    but the impact is bounded: what gets committed on a false agreement there
    is a piece of punctuation, not a word.
    """
    return [
        (surface, _EDGE_PUNCTUATION.sub("", surface.lower()))
        for surface in text.split()
    ]


def _agreed(previous: list[tuple[str, str]], current: list[tuple[str, str]]) -> int:
    """How many leading words two hypotheses agree on, by key.

    A prefix, not a tally: it stops at the first mismatch rather than counting
    every position that happens to match. Everything counted here gets
    committed, translated and spoken, so a false "agreed" is the direction
    that costs something - a missed one only delays.
    """
    n = 0
    for (_, previous_key), (_, current_key) in zip(previous, current):
        if previous_key != current_key:
            # Stop, do not skip: a later word that matches again must not
            # count, or words the recogniser never settled on get spoken.
            break
        n += 1
    return n


class FinalsOnlySegmenter:
    """One Unit per final result. Interims are discarded.

    **One instance per direction, never shared.** This implementation is
    stateless, so sharing would work today - but LocalAgreement-2 holds the
    previous hypothesis and the committed prefix as instance state, and one
    instance fed by both directions would interleave two conversations and
    emit nonsense. DirectionPipeline constructs one per direction; do not
    "hoist the constant out of the loop".
    """

    def feed(self, result: AsrResult) -> list[Unit]:
        if not result.is_final:
            return []
        if not result.text.strip():
            return []
        return [Unit.from_result(result)]


class LocalAgreementSegmenter:
    """Commit the longest word prefix two consecutive interims agree on.

    **One instance per direction, never shared.** Unlike FinalsOnlySegmenter
    this really does hold state, and one instance fed by both directions would
    interleave two conversations and commit a prefix of neither.

    Two agreements, not configurable, and the count is measured rather than
    taken from the name: the final in Experiment 6 inserted a word thirteen
    from the end of text that had already appeared in one interim. Two
    agreements leave that tail uncommitted; one would have spoken text the
    final then contradicted, and a synthesised voice cannot take a word back.
    """

    def __init__(self):
        self._previous: list[tuple[str, str]] = []
        self._previous_t_end = 0.0
        # The tokens already emitted for the utterance in progress. Kept as
        # tokens rather than a count so a final can be checked against them.
        self._committed: list[tuple[str, str]] = []
        self._span_start = 0.0

    def feed(self, result: AsrResult) -> list[Unit]:
        if result.is_final:
            return self._finalise(result)
        return self._interim(result)

    def _interim(self, result: AsrResult) -> list[Unit]:
        previous, previous_t_end = self._previous, self._previous_t_end
        current = _tokens(result.text)
        self._previous, self._previous_t_end = current, result.t_end

        agreed = _agreed(previous, current)
        if agreed <= len(self._committed):
            return []
        growth = current[len(self._committed) : agreed]

        # t_end is the OLDER hypothesis's audio position, because that is the
        # point through which this text is confirmed - not where the speaker
        # has since got to. The transcript's latency column is the spec's
        # stated evidence for this whole change, so overstating freshness here
        # would corrupt the one number it is judged by.
        unit = self._emit(result.direction, growth, previous_t_end, continues=True)
        self._committed = self._committed + growth
        return [unit]

    def _emit(
        self,
        direction: Direction,
        tokens: list[tuple[str, str]],
        t_end: float,
        *,
        continues: bool,
    ) -> Unit:
        """Build the Unit and advance the span watermark past it.

        Not a pure constructor: the caller must not call this speculatively or
        discard the result, or the next unit will claim to start where this one
        did and the two spans will overlap.
        """
        unit = Unit(
            direction=direction,
            text=" ".join(surface for surface, _ in tokens),
            t_start=self._span_start,
            t_end=t_end,
            continues=continues,
        )
        self._span_start = t_end
        return unit

    def _finalise(self, result: AsrResult) -> list[Unit]:
        return []
