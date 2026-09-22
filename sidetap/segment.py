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

from .types import AsrResult, Unit


def _tokens(text: str) -> list[tuple[str, str]]:
    """Split into (surface, key) pairs. Only the key is ever compared.

    The surface keeps case and punctuation, because it is what reaches the
    translator. The key drops both, because Experiment 6 observed both
    changing between two hypotheses whose words were otherwise identical -
    "Что" to "что", "сверхурочно." to "сверхурочно,". Comparing surfaces finds
    a common prefix of zero characters on the real capture.

    A token whose surface is punctuation alone keeps an empty key. It still
    occupies a position, so a dash present in one hypothesis and absent from
    the next ends the agreement there rather than silently shifting it - which
    errs toward committing less.
    """
    return [
        (surface, "".join(ch for ch in surface.lower() if ch.isalnum()))
        for surface in text.split()
    ]


def _agreed(previous: list[tuple[str, str]], current: list[tuple[str, str]]) -> int:
    """How many leading words two hypotheses agree on, by key."""
    n = 0
    for (_, previous_key), (_, current_key) in zip(previous, current):
        if previous_key != current_key:
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
