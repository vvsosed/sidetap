# Experiment 2 — does `pw-cat --playback` from a stdin pipe hold up?

**Question.** Playout keeps one long-lived `pw-cat` per direction and writes
silence between utterances rather than stopping. Does that hold steady for a
call's duration, and how much audio still emerges after a flush?

**Why it matters.** If the buffer grows without bound, lag accumulates
invisibly and the lag cap measures the wrong thing. The post-flush residue sets
how honest the "drop backlog" hotkey can be.

**Method.** Write 20 ms silence chunks continuously for 30 minutes; watch for
drift between chunks written and wall-clock elapsed. Then write 2 s of tone,
stop writing after 200 ms, and time how long sound continues.

**How to run.** Needs any playback sink — the default output device is fine.
This measures `pw-cat`'s own pacing, which does not depend on the target
sink, so it does not need the virtual mic from Task 23.

Step 1 — drift over 30 minutes of continuous silence:

    uv run python - <<'PY'
    import subprocess, time
    p = subprocess.Popen(
        ["pw-cat", "--playback", "--rate", "24000", "--channels", "1",
         "--format", "s16", "--raw", "-"],
        stdin=subprocess.PIPE,
    )
    chunk = b"\x00" * 960          # 20 ms of silence at 24 kHz s16 mono
    start = time.monotonic()
    written = 0
    while time.monotonic() - start < 1800:      # 30 minutes
        p.stdin.write(chunk)
        p.stdin.flush()
        written += 1
        time.sleep(0.02)
    print("wrote", written, "chunks in", time.monotonic() - start, "s")
    p.stdin.close()
    PY

Step 2 — post-flush residue. Feed 200 ms of an audible tone (so a human
listening can cross-check), then stop writing and close stdin. `pw-cat` has
no way to un-buffer audio it and PipeWire already queued, so the time from
"stop writing" to the process actually exiting is a runnable proxy for how
long sound keeps coming out after the caller stops feeding it:

    uv run python - <<'PY'
    import math, struct, subprocess, time

    RATE = 24000

    def tone_chunk(ms, freq=440.0, amplitude=8000):
        n = RATE * ms // 1000
        samples = [
            int(amplitude * math.sin(2 * math.pi * freq * i / RATE))
            for i in range(n)
        ]
        return struct.pack(f"<{n}h", *samples)

    p = subprocess.Popen(
        ["pw-cat", "--playback", "--rate", str(RATE), "--channels", "1",
         "--format", "s16", "--raw", "-"],
        stdin=subprocess.PIPE,
    )
    chunk = tone_chunk(20)      # 20 ms per write, same cadence as playout
    written_ms = 0
    while written_ms < 200:     # "stop writing after 200 ms"
        p.stdin.write(chunk)
        p.stdin.flush()
        written_ms += 20
    stop = time.monotonic()
    p.stdin.close()             # no more audio is coming; what's already
    p.wait()                    # queued keeps playing until this returns
    drain_s = time.monotonic() - stop
    print(f"residue: {drain_s * 1000:.0f} ms from stop-writing to pw-cat exit")
    PY

**Result.** Run 2026-09-18.

**Step 2 — post-flush residue: 441 ms.**

```
residue: 441 ms from stop-writing to pw-cat exit
```

Larger than the ~100-200 ms the plan assumed. When the drop-backlog hotkey
fires, the queue clears at once but nearly half a second of already-queued
audio still plays — so it cannot cut the current sentence short, only prevent
the next one. Goes in `README.md` beside the hotkey.

**Step 1 — inconclusive as originally written. The test was wrong.**

```
wrote 89382 chunks in 1800.0s
audio written: 1787.6s | drift: -12.4s
```

Two things came out of it, one useful and one a flaw in the experiment:

*Useful:* `pw-cat` fed continuously for 30 minutes without crashing, stalling
or disconnecting. And a **sleep-paced** loop drifts −0.69% — each iteration
cost 20.14 ms against a 20 ms target, under-feeding the sink by 12.4 s over
half an hour. That independently validates `Playout.run()` having **no**
`sleep`: a sleep-paced playout would slowly starve the sink and produce gaps.

*The flaw:* because the loop slept 20 ms per 20 ms chunk, the pipe buffer never
filled, so **`pw-cat` never had to block**. The loop paced itself. But
`Playout.run()` has no sleep at all — it writes in a tight loop and depends
entirely on `pw-cat` blocking once its buffer is full. That blocking *is* the
clock. So this run measured `time.sleep`'s accuracy rather than the mechanism
playout actually rests on.

**Step 1b — the corrected test: `pw-cat` blocks and paces correctly.**

```
wrote 3053 chunks = 61.1s of audio in 60.0s wall
ratio: 1.018   (1.000 = pw-cat paces correctly)
```

`Playout.run()`'s no-sleep design is sound — `pw-cat` blocking genuinely is
the clock. No Critical flaw.

**Step 1c — the 1.8% overshoot turned out to be the real finding.**

61.1 s of audio accepted in 60.0 s of wall clock means ~1.06 s was sitting
buffered. That matters more than it looks: playout writes silence continuously
between utterances, so whatever the pipe holds sits **ahead of every real
utterance**. A second of queued silence was being added to glass-to-glass
latency, invisibly.

The buffer is the **OS pipe**, not `pw-cat`'s node latency — 64 KiB ÷ 48000 B/s
= 1365 ms. Passing `--latency 20ms` alone changed nothing (1180 ms). Shrinking
the pipe with `F_SETPIPE_SZ` is what works. Steady-state buffering, three 12 s
runs at each size:

| pipe | capacity | runs (ms buffered) | |
|---|---|---|---|
| 8 KiB | 171 ms | `-57, +129, +139` | starves — the negative run means the buffer ran dry |
| **16 KiB** | **341 ms** | **`+299, +299, +299`** | **chosen: zero variance** |
| 32 KiB | 683 ms | `+609, +609, +609` | stable, leaves 300 ms unclaimed |
| 64 KiB | 1365 ms | `+1259, +1259, +1249` | the default |

`adapters.py` now sets `PIPE_BYTES = 16384` and passes `--latency 20ms`,
buying about **960 ms off every utterance**. 8 KiB's lower median was rejected:
a buffer that runs dry produces audible crackling, which is worse than 300 ms
of latency.

**Consequence.**

1. `Playout.run()` builds as specified — no `sleep`, `pw-cat` blocking is the
   pacing. Confirmed, not assumed.
2. `adapters.py` shrinks the playback pipe to 16 KiB and passes
   `--latency 20ms`, removing ~960 ms of buffered silence from in front of
   every utterance. This is the single largest latency win found so far,
   larger than the ~400-850 ms that unexploited TTS streaming costs.
3. Residue of 441 ms goes in `README.md` beside the drop-backlog hotkey: the
   queue clears at once, but what is already in the pipe still plays, so the
   hotkey cannot cut the current sentence short.
4. **Add to the manual smoke checklist:** listen for crackling or dropouts on
   a real call. 16 KiB measured stable here across every run, but this is the
   one change that trades buffer headroom for latency, and an underrun is
   audible where the old 1.3 s buffer was merely slow.
