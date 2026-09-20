# Streaming Playout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start speaking an utterance when its first synthesis chunk arrives instead of when the last one does, cutting felt latency by 271 ms on a short sentence and 2109 ms on a long one.

**Architecture:** `Playout`'s queue entry becomes a growable `Utterance` that the pipeline appends to while synthesis streams. One entry is still one sentence, so the lag cap, the drop counter and the transcript all keep working unchanged. Playout gains one new state — started but starved — in which it holds the duck closed rather than letting the untranslated original through a mid-sentence gap.

**Tech Stack:** Python 3, `uv`, pytest, threading + `collections.deque`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-20-tts-streaming-playout-design.md`
**Measurements:** `docs/experiments/04-tts-streaming.md` (addendum, 2026-09-20)

---

## Background for someone with no context

`sidetap` translates a live call in both directions. For each direction a
worker thread recognises speech, translates it, synthesises the translation,
and hands the audio to a `Playout` object that writes it 20 ms at a time to a
`pw-cat` process. While that audio plays, a "duck" turns the remote party's
original audio down to zero so you do not hear both at once.

Google's text-to-speech returns audio as a stream of chunks, but
`DirectionPipeline._speak` currently does `b"".join(...)` on that stream —
it waits for the whole sentence before handing anything to `Playout`. This
plan removes that wait.

**Key domain rules you must not break** (from `CLAUDE.md`):

- The duck **fails open**. A duck stuck closed silences the person you are
  talking to and leaves them talking to nobody. A duck stuck open is merely
  audibly wrong. Every new code path must prefer open.
- `Playout.tick()` writes exactly one 20 ms chunk and must never block. Pace
  comes from the sink blocking, not from sleeps.
- All of `Playout`'s mutable state is guarded by `self._lock`. Callbacks
  (`on_spoken`, `on_dropped`) are invoked through `self._invoke`, which
  swallows exceptions so a bad callback cannot kill the playout thread.
- Tests run with no audio hardware, no network, no credentials. Never add a
  test that needs any of them.

**Commands:**

```bash
uv run pytest -q                      # whole suite, 349 tests today
uv run pytest tests/test_playout.py -q
```

---

## File Structure

| File | Change | Responsibility after this plan |
|---|---|---|
| `sidetap/playout.py` | Modify | Adds `Utterance` (growable queue entry), `begin`/`append`/`finish`, start threshold, starvation handling. Still owns the queue, the lag cap and the duck. |
| `sidetap/types.py` | Modify | `Latency` gains `tts_total_ms`; `Record` gains `truncated`. |
| `sidetap/pipeline.py` | Modify | `_speak` streams chunks into `Playout` instead of joining them. |
| `sidetap/transcript.py` | Modify | Writes `tts_total_ms` and `truncated`. |
| `sidetap/tts.py` | Modify | Docstring only — it currently tells the reader the pipeline does **not** stream. |
| `tests/test_playout.py` | Modify | New streaming, threshold, starvation and cap tests. |
| `tests/test_pipeline.py` | Modify | New streaming, latency and partial-failure tests. |
| `tests/test_transcript.py` | Modify | Truncated marker. |

`sidetap/run.py` needs **no change**: it constructs `Playout` with the same
arguments, and `on_dropped` still receives a `Translated`.

---

### Task 1: `Utterance` — a queue entry that can still be growing

**Files:**
- Modify: `sidetap/playout.py`
- Test: `tests/test_playout.py`

This task changes Playout's internals and adds the streaming API. All 21
existing tests in `tests/test_playout.py` must still pass unchanged — that
is the main safety check.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_playout.py`:

```python
def _unit(text: str = "hi", seconds: float = 1.0) -> Unit:
    return Unit(direction=Direction.IN, text=text, t_start=0.0, t_end=seconds)


def test_an_utterance_can_be_played_while_it_is_still_arriving():
    sink = FakeAudioSink()
    playout = Playout(Direction.IN, sink)
    item = playout.begin(_unit(), "hi")

    # Nothing to play yet: the utterance is queued but empty.
    assert playout.tick() is False

    # 400 ms of audio arrives, which clears the start threshold.
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.4 / 2))
    assert playout.tick() is True

    # More arrives while the first part is still playing.
    playout.append(item, b"\x03\x04" * int(TTS_BYTES_PER_S * 0.4 / 2))
    playout.finish(item)
    spoken = sum(1 for _ in range(60) if playout.tick())
    assert spoken == 39  # 800 ms total, minus the one chunk already written


def test_backlog_counts_only_bytes_that_have_arrived():
    playout = Playout(Direction.IN, FakeAudioSink())
    item = playout.begin(_unit(), "hi")
    assert playout.backlog_s() == 0.0

    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.5 / 2))
    assert playout.backlog_s() == pytest.approx(0.5)

    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.5 / 2))
    assert playout.backlog_s() == pytest.approx(1.0)


def test_on_spoken_fires_once_when_a_streamed_utterance_drains():
    spoken = []
    playout = Playout(Direction.IN, FakeAudioSink(), on_spoken=spoken.append)
    item = playout.begin(_unit(), "hi")
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.04 / 2))
    playout.finish(item)

    for _ in range(5):
        playout.tick()
    assert len(spoken) == 1
    assert spoken[0].text == "hi"
    assert spoken[0].audio_s == pytest.approx(0.04)
```

Add `import pytest` and `Unit` to the imports at the top of the file if not
already there. The existing import line is:

```python
from sidetap.types import TTS_BYTES_PER_S, Direction, Translated, Unit
```

`Unit` is already imported. Add `import pytest` as the first line.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_playout.py -q -k "still_arriving or only_bytes_that_have_arrived or streamed_utterance_drains"`
Expected: FAIL, `AttributeError: 'Playout' object has no attribute 'begin'`

- [ ] **Step 3: Add `Utterance` and rework Playout's internals**

In `sidetap/playout.py`, add to the imports:

```python
from dataclasses import dataclass, field
```

and extend the types import to include `Unit`:

```python
from .types import LAG_CAP_S, TTS_BYTES_PER_S, TTS_RATE, Direction, Translated, Unit
```

Add the constants below `SILENCE_CHUNK`:

```python
# An utterance does not start playing until it holds this much audio, or is
# closed. Measured (docs/experiments/04-tts-streaming.md): the first chunk is
# always 200 ms and the second lands ~30 ms later, so waiting for 400 ms costs
# ~30 ms and doubles the margin available to absorb a network stall.
START_BUFFER_S = 0.4

# Consecutive starved ticks before an in-progress utterance is abandoned.
# 100 ticks x 20 ms = 2 s. Synthesis delivers 4.7-7.1x faster than playback,
# so 2 s of nothing means the producer is gone, not slow. Counted in ticks
# rather than seconds so playout needs no clock and the test is deterministic.
STARVE_LIMIT_TICKS = 100
```

Add the `Utterance` class immediately above `class Playout`:

```python
@dataclass
class Utterance:
    """One sentence, possibly still being synthesised.

    Mutated only under Playout._lock, like every other piece of Playout's
    state. `Translated` stays the callback currency: by the time on_spoken or
    on_dropped fires the audio is complete, so the immutable type is still
    honest there.
    """

    unit: Unit
    text: str
    pcm: bytearray = field(default_factory=bytearray)
    closed: bool = False      # synthesis finished, or failed
    dropped: bool = False     # flushed; the producer must stop synthesising
    truncated: bool = False   # closed by a failure, not by completion

    @property
    def audio_s(self) -> float:
        return len(self.pcm) / TTS_BYTES_PER_S

    def snapshot(self) -> Translated:
        return Translated(unit=self.unit, text=self.text, pcm=bytes(self.pcm))
```

In `Playout.__init__`, replace these two lines:

```python
        self._queue: deque[Translated] = deque()
        self._current: Translated | None = None
        self._pending = b""
```

with:

```python
        self._queue: deque[Utterance] = deque()
        self._current: Utterance | None = None
        self._offset = 0
        self._starved_ticks = 0
```

Replace `submit` with the streaming API:

```python
    def begin(self, unit: Unit, text: str) -> Utterance:
        """Queue an utterance that has not been synthesised yet."""
        item = Utterance(unit=unit, text=text)
        with self._lock:
            self._queue.append(item)
            self._trim_locked()
        return item

    def append(self, item: Utterance, chunk: bytes) -> bool:
        """Add synthesised audio. False means stop synthesising: the utterance
        was flushed (bypass), and the rest of it will never be heard."""
        with self._lock:
            if item.dropped:
                return False
            item.pcm.extend(chunk)
            self._trim_locked()
            return not item.dropped

    def finish(self, item: Utterance, *, truncated: bool = False) -> None:
        """No more audio is coming."""
        with self._lock:
            item.closed = True
            item.truncated = truncated
            if truncated and not item.pcm and item is not self._current:
                # Synthesis failed before producing anything. Unqueue it
                # rather than leave an empty entry for tick() to step over.
                try:
                    self._queue.remove(item)
                except ValueError:
                    pass

    def submit(self, item: Translated) -> None:
        """A complete utterance is the degenerate streaming case."""
        handle = self.begin(item.unit, item.text)
        if item.pcm:
            self.append(handle, item.pcm)
        self.finish(handle)
```

Replace `_backlog_locked`:

```python
    def _backlog_locked(self) -> float:
        unread = 0
        if self._current is not None:
            unread = len(self._current.pcm) - self._offset
        return (unread + sum(len(i.pcm) for i in self._queue)) / TTS_BYTES_PER_S
```

In `flush`, replace the body's three state lines:

```python
            count = len(self._queue)
            self._queue.clear()
            self._current = None
            self._pending = b""
            return count
```

with:

```python
            count = len(self._queue)
            for item in self._queue:
                item.dropped = True
            self._queue.clear()
            if self._current is not None:
                self._current.dropped = True
            self._current = None
            self._offset = 0
            self._starved_ticks = 0
            return count
```

In `_trim_locked`, change the callback line from:

```python
            self._invoke(self._on_dropped, victim)
```

to:

```python
            victim.dropped = True
            self._invoke(self._on_dropped, victim.snapshot())
```

Replace the locked section of `tick()` — everything from `finished: Translated | None = None` down to the `else: chunk = None` — with:

```python
        finished: Translated | None = None
        with self._lock:
            if self._current is None and self._queue:
                self._current = self._queue.popleft()
                self._offset = 0

            chunk = None
            if self._current is not None:
                unread = len(self._current.pcm) - self._offset
                if unread > 0:
                    chunk = bytes(
                        self._current.pcm[self._offset : self._offset + CHUNK_BYTES]
                    )
                    self._offset += len(chunk)
                    if self._current.closed and self._offset >= len(self._current.pcm):
                        finished = self._current.snapshot()
                        self._current = None
                        self._offset = 0
                    if len(chunk) < CHUNK_BYTES:
                        chunk = chunk + b"\x00" * (CHUNK_BYTES - len(chunk))
                    self._starved_ticks = 0
                elif self._current.closed:
                    # Closed after its final byte had already been written, or
                    # it never produced any audio at all. Only the first case
                    # was spoken, so only it gets on_spoken.
                    if self._offset > 0:
                        finished = self._current.snapshot()
                    self._current = None
                    self._offset = 0
                    self._starved_ticks = 0
```

Leave the rest of `tick()` (from `if chunk is None:`) as it is for now, but
add the `finished` dispatch to the silence branch, because an utterance can
now retire on a tick that writes silence:

```python
        if chunk is None:
            if self._duck is not None:
                self._duck.open()
            self._sink.write(SILENCE_CHUNK)
            if finished is not None:
                self._invoke(self._on_spoken, finished)
            return False
```

Finally, `set_suppressed`'s docstring names `_pending`; change that sentence
to name `_offset` instead:

```
        The flag is set before the flush so a tick already in flight returns
        early rather than pulling a fresh item; the 20 ms chunk it may already
        have written is gone, for the reason flush() documents.
```

(no `_pending` reference remains in that paragraph — verify with
`grep -n "_pending" sidetap/playout.py`, which must print nothing.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_playout.py -q`
Expected: PASS, all three new tests included. As written here none of them
depends on the 400 ms start threshold Task 2 adds — they assert only that
audio can be consumed from an utterance that is still growing. (This stopped
being true once Task 1's code-quality review rewrote
`test_on_spoken_fires_once_when_a_streamed_utterance_drains` to cover
silence-branch retirement, which needs an utterance playing *while open*.
See Task 2's Step 4.)

Then run the whole suite: `uv run pytest -q`
Expected: PASS, same count as before plus the new tests.

- [ ] **Step 5: Commit**

```bash
git add sidetap/playout.py tests/test_playout.py
git commit -F - <<'MSG'
Let a playout queue entry grow while it is still being synthesised

Playout's entry becomes an Utterance with a bytearray the producer appends
to, and a reader offset replaces the pcm slice, so audio can be consumed
from an utterance that is still arriving. submit() keeps working as the
degenerate case where the whole utterance arrives at once.

Backlog now counts bytes that have actually arrived. That is honest rather
than estimated: chunks arrive 4.7-7.1x faster than they play, so the
understatement is a fraction of one utterance and self-corrects.

No behaviour change yet - the pipeline still joins the generator.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
```

---

### Task 2: The start threshold

**Files:**
- Modify: `sidetap/playout.py`
- Test: `tests/test_playout.py`

**Why:** starting on the very first byte leaves only whatever that byte
arrived with. Waiting for 400 ms costs ~30 ms and doubles the stall margin.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_playout.py`:

```python
def test_an_utterance_waits_for_the_start_threshold():
    playout = Playout(Direction.IN, FakeAudioSink())
    item = playout.begin(_unit(), "hi")

    # 200 ms is under the 400 ms threshold: nothing plays yet.
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.2 / 2))
    assert playout.tick() is False

    # 400 ms total clears it.
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.2 / 2))
    assert playout.tick() is True


def test_a_short_utterance_plays_as_soon_as_it_is_closed():
    playout = Playout(Direction.IN, FakeAudioSink())
    item = playout.begin(_unit(), "hi")
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.05 / 2))

    # 50 ms is far under the threshold, but the utterance is complete, so
    # waiting for more would mean waiting forever.
    assert playout.tick() is False
    playout.finish(item)
    assert playout.tick() is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_playout.py -q -k "start_threshold or as_soon_as_it_is_closed"`
Expected: FAIL — `assert True is False` on the first tick, because Playout
currently starts on any byte.

- [ ] **Step 3: Implement the threshold**

**Note:** Task 1's review extracted `tick()`'s locked body into
`_advance_locked()`, so the pull condition now lives there, not in `tick()`.
Work against the real file.

In `sidetap/playout.py`, add this method to `Playout`, just above
`_advance_locked`:

```python
    def _startable_locked(self, item: Utterance) -> bool:
        """Hold a new utterance until it can absorb a stall.

        `closed` comes first: an utterance shorter than the threshold is
        complete, so waiting for more audio would wait forever.
        """
        return item.closed or item.audio_s >= START_BUFFER_S
```

In `_advance_locked()`, change the pull condition from:

```python
            if self._current is None and self._queue:
```

to:

```python
            if (
                self._current is None
                and self._queue
                and self._startable_locked(self._queue[0])
            ):
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_playout.py -q`
Expected: PASS.

Run: `uv run pytest -q`
Expected: PASS. Existing `submit()` tests are unaffected because `submit()`
closes the utterance, and closed utterances are startable at any size.

**One test does need changing, and it is not a regression.**
`test_on_spoken_fires_once_when_a_streamed_utterance_drains` was rewritten
during Task 1's review to cover retirement from the *silence* branch, which
can only be reached by an utterance that is playing while still open. It
appends 40 ms, which no longer starts. Keep its intent and give it audio that
clears the threshold: 0.42 s (exactly 21 chunks), tick 21 times asserting
`True`, assert nothing has retired yet, then `finish()` and one more tick
that retires it from the silence branch. Say so explicitly in the commit
message rather than folding it in.

- [ ] **Step 5: Commit**

```bash
git add sidetap/playout.py tests/test_playout.py
git commit -F - <<'MSG'
Hold a new utterance until it has 400ms buffered, or is complete

Starting on the first byte leaves only that byte's worth of margin. The
second chunk lands about 30ms after the first, so waiting for 400ms costs
roughly 30ms and doubles the buffer available to absorb a network stall.

Closed is checked first, so an utterance shorter than the threshold plays
as soon as it is complete rather than waiting for audio that will never
arrive.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
```

---

### Task 3: Starvation — hold the duck closed, and abandon if it lasts

**Files:**
- Modify: `sidetap/playout.py`
- Test: `tests/test_playout.py`

**Why:** if synthesis stalls mid-sentence, opening the duck lets a burst of
the untranslated original through the gap — worse than the gap. But holding it
closed needs a bound, or a producer thread that dies leaves the remote party
silenced for the rest of the call.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_playout.py`:

```python
def test_a_starved_utterance_holds_the_duck_closed():
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    item = playout.begin(_unit(), "hi")
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.4 / 2))
    for _ in range(20):  # play all 400 ms
        playout.tick()
    assert duck.is_open is False

    # Synthesis stalls. The duck must NOT flap open mid-sentence.
    for _ in range(10):
        assert playout.tick() is False
    assert duck.is_open is False

    # More audio arrives and playback resumes where it left off.
    playout.append(item, b"\x03\x04" * int(TTS_BYTES_PER_S * 0.1 / 2))
    assert playout.tick() is True


def test_a_long_stall_abandons_the_utterance_and_reopens_the_duck():
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    spoken = []
    playout = Playout(
        Direction.IN, FakeAudioSink(), duck=duck, on_spoken=spoken.append
    )

    item = playout.begin(_unit(), "hi")
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.4 / 2))
    for _ in range(20):
        playout.tick()
    assert duck.is_open is False

    for _ in range(STARVE_LIMIT_TICKS + 1):
        playout.tick()

    assert duck.is_open is True
    assert item.truncated is True
    assert len(spoken) == 1  # what did play is still reported


def test_a_producer_that_dies_under_the_threshold_does_not_block_the_queue():
    playout = Playout(Direction.IN, FakeAudioSink())
    stalled = playout.begin(_unit(), "stalled")
    playout.append(stalled, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.1 / 2))
    playout.submit(_translated(0.1, text="behind"))

    # Under the 400 ms threshold and never closed: nothing plays, and the
    # utterance queued behind it is stuck too.
    assert playout.tick() is False

    for _ in range(STARVE_LIMIT_TICKS + 2):
        playout.tick()

    # The fragment that did arrive is spoken, and the queue moves again.
    assert stalled.truncated is True
    assert stalled.closed is True


def test_an_utterance_that_has_not_started_leaves_the_duck_open():
    volume = FakeVolumeControl()
    duck = DuckControl(volume, 42)
    playout = Playout(Direction.IN, FakeAudioSink(), duck=duck)

    # Queued and under the threshold: nothing has been heard, so the original
    # must stay audible.
    item = playout.begin(_unit(), "hi")
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 0.1 / 2))
    for _ in range(10):
        playout.tick()
    assert duck.is_open is True
```

Add `STARVE_LIMIT_TICKS` to the playout import at the top of the file:

```python
from sidetap.playout import (
    CHUNK_MS,
    STARVE_LIMIT_TICKS,
    DuckControl,
    Playout,
)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_playout.py -q -k "starved or long_stall or has_not_started"`
Expected: FAIL with `assert True is False` on `duck.is_open` in the first
test, because `tick()` currently opens the duck on every silent tick. The
`STARVE_LIMIT_TICKS` import resolves — Task 1 added the constant.

- [ ] **Step 3: Implement starvation**

**Note:** Task 1's review caused `tick()`'s locked body to be extracted into
`_advance_locked()`, so the patch targets differ from an earlier draft of this
plan. Work against the real file, not from memory. `_advance_locked` currently
returns `tuple[bytes | None, Translated | None]` and must become a 3-tuple.

Change its signature and the `starved` initialiser:

```python
    def _advance_locked(self) -> tuple[bytes | None, Translated | None, bool]:
        """Pull, read and retire under the lock.

        Returns (chunk, finished, starved). `starved` means an utterance the
        listener is already part-way through has nothing to play - the duck
        must stay closed across that gap.
        """
```

Initialise `starved = False` beside `chunk` and `finished`, and change the
single `return` to `return chunk, finished, starved`.

Task 1 narrowed the first branch to `unread >= CHUNK_BYTES or (unread > 0 and
closed)` and left a comment marking where the `else` goes, so the `else` now
catches both "nothing at all arrived" and "less than one chunk arrived on an
utterance still being synthesised". Replace that comment with:

```python
                else:
                    # Started but nothing to read: synthesis has not kept up.
                    # `offset > 0` means the listener is mid-sentence, which is
                    # what the duck follows - an utterance that has begun and
                    # then starved is still in progress.
                    starved = self._offset > 0
                    self._starved_ticks += 1
                    if self._starved_ticks >= STARVE_LIMIT_TICKS:
                        # The producer is gone, not slow. Give the utterance
                        # up rather than hold the duck closed for the rest of
                        # the call, which would silence the remote party -
                        # the one failure worse than sidetap not working.
                        self._current.closed = True
                        self._current.truncated = True
                        if self._offset > 0:
                            finished = self._current.snapshot()
                        self._current = None
                        self._offset = 0
                        self._starved_ticks = 0
                        starved = False
```

Then add the second half of the bound, as an `elif`/`else` on the outer
`if self._current is not None:`. Without this, Task 2's start threshold opens
a hole: a head utterance holding under `START_BUFFER_S` that is never closed
is never pulled into `_current`, so the counter above never sees it, and
Task 4 stops the lag cap dropping it. A producer that dies after delivering a
fragment would block that head — and every utterance queued behind it — for
the rest of the call.

```python
        elif self._queue:
            # Nothing is playable because the head is still under the start
            # threshold. The same bound applies, for the same reason: a
            # producer that died mid-fragment must not block the queue.
            # Nothing has been heard yet, so the duck stays open and this is
            # not the stuck-closed failure - it is the whole direction going
            # quiet with nothing on screen explaining why.
            self._starved_ticks += 1
            if self._starved_ticks >= STARVE_LIMIT_TICKS:
                # Close it where it stands rather than discard it: `closed`
                # makes it startable, so the next tick speaks the fragment
                # that did arrive. That is the same rule the spec applies to
                # a synthesis that fails part way through - play what
                # arrived - and it unblocks everything queued behind it.
                self._queue[0].closed = True
                self._queue[0].truncated = True
                self._starved_ticks = 0
        else:
            self._starved_ticks = 0
```

Finally, update `tick()`'s call site to unpack three values:

```python
        with self._lock:
            chunk, finished, starved = self._advance_locked()
```

Change the silence branch to respect `starved`:

```python
        if chunk is None:
            if self._duck is not None:
                if starved:
                    # Mid-sentence gap. close() is idempotent, so this is not
                    # a wpctl call per tick.
                    self._duck.close()
                else:
                    self._duck.open()
            self._sink.write(SILENCE_CHUNK)
            if finished is not None:
                self._invoke(self._on_spoken, finished)
            return False
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_playout.py -q`
Expected: PASS

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sidetap/playout.py tests/test_playout.py
git commit -F - <<'MSG'
Hold the duck closed through a mid-sentence stall, but not forever

An utterance that has started and then run dry is still in progress, so
opening the duck for the gap would let a burst of the untranslated original
through the middle of a sentence - worse than the gap itself.

That needs a bound. After 100 starved ticks (2s) the utterance is abandoned
as truncated and the duck reopens. Synthesis runs several times faster than
playback, so 2s of nothing means the producer is gone rather than slow, and
a producer that died must not leave the remote party silenced for the rest
of the call.

Counted in ticks rather than seconds: playout needs no clock port, and the
quantity measured is the one that matters, silence actually written.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
```

---

### Task 4: The lag cap never drops an utterance that is still arriving

**Files:**
- Modify: `sidetap/playout.py`
- Test: `tests/test_playout.py`

**Why:** an open utterance's duration is unknown, so trimming it cannot be
shown to help, and it is always the newest content. The loop must break on it
rather than spin forever.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_playout.py`:

```python
def test_the_cap_does_not_drop_an_utterance_that_is_still_arriving():
    dropped = []
    playout = Playout(
        Direction.IN, FakeAudioSink(), lag_cap_s=1.0, on_dropped=dropped.append
    )
    playout.submit(_translated(2.0))     # becomes _current
    playout.tick()

    item = playout.begin(_unit(), "new")
    playout.append(item, b"\x01\x02" * int(TTS_BYTES_PER_S * 3.0 / 2))

    # Well over the 1 s cap, but the only queued item is still open.
    assert playout.backlog_s() > 1.0
    assert dropped == []

    # Once closed it becomes droppable like anything else.
    playout.finish(item)
    playout.submit(_translated(0.1))
    assert len(dropped) == 1


def test_flushing_tells_the_producer_to_stop_synthesising():
    playout = Playout(Direction.IN, FakeAudioSink())
    item = playout.begin(_unit(), "hi")
    assert playout.append(item, b"\x01\x02" * 100) is True

    playout.set_suppressed(True)
    assert playout.append(item, b"\x01\x02" * 100) is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_playout.py -q -k "still_arriving or stop_synthesising"`
Expected: FAIL — the first with `assert [] == [<Translated...>]` because the
cap drops the open utterance; the second with `assert True is False` because
`flush()` does not yet mark `_current` dropped in a way `append` sees for an
item it already removed.

- [ ] **Step 3: Implement**

In `_trim_locked`, add the guard as the first statement inside the `while`
body, before `victim = self._queue.popleft()`:

```python
            if not self._queue[0].closed:
                # Still being synthesised: its duration is unknown, so
                # dropping it cannot be shown to help, and it is always the
                # newest content - popleft() takes the oldest, so an open
                # utterance is only ever reached when it is the last one
                # left. Break rather than spin on an undroppable head.
                break
```

`flush()` already marks both the queued items and `_current` as dropped
(Task 1), so `append` returns False for any of them. Verify the
`test_flushing_tells_the_producer_to_stop_synthesising` case passes; if the
item was neither in `_queue` nor `_current` at flush time, it is a fresh
`begin()` that has not been pulled yet, which **is** in `_queue`, so it is
covered.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_playout.py -q`
Expected: PASS

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sidetap/playout.py tests/test_playout.py
git commit -F - <<'MSG'
Never let the lag cap drop an utterance that is still arriving

Its duration is unknown, so trimming it cannot be shown to help, and it is
always the newest content: popleft takes the oldest, so an open utterance
is only reachable when it is the last one queued. The loop breaks on it
rather than spinning on a head it refuses to drop.

Cancellation is therefore driven by flush, not by the cap - which is the
case that matters. Entering bypass should stop a synthesis in flight, not
merely discard its output.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
```

---

### Task 5: Carry full synthesis time and truncation to the transcript

**Files:**
- Modify: `sidetap/types.py`
- Modify: `sidetap/transcript.py`
- Test: `tests/test_transcript.py`, `tests/test_types.py`

**Why:** `tts_ms` is about to change meaning to time-to-first-chunk. The full
synthesis wall time is still the throughput and cost signal, so it needs
somewhere to go that is **not** part of `total_ms`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_types.py`:

```python
def test_full_synthesis_time_is_recorded_but_not_counted_as_felt_latency():
    latency = Latency(asr_ms=100.0, mt_ms=50.0, tts_ms=200.0, tts_total_ms=2339.0)
    # total_ms is what the listener waited, not what synthesis cost.
    assert latency.total_ms == 350.0
```

Append to `tests/test_transcript.py`:

```python
def test_a_truncated_record_is_marked_in_the_markdown():
    unit = Unit(direction=Direction.IN, text="hello", t_start=1.0, t_end=1.0)
    record = Record(unit=unit, target_text="privet", truncated=True)
    text = render_markdown("s", [record])
    assert "_(cut short: synthesis failed)_" in text


def test_the_jsonl_row_carries_truncation_and_full_synthesis_time():
    unit = Unit(direction=Direction.IN, text="hello", t_start=1.0, t_end=1.0)
    record = Record(
        unit=unit,
        target_text="privet",
        latency=Latency(tts_ms=200.0, tts_total_ms=2339.0),
        truncated=True,
    )
    row = record_to_dict(record)
    assert row["truncated"] is True
    assert row["latency"]["tts_total_ms"] == 2339.0
```

Make sure `tests/test_transcript.py` imports `record_to_dict` and
`render_markdown` from `sidetap.transcript`, and `Latency`, `Record`, `Unit`,
`Direction` from `sidetap.types`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_types.py tests/test_transcript.py -q -k "felt_latency or truncated or full_synthesis"`
Expected: FAIL, `TypeError: Latency.__init__() got an unexpected keyword argument 'tts_total_ms'`

- [ ] **Step 3: Implement**

In `sidetap/types.py`, replace the `Latency` dataclass:

```python
@dataclass(frozen=True)
class Latency:
    asr_ms: float = 0.0
    mt_ms: float = 0.0
    tts_ms: float = 0.0
    # Full synthesis wall time. Deliberately NOT part of total_ms: playout
    # starts on the first chunk, so what the listener waited for is tts_ms,
    # and adding the rest back would make the TUI overstate felt latency by
    # exactly the amount streaming saved. Kept because it is still the
    # throughput and cost signal.
    tts_total_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return self.asr_ms + self.mt_ms + self.tts_ms
```

Add `truncated` to `Record`:

```python
@dataclass(frozen=True)
class Record:
    """One transcript row."""

    unit: Unit
    target_text: str
    latency: Latency = field(default_factory=Latency)
    dropped: bool = False
    truncated: bool = False
```

In `sidetap/transcript.py`, in `record_to_dict`, add `"truncated"` beside
`"dropped"` and `"tts_total_ms"` inside the latency dict:

```python
        "dropped": record.dropped,
        "truncated": record.truncated,
        "latency": {
            "asr_ms": record.latency.asr_ms,
            "mt_ms": record.latency.mt_ms,
            "tts_ms": record.latency.tts_ms,
            "tts_total_ms": record.latency.tts_total_ms,
            "total_ms": record.latency.total_ms,
        },
```

In `render_markdown`, replace the suffix line:

```python
        suffix = "  _(not spoken: backlog dropped)_" if record.dropped else ""
```

with:

```python
        if record.dropped:
            suffix = "  _(not spoken: backlog dropped)_"
        elif record.truncated:
            suffix = "  _(cut short: synthesis failed)_"
        else:
            suffix = ""
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_types.py tests/test_transcript.py -q`
Expected: PASS

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sidetap/types.py sidetap/transcript.py tests/test_types.py tests/test_transcript.py
git commit -F - <<'MSG'
Record full synthesis time and truncation, apart from felt latency

tts_ms is about to mean time-to-first-chunk, so full synthesis wall time
needs its own field. tts_total_ms is deliberately not part of total_ms:
playout starts on the first chunk, and adding the rest back would make the
TUI overstate felt latency by exactly what streaming saved.

Record gains truncated, so a sentence cut short by a synthesis failure
reads differently in the transcript from one the lag cap never spoke.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
```

---

### Task 6: Stream the synthesis into playout

**Files:**
- Modify: `sidetap/pipeline.py:132-180` (`_speak`)
- Test: `tests/test_pipeline.py`

This is the task that actually delivers the latency win.

**Note on truncation, from Task 3's review.** There are two independent ways
an utterance ends early, and only one of them is visible from inside `_speak`:
synthesis failing (producer side), and playout abandoning the utterance at the
starvation bound (consumer side). An earlier draft of this step returned
immediately when `append()` was refused, which emitted **no transcript row at
all** for a sentence the listener had already partly heard, and charged
nothing for a request that was billed. It also read a local `truncated`
variable that could not see playout's decision. The code below breaks out of
the loop instead and takes `handle.truncated` as the single source of truth
after `finish()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pipeline.py`:

```python
def test_audio_reaches_playout_before_synthesis_finishes():
    seen = []

    class SlowSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            yield b"\x01\x02" * 8000
            seen.append(playout.backlog_s())   # first chunk already queued
            yield b"\x03\x04" * 8000

    playout = Playout(Direction.IN, FakeAudioSink())
    pipeline = _pipeline(synthesizer=SlowSynthesizer(), playout=playout)
    pipeline.handle(_final(t_end=1.0))

    assert seen and seen[0] > 0.0


def test_tts_latency_is_time_to_the_first_chunk_not_the_whole_synthesis():
    clock = FakeClock(start=10.0)

    class TickingSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            clock.advance(0.2)          # time to first chunk
            yield b"\x01\x02" * 8000
            clock.advance(2.0)          # the rest of the synthesis
            yield b"\x03\x04" * 8000

    metrics = Metrics()
    pipeline = _pipeline(
        synthesizer=TickingSynthesizer(), metrics=metrics, clock=clock
    )
    pipeline.handle(_final(t_end=1.0))

    latency = metrics.snapshot().directions[Direction.IN].latency
    assert latency.tts_ms == 200.0
    assert latency.tts_total_ms == 2200.0


def test_a_synthesis_failure_part_way_through_keeps_what_was_spoken():
    records = []

    class FailingSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            yield b"\x01\x02" * 8000
            raise RuntimeError("stream died")

    playout = Playout(Direction.IN, FakeAudioSink())
    pipeline = _pipeline(
        synthesizer=FailingSynthesizer(), playout=playout, on_record=records.append
    )
    pipeline.handle(_final(t_end=1.0))

    assert playout.backlog_s() > 0.0        # the first chunk is still spoken
    assert len(records) == 1
    assert records[0].truncated is True


def test_a_synthesis_failure_before_any_audio_records_nothing():
    records = []

    class FailingSynthesizer:
        def synthesize(self, text, voice, speaking_rate=1.0):
            raise RuntimeError("stream died")
            yield b""                        # pragma: no cover - generator marker

    playout = Playout(Direction.IN, FakeAudioSink())
    pipeline = _pipeline(
        synthesizer=FailingSynthesizer(), playout=playout, on_record=records.append
    )
    pipeline.handle(_final(t_end=1.0))

    assert playout.backlog_s() == 0.0
    assert records == []
```

Check the existing `_pipeline` helper in `tests/test_pipeline.py` accepts
`synthesizer`, `playout`, `metrics`, `clock` and `on_record` keyword
arguments. If it does not accept `playout` or `on_record`, add them with the
same defaults the helper already uses for the others.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_pipeline.py -q -k "before_synthesis_finishes or first_chunk or part_way_through or before_any_audio"`
Expected: FAIL. The first fails on `assert seen and seen[0] > 0.0` because
nothing is queued until synthesis completes. The second fails with
`assert 2200.0 == 200.0`, because `tts_ms` currently measures the whole
synthesis rather than the wait before the first chunk.

- [ ] **Step 3: Rewrite `_speak`'s synthesis section**

In `sidetap/pipeline.py`, replace everything from `started = self._clock.monotonic()`
(the **second** one, immediately after the `if not target_text.strip(): return`
guard) to the end of the method with:

```python
        started = self._clock.monotonic()
        handle = self._playout.begin(unit, target_text)
        chunks = self._synthesizer.synthesize(
            target_text, self._config.voice, self._config.speaking_rate
        )
        first_ms: float | None = None
        truncated = False
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
                    truncated = True
                    break
        except Exception as exc:
            log.error("synthesis failed (%s): %s", direction.value, exc)
            self._metrics.set_health(direction, tts=Health.FAILED)
            truncated = True
        else:
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_pipeline.py -q`
Expected: PASS

Run: `uv run pytest -q`
Expected: PASS. If a pre-existing test asserts a `tts_ms` equal to a whole
fake synthesis, update it to the first-chunk value — that is the intended
change, not a regression.

- [ ] **Step 5: Commit**

```bash
git add sidetap/pipeline.py tests/test_pipeline.py
git commit -F - <<'MSG'
Speak the first synthesis chunk instead of waiting for the last

_speak joined the whole generator before submitting, so the listener waited
for the end of the sentence to hear its beginning. Measured against Chirp 3
HD, that cost 271ms on a short utterance and 2109ms on a long one, because
synthesis runs several times faster than playback and the wait grows with
length.

Chunks now stream into playout as they arrive, and tts_ms becomes time to
the first chunk, so total_ms reads as what the listener actually waited.
Full synthesis time moves to tts_total_ms.

A failure part way through now keeps what was already spoken and records it
truncated, rather than discarding a sentence the listener has already half
heard. A failure before any audio behaves exactly as before.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
```

---

### Task 7: Correct the documentation that says this does not happen

**Files:**
- Modify: `sidetap/tts.py:1-22` (module docstring)
- Modify: `CLAUDE.md`

**Why:** `tts.py`'s docstring currently instructs the reader, in bold, that
the pipeline does not exploit streaming and tells them not to "fix" the
docstring. After Task 6 that text is actively misleading, and it names a
trade-off the measurement showed does not exist.

- [ ] **Step 1: Replace the `tts.py` docstring**

Replace lines 1-22 of `sidetap/tts.py` with:

```python
"""Google Cloud Text-to-Speech, Chirp 3: HD streaming synthesis.

One fixed voice per direction - no cloning, which costs roughly 600 ms of
time-to-first-audio for a v1 that does not need it.

`synthesize` is a true incremental generator and the pipeline exploits it:
DirectionPipeline._speak appends each chunk to a Playout utterance as it
arrives, so speech starts at time-to-first-chunk rather than at full
synthesis wall time. Measured (docs/experiments/04-tts-streaming.md), that
is the difference between 194 ms and 465 ms on a short utterance, and
between 230 ms and 2339 ms on a long one.

The shape that makes it safe: the first chunk is always 200 ms of audio,
every chunk after it is 240 ms arriving every ~30 ms, and production runs
4.7-7.1x faster than playback - so simulated early playout never dipped
below a 200 ms buffer margin across nine runs. Playout still waits for
START_BUFFER_S before starting, which costs ~30 ms and doubles that margin.

An earlier version of this docstring claimed early playout would force the
lag cap to estimate the backlog rather than measure it. It does not: at
those ratios, counting only bytes that have arrived understates the backlog
by a fraction of one utterance and self-corrects within about a second.

The first call after construction costs ~543 ms against a ~267 ms warm
median, which is why Session.setup() performs a throwaway synthesis while
the audio graph is being rewired.
"""
```

- [ ] **Step 2: Update `CLAUDE.md`**

In the **Modules** table, change the `playout.py` row from:

```
| `playout.py` | lag-capped queue, `DuckControl`, PCM writer |
```

to:

```
| `playout.py` | lag-capped queue of growable utterances, `DuckControl`, PCM writer |
```

In the **Non-obvious mechanics** section, add this bullet after the
"Cost is billed on audio actually sent" bullet:

```markdown
- **Playout starts an utterance before it has been fully synthesised.**
  `DirectionPipeline._speak` appends chunks to a `playout.Utterance` as they
  arrive rather than joining the generator, which is worth 271 ms on a short
  sentence and 2109 ms on a long one. Two consequences that are easy to break:
  an utterance that has *started* and then run dry holds the duck **closed**,
  because opening it would let a burst of the untranslated original through a
  mid-sentence gap — bounded by `STARVE_LIMIT_TICKS` so a dead producer cannot
  silence the remote party for the rest of the call; and the lag cap never
  drops an utterance that is still arriving, since its duration is unknown and
  it is always the newest thing queued.
```

- [ ] **Step 2b: Note the blocking-sink caveat in `run()`**

Task 3's review found a path worth documenting rather than fixing here. If the
sink is alive but blocking — `pw-cat` stops draining the deliberately-shrunk
pipe — `tick()` blocks *inside* `self._sink.write()` **after** the duck has
been set closed, so `_starved_ticks` freezes and the duck stays closed for as
long as the write blocks. This is not new in kind (a blocking write during
ordinary speech already held the duck closed), but starvation adds another way
to reach it, and the starvation bound cannot expire while the thread is parked
in `write()`. Bypass force-opens the ducks, so there is a manual escape.

Add a paragraph saying so to `Playout.run`'s docstring, beside the existing
explanation of why a dead sink needs the `stop.wait` fallback.

- [ ] **Step 3: Verify nothing else still describes the old behaviour**

Run:

```bash
grep -rn "b\"\".join\|does not currently exploit\|becomes estimated" sidetap/ docs/superpowers/specs/ CLAUDE.md
```

Expected: no hits in `sidetap/` or `CLAUDE.md`. The spec may legitimately
mention "becomes estimated" while quoting the old docstring.

- [ ] **Step 4: Run the full suite one more time**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sidetap/tts.py CLAUDE.md
git commit -F - <<'MSG'
Correct the docs that said the pipeline does not stream synthesis

tts.py's docstring told the reader in bold that _speak joins the generator,
and not to fix the docstring without fixing the pipeline. The pipeline is
fixed, so the instruction is now backwards.

It also named a trade-off that measurement retired: early playout does not
force an estimated backlog, because chunks arrive several times faster than
they play.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
```

---

## Manual verification

The automated suite cannot hear anything. After Task 7, before trusting this
on a real call, work through the capture/routing/playout sections of
`docs/manual-smoke.md`, plus these two checks specific to this change:

1. **The win is audible.** Run with `--no-tui` against a long sentence and
   confirm from the log that `tts_ms` is ~200 ms and `tts_total_ms` is much
   larger. The gap between them is the latency this plan removed.
2. **No mid-sentence duck flap.** During a long translated sentence the
   original must stay silent throughout, with no burst of it between chunks.
   This is the one regression the fakes structurally cannot catch, because a
   `FakeAudioSink` never stalls.

## Out of scope

Splitting long utterances at clause boundaries, and LocalAgreement-2 in the
segmenter seam. Both attack latency further; neither belongs in this change.
