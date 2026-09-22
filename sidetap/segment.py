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


# How many spoken tokens one utterance keeps for the alignment below. The
# search is quadratic in this number, so it cannot be unbounded on a stream
# that never finalises - but it must stay comfortably above any overlap that
# can really occur, because a token trimmed out of the window can no longer be
# recognised as already spoken, and the next hypothesis that re-covers it gets
# it spoken a second time. That is the regression this whole rule exists to
# stop. The largest overlap ever measured on a real call was 69 words; 400 is
# nearly six times that and more words than any single Chirp utterance
# observed (the 34.8 s monologue of Experiment 6 ran to about 100), so the
# window can only bite on a stream that has gone minutes without a final.
_SPOKEN_LIMIT = 400


def _overlap(spoken: list[tuple[str, str]], candidate: list[tuple[str, str]]) -> int:
    """How many of the candidate's leading tokens the listener already heard.

    The largest k for which the LAST k keys of `spoken` equal the FIRST k keys
    of `candidate` - the candidate picking up where speech stopped. Content,
    not position: Chirp re-windows its hypothesis mid-utterance, dropping
    words off the front of what it reports, and any rule that slices by a
    count re-emits whatever that shift exposes. On the first real call that
    cost roughly 15% of everything the listener heard, in bursts of 50 to 110
    words each.

    Maximal, so of several possible anchors the LAST one wins. That is the
    direction to fail in: too large a k drops words that were new, too small a
    one speaks words already spoken, and a synthesised voice cannot take a
    word back. "and then, and then" anchors on the second "and then" and loses
    nothing, because the first is inside the overlap either way.

    The trailing containment check covers the other shape the real call
    produced: a hypothesis that SHRANK, so the candidate is a run from the
    middle of what was already spoken rather than a continuation of its end.
    Nothing in it is new, so nothing may be emitted - without this the
    agreement between two early-diverging interims is spoken again in full,
    which is the same 50-word repeat by a different door. Its one cost is a
    speaker who repeats a whole phrase verbatim and has it interpreted once;
    that is cheap next to what it prevents, and it can only ever suppress
    words the listener has already heard in that same order.
    """
    spoken_keys = [key for _, key in spoken]
    candidate_keys = [key for _, key in candidate]
    for k in range(min(len(spoken_keys), len(candidate_keys)), 0, -1):
        if spoken_keys[len(spoken_keys) - k :] == candidate_keys[:k]:
            return k
    width = len(candidate_keys)
    if width and any(
        spoken_keys[i : i + width] == candidate_keys
        for i in range(len(spoken_keys) - width + 1)
    ):
        return width
    return 0


class FinalsOnlySegmenter:
    """One Unit per final result. Interims are discarded.

    **One instance per direction, never shared.** This implementation is
    stateless, so sharing would work today - but LocalAgreement-2 holds the
    previous hypothesis and the words it has already spoken as instance state,
    and one instance fed by both directions would interleave two conversations
    and emit nonsense. DirectionPipeline constructs one per direction; do not
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

    What keeps a clause from being spoken twice is `_spoken` and nothing else.
    Every candidate - an interim's growth or a final's text - is aligned by
    CONTENT against the words this utterance has already put through the
    translator, and only the part past that alignment is emitted. There is no
    second notion of "how far we got" to fall out of step with it: the first
    real call ran a count-based prefix instead, and Chirp re-windowing its
    hypothesis mid-utterance turned that into roughly 15% of the call being
    verbatim repeats of speech from up to a minute earlier.
    """

    def __init__(self):
        self._previous: list[tuple[str, str]] = []
        self._previous_t_end = 0.0
        # Everything this utterance has actually emitted, in order. Tokens
        # rather than a count, because a count can only be spent positionally
        # and a re-windowed hypothesis moves every position.
        self._spoken: list[tuple[str, str]] = []
        self._span_start = 0.0

    def feed(self, result: AsrResult) -> list[Unit]:
        if result.is_final:
            return self._finalise(result)
        return self._interim(result)

    def _align(
        self, candidate: list[tuple[str, str]], direction: Direction, stage: str
    ) -> int:
        """`_overlap` against what was spoken, plus the one report of it.

        Logged here rather than at the call sites so both paths report the
        same event in the same words - the previous version logged a revision
        on the final path only for its first three weeks, and the interim half
        of the same defect went unnoticed for exactly that long.
        """
        spoken = self._spoken
        k = _overlap(spoken, candidate)
        if not spoken or not candidate:
            # Nothing spoken yet is the ordinary start of an utterance, and an
            # empty candidate is an empty final. Neither is a revision, and a
            # signal that fires when nothing went wrong is not a signal.
            return k
        if k == 0:
            # Warning: this is the one outcome that can still put a repeat in
            # the listener's ear. A stream restart lands here and is harmless
            # (nothing of the old utterance is in this text), but so does a
            # wholesale revision, and then everything emitted below re-covers
            # ground already spoken. Nothing in the AsrResult tells the two
            # apart - timestamps stay monotonic across a rotation by
            # construction - so the log is the only evidence the ear-witness
            # in manual-smoke.md will ever have.
            log.warning(
                "%s: %s shares no words with the %d already spoken (stream "
                "restart, or a re-windowed hypothesis); emitting it whole",
                direction.value,
                stage,
                len(spoken),
            )
        elif k < len(spoken):
            # Debug: the alignment found the seam and trimmed to it, so
            # nothing is repeated and nothing is lost. It still means Chirp
            # revised or re-windowed, which the offline experiment never saw,
            # so it is worth a line - just not one that competes with the
            # case above.
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
        # Trim from the FRONT. The alignment anchors on the END of what was
        # spoken, so the oldest tokens are the only ones it can afford to
        # lose; trimming the other end would discard exactly what the next
        # hypothesis is about to be matched against.
        self._spoken = (self._spoken + tokens)[-_SPOKEN_LIMIT:]

    def _interim(self, result: AsrResult) -> list[Unit]:
        previous, previous_t_end = self._previous, self._previous_t_end
        current = _tokens(result.text)
        self._previous, self._previous_t_end = current, result.t_end

        agreed = _agreed(previous, current)
        if not agreed:
            return []

        # The whole agreed prefix, not a slice of it. What has already been
        # said is decided by content below, so there is nothing here for a
        # position to be right or wrong about.
        candidate = current[:agreed]
        spoken_before = len(self._spoken)
        aligned = self._align(candidate, result.direction, "interim")
        growth = candidate[aligned:]
        if not growth:
            # Two hypotheses agreeing on nothing the listener has not already
            # heard. Common while a re-windowed hypothesis catches back up,
            # and the reason the count-based version spoke clauses twice.
            return []
        growth = growth[: _cut(growth)]
        if not growth:
            # Unreachable today: `growth` is non-empty here and _cut never
            # returns 0 for a non-empty list. Kept because nothing downstream
            # can be counted on to catch an empty Unit instead. _speak
            # (pipeline.py) calls translate() before checking its result, and
            # only GoogleTranslator's own internal empty-string check
            # (translate.py) stops it there with no network call; the
            # Translator Protocol makes no such promise, and FakeTranslator
            # (tests/conftest.py) returns a non-empty marker for "" - which
            # would carry a sourceless Unit past _speak's
            # `if not target_text.strip()` bail into a synthesized, recorded
            # transcript row with nothing behind it. The invariant that rules
            # this branch out lives in _cut, a different function from this
            # one, so a change there could quietly make it reachable.
            return []

        if spoken_before and not aligned:
            # Nothing of this candidate continues what was spoken (the
            # alignment came back zero), so the span watermark - which
            # describes the end of that spoken text - does not describe this
            # either. A stream restart is the case that matters: chaining
            # across it dates the new utterance's first clause to before the
            # break, and render_markdown sorts on t_start, so the bilingual
            # transcript interleaves it among rows from minutes earlier.
            # Anchoring on this unit's own t_end gives it the "one timestamp,
            # no duration" shape a whole-utterance final carries.
            self._span_start = previous_t_end

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
        aligned = self._align(tokens, result.direction, "final")

        self._previous = []
        self._previous_t_end = 0.0
        # Per-utterance. Carrying it into the next utterance would let that
        # one's opening word anchor on this one's last - "всё." closing one
        # sentence and opening the next - and the alignment would then eat a
        # word nobody has heard and chain a span across the gap between them.
        self._spoken = []

        # A final is never cut: nothing is left to wait for, so nothing is
        # held back.
        remainder = tokens[aligned:]

        if not remainder:
            # Nothing further to say - the final only repeated what was
            # already spoken, or carried less than it, which _align has
            # already logged. The span still advances, so the next utterance's
            # first commit does not claim to start back here.
            self._span_start = result.t_end
            return []

        # An utterance that committed nothing early is one whole utterance,
        # and must be indistinguishable from what FinalsOnlySegmenter would
        # have produced - span included, since that is what the transcript is
        # sorted and timed by. Only a final that actually continues text
        # already spoken chains from it; a zero alignment means this text
        # continues nothing, whether because the utterance is untouched or
        # because the stream restarted under it.
        t_start = self._span_start if aligned else result.t_start
        return [
            self._emit(
                result.direction, remainder, t_start, result.t_end, continues=False
            )
        ]
