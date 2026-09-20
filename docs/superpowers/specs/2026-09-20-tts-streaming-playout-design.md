# Streaming playout — speak the first chunk instead of the last

Status: design, approved 2026-09-20.

## The problem

`DirectionPipeline._speak` does `b"".join(synthesizer.synthesize(...))`. The
generator is genuinely incremental, but joining it discards that: the listener
waits for the *whole* utterance to be synthesised before hearing the first
word of it.

Chirp 3 HD delivers its first chunk in ~200 ms and the rest at 4.7-7.1x real
time, so the cost of joining grows with sentence length:

| utterance | audio | full synthesis | first chunk | saved |
|---|---|---|---|---|
| short | 2.2 s | 465 ms | 194 ms | **271 ms** |
| medium | 6.9 s | 1052 ms | 208 ms | **844 ms** |
| long | 16.6 s | 2339 ms | 230 ms | **2109 ms** |

Measured 2026-09-20; see `docs/experiments/04-tts-streaming.md`.

This is the largest single latency win left in the pipeline, and it is
concentrated on long sentences — the ones where waiting is already worst.

## What this is not

It is **not** a fix for backlog. Backlog grows at `(output duration / input
duration) - 1` per second and is a rate problem; this change moves the
starting offset, not the slope. `--speaking-rate-in` remains the only lever
that makes a backlog drain. Anyone reading this spec hoping to stop the drops
is in the wrong document.

## The trade-off that turned out not to exist

`tts.py`'s module docstring warns that exploiting streaming means "the backlog
becomes estimated rather than known". It does not.

Chunks arrive 4.7-7.1x faster than playout consumes them, so counting only
bytes that have actually arrived understates the backlog by at most a fraction
of one utterance, and self-corrects within about a second. The lag cap stays
measured. That docstring paragraph is now wrong and is replaced as part of
this work.

## Design

### Utterance — a queue entry that can still be growing

`playout.py` gains the queue's entry type:

```python
@dataclass
class Utterance:
    unit: Unit
    text: str
    pcm: bytearray = field(default_factory=bytearray)
    closed: bool = False      # synthesis finished, or failed
    dropped: bool = False     # flushed; the producer must stop synthesising
    truncated: bool = False   # closed by a failure, not by completion
```

Mutated only under `Playout._lock`, like every other piece of Playout's state.

`Translated` is unchanged and remains the **callback** currency. By the time
`on_spoken` or `on_dropped` fires, the audio is complete, so the immutable type
is still honest there; `Utterance.snapshot()` produces one.

One entry is still one sentence. That invariant is what the lag cap, the
`dropped` counter and the transcript all rest on, and preserving it is the
whole reason for a growable entry rather than the cheaper alternative of
queueing each chunk separately.

### Playout's API

```python
begin(unit, text) -> Utterance      # queued immediately, empty
append(item, chunk) -> bool         # False means "dropped, stop synthesising"
finish(item, *, truncated=False)
submit(translated)                  # begin + append + finish
```

`submit()` stays. A complete utterance is the degenerate streaming case, and
24 call sites across `test_playout.py` and `test_tui.py` test queue behaviour
this change must not alter.

Internally `_pending: bytes` becomes `_current: Utterance | None` plus
`_offset: int`, so the buffer can grow underneath a reader already partway
through it.

### tick() gains one state

Beyond "speech" and "idle" there is now *started, but nothing to read yet*:

A head utterance is *startable* once `offset > 0` (already started), or it
is `closed`, or it holds at least the start threshold below. Given that:

| head state | action | duck |
|---|---|---|
| a full 20 ms unread | write 20 ms | closed |
| a final partial chunk of a `closed` utterance | write it, zero-padded | closed |
| no unread bytes, `closed` | fire `on_spoken`, pull next | — |
| under 20 ms unread, open, started | write silence, do not advance | **held closed** |
| not yet startable (under threshold) | write silence | open |
| nothing queued | write silence | open |

The second and fourth rows are one distinction and it is load-bearing:
**zero-padding is only ever correct at the genuine end of an utterance.**
Padding a short remainder of an utterance that is still arriving splices
silence into the middle of a word, and because that path writes a chunk it
also resets the starvation counter and closes the duck — so the
`STARVE_LIMIT_TICKS` bound below could never fire, and a trickling producer
would pin the duck closed for the rest of the call. Real Chirp chunks happen
to be exact multiples of 20 ms, which hides this; `FakeSynthesizer`'s are not,
and neither is a clause-splitting producer.

"Started" means `offset > 0` — the listener has already heard part of this
sentence. That is the distinction the duck follows: an utterance that has
begun and then starved is still in progress, while one that has not begun is
not, and the original should stay audible until the translation actually
starts.

The held-closed row is the point. Opening the duck for a mid-sentence gap
lets a burst of the untranslated original through, which is worse than the
gap itself.

That row needs a fail-safe: **100 consecutive starved ticks (2 s)** abandons
the utterance as truncated and opens the duck. Ticks rather than seconds
because it needs no clock port in `playout.py`, is deterministic under test,
and counts the quantity that actually matters — silence written. Without it a
producer thread dying mid-utterance leaves the duck closed for the rest of the
call, which is the exact failure `CLAUDE.md` calls silently cruel.

### Start threshold

An utterance does not start until it holds **400 ms of audio or is closed**.

Costs ~30 ms, because chunk 2 lands ~30 ms after chunk 1, and doubles the
stall margin from 200 ms to 440 ms. Against a 271-2109 ms win that is cheap.
The `closed` half of the condition means an utterance shorter than 400 ms
still plays as soon as it is complete.

### Who the lag cap may drop

**An open utterance is never dropped.** Its duration is unknown, so trimming
it cannot be shown to help, and it is always the newest content: `popleft()`
drops oldest-first, so the open one sits at the tail and is only reachable
when it is the sole queued item. The trim loop breaks on it rather than
spinning.

This rests on an invariant worth stating explicitly: **at most one utterance
per direction is open at a time**, because `_speak` runs sequentially on that
direction's consume thread and exhausts its generator before returning.

Cancellation (`append() -> False`) is therefore driven by `flush()`, not by
the cap — that is, by bypass, which is the case that matters: entering bypass
should stop a synthesis in flight, not merely discard its output. `_speak`
breaks out of the loop and calls `.close()` on the iterator explicitly, so the
gRPC stream ends rather than waiting for garbage collection.

Backlog becomes `(len(current.pcm) - offset) + sum(len(i.pcm) for i in queue)`.

### Metrics and transcript

- `Latency.tts_ms` becomes **time to first chunk**, measured from the
  synthesis request to the first chunk handed to `append()`, so `total_ms`
  reads as felt latency — the number the TUI has always been used as.
- `Latency.tts_total_ms` is new and deliberately **not** part of `total_ms`.
  It carries full synthesis wall time to the jsonl and the debug log, so the
  throughput and cost signal is not lost.
- `Record` gains `truncated: bool`; `render_markdown` marks it
  `_(cut short: synthesis failed)_`, beside the existing dropped marker.

### Failure part-way through

Synthesis failing after some chunks have played is a new case — today a
failure means nothing is spoken at all.

- **After at least one chunk:** play what arrived, emit the record marked
  truncated, health -> FAILED, and charge the cost. A half sentence tells the
  listener something broke; silence does not, and this module's existing
  stance is that failures should be audible rather than silent. The request
  went out and produced audio, so it is billed.
- **Before any chunk:** unqueue the empty utterance and return with no record
  — byte-for-byte today's behaviour.

## Testing

New coverage in `tests/test_playout.py` and `tests/test_pipeline.py`:

- the start threshold holds, then releases at 400 ms
- an utterance shorter than the threshold plays once closed
- starvation writes silence without advancing, and holds the duck closed
- the starvation limit abandons the utterance and re-opens the duck
- `flush()` cancels an in-flight synthesis (`append` returns False)
- `on_spoken` fires exactly once, at close-and-drain
- backlog counts only arrived bytes
- partial failure plays what arrived and records it truncated
- failure before the first chunk leaves no record
- `tts_ms` is time-to-first-chunk, `tts_total_ms` is full synthesis

`FakeSynthesizer` already yields two chunks, so existing pipeline tests
exercise the streaming path without modification — which is the point of it
yielding two.

## Out of scope

Splitting long utterances at clause boundaries, and LocalAgreement-2 in the
segmenter seam. Both attack latency further; neither belongs in this change.
