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

from .types import AsrResult, Unit

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
