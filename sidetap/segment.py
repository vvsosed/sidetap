"""The seam between recognition and translation.

A Segmenter turns AsrResults into Units - the text worth translating and
speaking. FinalsOnlySegmenter waits for a complete utterance;
LocalAgreementSegmenter commits a stable prefix part-way through.

Chirp sends no interims for short utterances and one per ~5 s of sent audio
otherwise (docs/experiments/06-interim-cadence.md), so LocalAgreement-2 only
helps monologues: two hypotheses need ~11 s of speech before they can agree.
There is deliberately no length threshold - it would duplicate a bound the API
already imposes.
"""

from __future__ import annotations

import logging
import re

from .types import AsrResult, Direction, Unit

log = logging.getLogger(__name__)

# Edges only: stripping punctuation throughout would key "1.2" and "12" alike
# and commit a number the recogniser had not settled on.
_EDGE_PUNCTUATION = re.compile(r"^\W+|\W+$", re.UNICODE)


def _tokens(text: str) -> list[tuple[str, str]]:
    """Split into (surface, key) pairs. Only the key is ever compared.

    The surface keeps case and punctuation, because it is what the translator
    gets. The key is lowercased and edge-stripped, because both change between
    hypotheses of the same words ("Что"/"что", "сверхурочно."/"сверхурочно,");
    comparing surfaces finds almost no common prefix on real speech.

    Accepted gaps: "-12" keys like "12", since a leading "-" cannot be told
    from an edge dash and speech usually says "minus" anyway. A
    punctuation-only token keys to "" but keeps its position, so one present
    in only one hypothesis ends the agreement; two different ones agree, which
    at worst commits a punctuation mark.
    """
    return [
        (surface, _EDGE_PUNCTUATION.sub("", surface.lower()))
        for surface in text.split()
    ]


# What ends a clause. Interims carry punctuation, so a boundary is usually
# available within five seconds of speech.
#
# No em dash: in Russian it also stands in for a missing copula ("Москва -
# столица"), and cutting there hands the translator a subject with no
# predicate. If a dash is the only candidate, _cut commits the growth uncut,
# which is still a complete thought.
#
# Only a token's last character is tested, so "..." is covered by ".". A
# closing quote or bracket with nothing after it is missed, which costs a later
# commit rather than a wrong one.
_BOUNDARY = ",.;:!?…"


def _cut(tokens: list[tuple[str, str]]) -> int:
    """How many of these tokens to commit now.

    Up to and including the last clause boundary, so the translator never
    gets a fragment cut mid-clause. With no boundary, commit everything:
    holding back unpunctuated speech would bring back the stall this exists
    to remove.
    """
    for i in range(len(tokens) - 1, -1, -1):
        if tokens[i][0][-1:] in _BOUNDARY:
            return i + 1
    return len(tokens)


def _agreed(previous: list[tuple[str, str]], current: list[tuple[str, str]]) -> int:
    """How many leading words two hypotheses agree on, by key.

    A prefix, not a tally. A false agreement gets spoken; a missed one only
    delays.
    """
    n = 0
    for (_, previous_key), (_, current_key) in zip(previous, current):
        if previous_key != current_key:
            # Stop, do not skip: a later match is not settled text.
            break
        n += 1
    return n


# How many spoken tokens one utterance keeps for alignment. Bounded because the
# search is quadratic; far above the largest real overlap (69 words), because a
# token trimmed out can no longer be recognised as spoken and gets repeated.
_SPOKEN_LIMIT = 400


# The shortest run of already-spoken words `_overlap`'s third arm anchors on.
# That arm drops everything in front of its match, so a phrase that merely
# recurs in new speech would silently discard the new words before it.
#
# Measured on real calls: coincidental shared runs reached 6 words, while a
# genuine re-window matched 64. Too low, and new speech is dropped silently;
# too high, and a short re-window is repeated audibly. A repeat is at least
# heard, so err high.
MIN_ANCHOR = 8


def _overlap(
    spoken: list[tuple[str, str]], candidate: list[tuple[str, str]]
) -> tuple[int, int]:
    """How many of the candidate's leading tokens not to emit, and why.

    Returns `(k, anchor)`; the caller emits `candidate[k:]`. `anchor` is 0
    unless the third arm decided k, and is then the length of the spoken run
    it matched, with the `k - anchor` tokens before it dropped. `_align` needs
    it because k alone cannot tell skipped heard words from dropped new ones.

    Alignment is by content, not position, because Chirp re-windows its
    hypothesis mid-utterance and a positional slice re-emits whatever the
    shift exposes. First match wins:

    1. The largest k for which the last k keys of `spoken` equal the first k
       of `candidate` - the candidate continuing where speech stopped. Largest,
       so of several possible anchors the last wins: a synthesised voice
       cannot take a repeated word back, and "and then, and then" loses
       nothing either way.
    2. The candidate lies wholly inside `spoken` - a hypothesis that shrank.
       Nothing is new, so nothing is emitted; the cost is that a phrase
       repeated verbatim is interpreted once.
    3. The longest tail of `spoken`, at least MIN_ANCHOR tokens, found
       anywhere in the candidate, with k just past it (`_anchor`). This is a
       re-window reaching back before the utterance's first commit. It is the
       only arm that can drop unheard words, hence the floor.
    """
    spoken_keys = [key for _, key in spoken]
    candidate_keys = [key for _, key in candidate]
    for k in range(min(len(spoken_keys), len(candidate_keys)), 0, -1):
        if spoken_keys[len(spoken_keys) - k :] == candidate_keys[:k]:
            return k, 0
    width = len(candidate_keys)
    if width and any(
        spoken_keys[i : i + width] == candidate_keys
        for i in range(len(spoken_keys) - width + 1)
    ):
        return width, 0
    return _anchor(spoken_keys, candidate_keys)


def _anchor(spoken_keys: list[str], candidate_keys: list[str]) -> tuple[int, int]:
    """`_overlap`'s third arm, as `(k, anchor)`, or `(0, 0)` if it finds nothing.

    Longest run first, so a genuine re-window matches over its whole length
    rather than on a shorter tail that recurs later in new speech. Never
    matches at index 0, which the first arm already tried, so every match
    drops at least one token.
    """
    end, length = 0, 0
    # Right to left, replacing only on a strictly longer run, so of two equal
    # runs the later wins: anchoring on the earlier would re-emit the later.
    for stop in range(len(candidate_keys), 0, -1):
        # Walk back from `stop` while the candidate matches the end of what
        # was spoken; linear for ordinary text, which mismatches at once.
        run = 0
        reach = min(len(spoken_keys), stop)
        while run < reach and spoken_keys[-1 - run] == candidate_keys[stop - 1 - run]:
            run += 1
        if run > length:
            end, length = stop, run
    if length < MIN_ANCHOR:
        return 0, 0
    return end, length


class FinalsOnlySegmenter:
    """One Unit per final result. Interims are discarded.

    **One instance per direction, never shared.** This one is stateless, but
    LocalAgreementSegmenter is not, and the two must stay interchangeable.
    """

    def feed(self, result: AsrResult) -> list[Unit]:
        if not result.is_final:
            return []
        if not result.text.strip():
            return []
        return [Unit.from_result(result)]


class LocalAgreementSegmenter:
    """Commit the longest word prefix two consecutive interims agree on.

    **One instance per direction, never shared.** It holds per-utterance
    state, and a shared instance would interleave two conversations.

    Two agreements, not configurable: in Experiment 6 the final inserted a
    word thirteen from the end of text an interim had already shown. One
    agreement would have spoken text the final then contradicted.

    `_spoken` alone prevents repeats. Every candidate is aligned by content
    against what this utterance has already emitted, and only the part past
    that alignment is emitted. A positional count would fall out of step
    whenever Chirp re-windows its hypothesis.
    """

    def __init__(self):
        self._previous: list[tuple[str, str]] = []
        self._previous_t_end = 0.0
        # Everything this utterance has emitted, in order - tokens, not a
        # count, because a re-windowed hypothesis moves every position.
        self._spoken: list[tuple[str, str]] = []
        self._span_start = 0.0

    def feed(self, result: AsrResult) -> list[Unit]:
        if result.is_final:
            return self._finalise(result)
        return self._interim(result)

    def _align(
        self, candidate: list[tuple[str, str]], direction: Direction, stage: str
    ) -> int:
        """`_overlap` against what was spoken, logged in one place for both
        the interim and the final path."""
        spoken = self._spoken
        k, anchor = _overlap(spoken, candidate)
        if not spoken or not candidate:
            # The start of an utterance, or an empty final: not a revision.
            return k
        if anchor:
            # Tested first: k here counts dropped words too and can exceed
            # len(spoken), which the branches below would misreport.
            #
            # Warning, because this is the only outcome that discards
            # recognised words - right for a re-window, silent loss for a false
            # anchor. The two numbers tell them apart afterwards: a run far
            # above MIN_ANCHOR with few words dropped is a re-window, one near
            # it with a clause dropped needs checking. A re-windowed hypothesis
            # can log this once per interim until its final.
            log.warning(
                "%s: %s dropped %d leading word(s) in front of a run of %d "
                "already spoken (a re-windowed or revised hypothesis); "
                "emitting only what follows the run",
                direction.value,
                stage,
                k - anchor,
                anchor,
            )
        elif k == 0:
            # Warning, because this is the only outcome that can repeat speech.
            # A stream restart lands here harmlessly, but so does a wholesale
            # revision or a re-window shorter than MIN_ANCHOR, and nothing in
            # the AsrResult tells them apart.
            log.warning(
                "%s: %s shares no words with the %d already spoken (stream "
                "restart, or a re-windowed hypothesis); emitting it whole",
                direction.value,
                stage,
                len(spoken),
            )
        elif k < len(spoken):
            # Trimmed to the seam, so nothing is repeated or lost; debug only.
            log.debug(
                "%s: %s revised text already spoken; %d of %d spoken word(s) "
                "still align, emitting only what is past them",
                direction.value,
                stage,
                k,
                len(spoken),
            )
        return k

    def _remember(self, tokens: list[tuple[str, str]]) -> None:
        # Trim from the front: alignment matches against the end.
        self._spoken = (self._spoken + tokens)[-_SPOKEN_LIMIT:]

    def _interim(self, result: AsrResult) -> list[Unit]:
        previous, previous_t_end = self._previous, self._previous_t_end
        current = _tokens(result.text)
        self._previous, self._previous_t_end = current, result.t_end

        agreed = _agreed(previous, current)
        if not agreed:
            return []

        # The whole agreed prefix: alignment by content decides what is new.
        candidate = current[:agreed]
        spoken_before = len(self._spoken)
        aligned = self._align(candidate, result.direction, "interim")
        growth = candidate[aligned:]
        if not growth:
            # Agreement on nothing new, common while a re-windowed hypothesis
            # catches back up.
            return []
        growth = growth[: _cut(growth)]
        if not growth:
            # Unreachable while _cut never returns 0 for a non-empty list.
            # Kept because nothing downstream reliably rejects an empty Unit.
            return []

        if spoken_before and not aligned:
            # A zero alignment continues nothing spoken, so the span watermark
            # no longer applies. Chaining across a stream restart would date
            # the new clause before the break and misorder the transcript,
            # which sorts on t_start. An anchored (non-zero) alignment still
            # continues the spoken run and chains normally.
            self._span_start = previous_t_end

        # t_end is the older hypothesis's position - the point through which
        # this text is confirmed. The newer one would understate latency in
        # the transcript.
        unit = self._emit(
            result.direction,
            growth,
            self._span_start,
            previous_t_end,
            continues=True,
        )
        self._remember(growth)
        return [unit]

    def _emit(
        self,
        direction: Direction,
        tokens: list[tuple[str, str]],
        t_start: float,
        t_end: float,
        *,
        continues: bool,
    ) -> Unit:
        """Build the Unit and advance the span watermark past it.

        The caller chooses `t_start`. A clause committed mid-utterance chains
        from the previous clause's t_end. An utterance that committed nothing
        early carries `result.t_start` (equal to t_end, see asr.py), matching
        FinalsOnlySegmenter, since the transcript sorts on t_start.

        Not a pure constructor: never call it speculatively or discard the
        result, or the next unit's span will overlap this one.
        """
        unit = Unit(
            direction=direction,
            text=" ".join(surface for surface, _ in tokens),
            t_start=t_start,
            t_end=t_end,
            continues=continues,
        )
        self._span_start = t_end
        return unit

    def _finalise(self, result: AsrResult) -> list[Unit]:
        tokens = _tokens(result.text)
        aligned = self._align(tokens, result.direction, "final")

        self._previous = []
        self._previous_t_end = 0.0
        # Per-utterance: carried over, the next utterance's first word could
        # anchor on this one's last ("всё.") and be eaten.
        self._spoken = []

        # A final is never cut: there is nothing left to wait for.
        remainder = tokens[aligned:]

        if not remainder:
            # Nothing new. Still advance the span, so the next utterance does
            # not claim to start back here.
            self._span_start = result.t_end
            return []

        # Chain from the span only if this continues spoken text (any non-zero
        # alignment, anchored included). A zero alignment - nothing committed
        # early, or a stream restart - must match FinalsOnlySegmenter's span.
        t_start = self._span_start if aligned else result.t_start
        return [
            self._emit(
                result.direction, remainder, t_start, result.t_end, continues=False
            )
        ]
