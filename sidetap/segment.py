"""The seam between recognition and translation.

A Segmenter turns AsrResults into Units - the things worth paying to translate
and speak. Two ship: FinalsOnlySegmenter waits for a complete utterance,
LocalAgreementSegmenter commits a stable prefix part-way through.

What LocalAgreement-2 buys here is NOT the "roughly 0.5-1 s on a normal
sentence" the research describes, and an earlier version of this docstring
claimed. Chirp emits no interim results at all for short turn-taking
utterances (docs/experiments/06-interim-cadence.md), so there is nothing for a
prefix comparison to work on and ordinary conversation is untouched. What it
buys is the monologue: 34.8 s of continuous speech measured as one final after
29.3 s of silence, which this turns into a commit roughly every 5 s.

It is self-limiting with no constant to tune. Interims arrive once per 5 s of
sent audio, so two agreeing hypotheses need ~11 s of continuous speech; below
that both classes behave identically. Do not add a length threshold - it would
be a second, worse copy of a bound the API already imposes.
"""

from __future__ import annotations

import logging
import re

from .types import AsrResult, Direction, Unit

log = logging.getLogger(__name__)

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


# What ends a clause. Interims carry punctuation (Experiment 6 finding 7), so
# a boundary is almost always available inside five seconds of speech.
#
# The em dash is deliberately NOT here, though Russian uses it to separate
# clauses. It also stands in for the missing copula - "Москва - столица" - and
# cutting there would hand the translator a subject with no predicate, which
# is precisely the mid-clause fragment this function exists to prevent. Since
# nothing distinguishes the two uses from the text alone, the safe reading is
# to treat it as no boundary.
#
# Note what that does when the dash is the only candidate in the growth: the
# fallback below commits the whole growth UNCUT this round. Not later, not
# held back - the opposite. That is still the outcome to want, because an
# uncut span is a complete thought, where a cut at a copula would hand the
# translator a subject with no predicate.
#
# Only the token's LAST character is tested, so "..." needs no entry of its
# own - it ends in "." already. A closing quote or bracket with no punctuation
# after it ("(да)") is missed for the same reason, and costs a later commit
# rather than a wrong one.
_BOUNDARY = ",.;:!?…"


def _cut(tokens: list[tuple[str, str]]) -> int:
    """How many of these tokens to commit now.

    Everything up to and including the last clause boundary, so a fragment
    does not reach the translator mid-clause - the word-order cost the v1 spec
    named as LocalAgreement-2's main downside.

    With no boundary anywhere, commit the lot. Holding it back would reproduce
    the stall this exists to remove: five seconds of speech with no punctuation
    is precisely where waiting hurts. One rule, no constant, and it fails
    toward speaking.
    """
    for i in range(len(tokens) - 1, -1, -1):
        if tokens[i][0][-1:] in _BOUNDARY:
            return i + 1
    return len(tokens)


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

        if _agreed(current, self._committed) < len(self._committed):
            # This hypothesis no longer begins with the text already spoken -
            # a word was inserted inside the committed prefix, or dropped from
            # it. `growth` below slices by POSITION, so every later word has
            # shifted and the slice re-emits something the listener already
            # heard: after committing "a b c d,", the pair "a b X c d, e f" /
            # "a b X c d, e f g." grows into "d, e f" and speaks "d," twice.
            #
            # Logged, not corrected. Experiment 6 measured interims as purely
            # additive, so on a real capture this does not fire at all;
            # correcting it would mean re-matching by content, which trades a
            # shift nobody has observed for a repeat, and a synthesised voice
            # cannot take a word back. The mirror of _finalise's revision log
            # below - without it, manual-smoke.md's "no word repeated" check
            # has a symptom and no evidence, and the only record of the cause
            # is audio nobody kept.
            #
            # It also fires on a stale commit that survived a stream restart
            # (see _finalise), where the interim slice is wrong for a
            # different reason. That case is only detected at the final today.
            log.debug(
                "%s: interim revised committed text; the next commit may repeat a word",
                result.direction.value,
            )

        agreed = _agreed(previous, current)
        if agreed <= len(self._committed):
            return []
        growth = current[len(self._committed) : agreed]
        growth = growth[: _cut(growth)]
        if not growth:
            # Unreachable today: the agreement check above guarantees at
            # least one token, and _cut never returns 0 for a non-empty
            # list. Kept because nothing downstream can be counted on to
            # catch an empty Unit instead. _speak (pipeline.py) calls
            # translate() before checking its result, and only
            # GoogleTranslator's own internal empty-string check
            # (translate.py) stops it there with no network call; the
            # Translator Protocol makes no such promise, and FakeTranslator
            # (tests/conftest.py) returns a non-empty marker for "" - which
            # would carry a sourceless Unit past _speak's
            # `if not target_text.strip()` bail into a synthesized, recorded
            # transcript row with nothing behind it. The invariant that
            # rules this branch out lives in _cut, a different function from
            # this one, so a change there could quietly make it reachable.
            return []

        # t_end is the OLDER hypothesis's audio position, because that is the
        # point through which this text is confirmed - not where the speaker
        # has since got to. The transcript's latency column is the spec's
        # stated evidence for this whole change, so overstating freshness here
        # would corrupt the one number it is judged by.
        unit = self._emit(
            result.direction,
            growth,
            self._span_start,
            previous_t_end,
            continues=True,
        )
        self._committed = self._committed + growth
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

        `t_start` is the caller's to choose, and the choice is not cosmetic.
        A clause committed part-way through an utterance chains from the
        previous clause's t_end, so the pieces read as a sequence. An
        utterance that committed nothing early must instead carry
        `result.t_start`, which asr.py makes equal to t_end - anything else
        makes the default segmenter disagree with FinalsOnlySegmenter on every
        row of an ordinary call, and render_markdown sorts on t_start, so the
        bilingual transcript comes out in the wrong order.

        Not a pure constructor: the caller must not call this speculatively or
        discard the result, or the next unit will claim to start where this one
        did and the two spans will overlap.
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
        committed = self._committed

        self._previous = []
        self._previous_t_end = 0.0
        self._committed = []

        if committed and _agreed(tokens, committed) == 0:
            # Not one word in common with what we already spoke. The committed
            # state does not describe this utterance at all, so slicing by its
            # length would cut that many words off the front of a sentence
            # nobody has heard - silent loss of speech.
            #
            # The case this exists for is a stream restart. RecognitionWorker
            # (asr.py) rebuilds its stream every MAX_STREAM_SECONDS and after
            # any non-fatal error, and NEITHER path guarantees a final, so the
            # commit state of the interrupted utterance survives into the next
            # one with nothing to clear it. A restart is not observable from
            # the AsrResult values - timestamps stay monotonic across a
            # rotation by construction (StreamClock.rotated) - so the content
            # is the only evidence available here.
            #
            # Residual, stated rather than hidden: a restarted stream whose
            # first final happens to share its LEADING word with the stale
            # commit still gets mis-sliced, by at most the non-matching
            # remainder of that commit. The debug log below is what reports
            # it. Widening this to "discard on any disagreement" would fix
            # that at the cost of repeating a clause the listener already
            # heard on every ordinary tail revision, which is the trade
            # _finalise has always refused.
            log.warning(
                "%s: final shares nothing with the committed prefix (stream "
                "restart?); discarding %d committed word(s) and emitting the "
                "whole final",
                result.direction.value,
                len(committed),
            )
            committed = []

        remainder = tokens[len(committed) :]

        if _agreed(tokens, committed) < len(committed):
            # A final may revise text already committed - Experiment 6 saw one
            # insert a word thirteen from the end. This is only ever logged,
            # never corrected: emitting from where the revision starts would
            # repeat a clause the listener already heard, where emitting from
            # the commit point at worst drops a word they will never know was
            # missing.
            #
            # Checked BEFORE the remainder, because the worst case has no
            # remainder to emit: a final shorter than what was committed, or
            # one that diverges outright, slices to nothing and returns below.
            # Checking after would leave the loudest available signal that
            # committing early went wrong completely unlogged.
            log.debug(
                "%s: final revised committed text; emitting from the commit point",
                result.direction.value,
            )

        if not remainder:
            # Nothing further to say - either the final only repeated what was
            # already committed, or it carried less than that, which the check
            # above has already logged. The span still advances, so the next
            # utterance's first commit does not claim to start back here.
            self._span_start = result.t_end
            return []

        # An utterance that committed nothing early is one whole utterance,
        # and must be indistinguishable from what FinalsOnlySegmenter would
        # have produced - span included, since that is what the transcript is
        # sorted and timed by. Only an utterance actually spoken in pieces
        # chains its span from the previous piece.
        t_start = self._span_start if committed else result.t_start
        return [
            self._emit(
                result.direction, remainder, t_start, result.t_end, continues=False
            )
        ]
