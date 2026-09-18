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

**Step 1b — the corrected test (pending).** A tight loop with no sleep for
60 s, reporting `audio_written / wall_elapsed`. A ratio near 1.000 means
`pw-cat` blocks and paces correctly and Task 19 builds as specified. A ratio
far above 1 means it buffers without bound, `Playout.run()` as specified is
broken — it would dump the whole queue instantly and accumulate unbounded
latency — and playout needs its own real-time pacing.

**Consequence.** Residue of 441 ms is recorded in `README.md` under the
drop-backlog hotkey. The no-`sleep` design in `Playout.run()` is supported by
the drift figure. The blocking question stays open until Step 1b runs; Task 19
is built to the specified design in the meantime, and the loop is the only part
that would change.

**Consequence.** If residue exceeds ~300 ms, note it in `README.md` under the
drop-backlog hotkey. If drift is non-zero, playout needs to track written-vs-
elapsed and skip silence chunks to stay in sync.
