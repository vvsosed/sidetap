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


class FinalsOnlySegmenter:
    """One Unit per final result. Interims are discarded."""

    def feed(self, result: AsrResult) -> list[Unit]:
        if not result.is_final:
            return []
        if not result.text.strip():
            return []
        return [Unit.from_result(result)]
