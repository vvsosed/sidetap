# Experiment 1 — is the additive tap pre- or post-volume?

**Question.** When `wpctl set-volume` turns an application's stream down, does
an additive `pw-link` tap of that stream's output ports still receive
full-amplitude audio?

**Why it matters.** If the tap is post-volume, ducking the original by turning
the application down would also silence the recogniser that has to hear the
next sentence — so `routing.py` must own a separate duck node and re-route the
application through it. If the tap is pre-volume, `routing.py` collapses to one
`wpctl` call: no unlink, no re-route, no journal, no crash repair.

**Method.** `scripts/exp01_tap_volume.py` — additively taps a playing
application, drains ~3 s of the capture pipe (pw-record can leave up to
~2.05 s of audio buffered in the pipe from before a change, and reading that
backlog first would contaminate the next measurement with stale audio),
measures mean RMS over 5 s at full volume, sets the stream volume to 0,
drains again, measures again over 5 s, restores. Prefer a continuous level
source — music or a test tone — over a live conversation: speech has pauses,
and a pause landing in one 5 s window but not the other shifts the ratio for
a reason that has nothing to do with volume.

**How to run.** Requires Task 10 to be complete (the script imports
`sidetap.adapters` and `sidetap.recorder`), and an application actually playing
audio:

    uv run python scripts/exp01_tap_volume.py --app zoom

A ratio under 0.1 means post-volume.

**Recovery.** The script restores the stream's original volume and stops its
own `pw-record` in a `finally`, so a clean exit — including Ctrl-C in most
cases — leaves nothing behind. If it hangs anyway, the script prints, before
anything is muted, which identifier `wpctl` actually accepted for the stream
(it tries `object.id` first, falling back to `object.serial` — these are
different numbers, and only one of them works with `wpctl`) and the original
volume; recover by hand with that printed identifier:

    wpctl set-volume <id or serial, whichever the script printed> <original volume>
    pkill -f pw-record

**Result.** Run 2026-09-18 against Firefox playing continuous audio.
PipeWire 1.6.8 (libpipewire), Python 3.13.12.

```
tapping Firefox (serial=145, wpctl id=76), original volume=1.00
drained 30 stale blocks before measuring (3.1s)
RMS at full volume: 7028.0  (51 blocks, 5.1s measured)
drained 31 stale blocks after muting (3.1s)
RMS at zero volume: 0.0  (51 blocks, 5.1s measured)

ratio=0.000 -> tap is POST-volume (duck node REQUIRED)
```

**Verdict: POST-volume.** Unambiguous — not a marginal ratio. The tap received
a strong signal (RMS 7028 of a 32767 maximum) at full volume and *literally
zero* at muted, across 51 captured blocks in each window. Both windows measured
the full 5.1 s, so this is a real measurement and not a truncated one.

Three things this run also confirmed, each of which had been a correctness fix
made on review rather than on evidence:

1. **`object.serial` and wpctl's id are genuinely different numbers here** —
   serial 145, wpctl id 76. Passing the serial to `wpctl set-volume`, as the
   plan originally did, would have failed with "Object not found", and the
   duck would have silently never engaged.
2. **The stale-pipe-backlog fix was necessary.** 30-31 blocks (3.1 s) of stale
   audio were discarded before each measurement. Without the drain, roughly
   two seconds of *full-volume* audio would have been read at the head of the
   quiet window, pulling the ratio toward 0.4 and printing "PRE-volume" — the
   exact opposite of the truth.
3. **The volume restore read the prior value** (1.00) rather than assuming it,
   and put it back.

**Consequence.** The duck-node design in Task 20 is **required**, exactly as
specified: ducking the application's own stream volume would also silence the
recogniser that has to hear the next sentence. No simplification follow-up —
`routing.py` keeps the separate duck node, the re-route, the journal and the
startup repair.

**Consequence.** The duck-node design in Task 20 is what gets built either way.
If the verdict is PRE-volume, open a follow-up to simplify `routing.py` — do
not change Task 20 mid-flight.
