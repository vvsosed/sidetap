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
application, measures mean RMS over 5 s at full volume, sets the stream volume
to 0, measures again over 5 s, restores.

**How to run.** Requires Task 10 to be complete (the script imports
`sidetap.adapters` and `sidetap.recorder`), and an application actually playing
audio:

    uv run python scripts/exp01_tap_volume.py --app zoom

A ratio under 0.1 means post-volume.

**Result.** _(fill in: RMS loud, RMS quiet, ratio, verdict, PipeWire version,
date)_

**Consequence.** The duck-node design in Task 20 is what gets built either way.
If the verdict is PRE-volume, open a follow-up to simplify `routing.py` — do
not change Task 20 mid-flight.
