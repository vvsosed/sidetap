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

**How to run.** Requires the virtual mic from Task 23.

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

**Result.** _(fill in: chunks written vs elapsed, drift, residue in ms)_

**Consequence.** If residue exceeds ~300 ms, note it in `README.md` under the
drop-backlog hotkey. If drift is non-zero, playout needs to track written-vs-
elapsed and skip silence chunks to stay in sync.
