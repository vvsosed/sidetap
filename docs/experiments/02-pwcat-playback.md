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

**Result.** _(fill in: chunks written vs elapsed, drift, residue in ms)_

**Consequence.** If residue exceeds ~300 ms, note it in `README.md` under the
drop-backlog hotkey. If drift is non-zero, playout needs to track written-vs-
elapsed and skip silence chunks to stay in sync.
