# Manual smoke checklist

The automated suite runs with no audio hardware, no network and no credentials.
Everything below is what it therefore cannot verify. Run it by hand before
trusting a change to capture, routing, playout or the duck.

## Setup

- [ ] `sidetap doctor --install`, then
      `systemctl --user restart pipewire pipewire-pulse`.
- [ ] `sidetap doctor` reports all checks passing.
- [ ] `wpctl status` shows both `sidetap_tts_sink` and `sidetap_virtmic`.

## Device visibility

- [ ] Zoom's microphone list shows "sidetap Virtual Mic".
- [ ] Select it, restart Zoom, and confirm the selection **survived**. This is
      the failure the permanent config file exists to prevent, and its symptom
      is the remote party hearing your untranslated voice.
- [ ] In Chrome or an Electron client, disable the browser's own echo
      cancellation and noise suppression. They are applied to microphone input
      by default and can only gate or duck clean synthetic speech.

## The duck

- [ ] Start a call, run `sidetap run --app <app> --their-lang .. --my-lang ..`.
- [ ] While the remote party speaks, confirm you hear the translation and
      **not** their original voice.
- [ ] In the gaps between translations, confirm their original is audible
      again.
- [ ] Kill the recogniser's network (disable wifi for 20 s). Confirm you start
      hearing their raw voice rather than silence. This is the IN direction's
      fail-safe and it must not regress.

## Dead air

- [ ] With the call up, revoke the Translation API permission (or set an
      invalid `--project`), then speak for 10 seconds.
- [ ] Confirm the OUT pane turns red and an earcon plays in your headphones.

## Bypass — three effects, and every one of them can fail alone

Bypass is the control you reach for when the interpretation is making the call
worse, so it has to work when everything else is not.

- [ ] Press `b`. Confirm **all three** at once: you hear the remote party's
      own voice unducked, they hear your own voice untranslated, and no
      translated speech plays over either. Any one of these failing alone
      leaves two people talking over a robot.
- [ ] Press `b` again. Confirm the interpretation resumes, and that it does
      **not** replay a backlog of everything said while bypassed.
- [ ] Press `b`, then Ctrl-C while still bypassed. Run
      `pw-link -l | grep sidetap` and confirm no leftover link from your
      microphone into `sidetap_tts_sink`. That link outlives the process, is
      not in the journal, and `doctor --repair` cannot find it — so if it is
      there, every later call carries your raw voice alongside the
      translation.

## Silence that is not silence

An unlinked PipeWire capture node delivers zero bytes rather than silence, so
every stage downstream looks healthy while doing nothing.

- [ ] Mid-call, quit the tapped application entirely. Within ~15 s confirm the
      IN pane reads `NO AUDIO ARRIVING` and not `DEAD AIR` — they point at
      opposite ends of the pipeline and the wrong one sends you debugging the
      wrong half.
- [ ] Start sidetap *before* the call and leave it for a minute with nothing
      playing. Confirm the IN pane does **not** alarm. Alarming here is how a
      warning gets trained out of a user.

## Playout buffering

Experiment 2 shrank the playback pipe to 16 KiB, buying about 960 ms off every
utterance. It is the one change that trades buffer headroom for latency, and an
underrun is audible where the old 1.3 s buffer was merely slow.

- [ ] Listen for crackling or dropouts across a full call. Measured stable at
      16 KiB here, but that was one machine.
- [ ] Press the drop-backlog hotkey mid-utterance. Confirm the current sentence
      **finishes** rather than cutting off: experiment 2 measured 441 ms of
      audio already past `pw-cat` that cannot be recalled. The hotkey prevents
      the next sentence, it does not truncate this one.

## Lag and drops

- [ ] Have the remote party talk continuously for two minutes.
- [ ] Confirm the lag figure rises and then stops at the cap rather than
      growing without bound.
- [ ] Confirm the dropped count increments and the transcript's Markdown marks
      the dropped utterances as "not spoken".

## Restoration

- [ ] Ctrl-C. Confirm the application's audio returns to your speakers and
      `wpctl status` shows no leftover `sidetap_duck`.
- [ ] Repeat, but `kill -9` the process instead. Confirm call audio is gone,
      then run `sidetap doctor --repair` and confirm it comes back.
- [ ] Start sidetap with **no call running at all**, wait, then Ctrl-C. Confirm
      `wpctl status` shows no leftover `sidetap_duck`. Nothing is ever routed
      in this case so the journal stays empty, which is exactly the path that
      used to skip tearing the duck down.

## Tuning constants that were inherited, not measured

Both came from meetscribe, a transcriber with no latency budget. Neither has
been measured against Chirp 3 or against sidetap's two very different input
signals. Record numbers, not opinions.

- [ ] **`SILENCE_TAIL_BLOCKS = 5`** forwards 500 ms of trailing silence so the
      engine can finalise, sitting inside the spec's 300-800 ms "endpoint
      wait" budget. Measure which way it is wrong: if Chirp 3 needs more than
      500 ms of trailing audio to fire, finalisation falls through to the 2 s
      keepalive cadence instead and blows the latency target; if it finalises
      faster, the tail is billed silence for nothing.
- [ ] **`aggressiveness = 2`** is used for BOTH directions, but its docstring
      justifies it for "meeting audio, where fans and keyboards are common" —
      a raw room mic. The remote direction is nothing like that: it arrives
      already compressed, AGC'd and probably noise-suppressed by the far end.
      Check whether it clips quiet speech or passes too much comfort noise,
      and whether the two directions want different values.

## Timing

- [ ] Measure glass-to-glass: have the remote party say a short sentence and
      time from the end of their speech to the start of the translation.
      Target is under 2.5 s per direction.
- [ ] Time the gap at a 4-minute stream rotation boundary. Under
      full-replacement routing the user hears nothing at all during the
      reconnect, so measure whether it can swallow a sentence.
- [ ] Run a 30-minute call. Confirm no gap at the 4-minute stream rotations and
      no drift in playout.
