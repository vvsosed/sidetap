# Manual smoke checklist

The automated suite runs with no audio hardware, no network and no credentials.
Everything below is what it therefore cannot verify. Run it by hand before
trusting a change to capture, routing, playout or the duck.

## Setup

- [+] `sidetap doctor --install`, then
      `systemctl --user restart pipewire pipewire-pulse`.
- [+] `sidetap doctor` reports all checks passing.
- [+] `wpctl status` shows both `sidetap_tts_sink` and `sidetap_virtmic`.

## Device visibility

- [+] Zoom's microphone list shows "sidetap Virtual Mic".
- [+] Select it, restart Zoom, and confirm the selection **survived**. This is
      the failure the permanent config file exists to prevent, and its symptom
      is the remote party hearing your untranslated voice.
- [?] In Chrome or an Electron client, disable the browser's own echo
      cancellation and noise suppression. They are applied to microphone input
      by default and can only gate or duck clean synthetic speech.

## The duck

- [ ] Start a call, run `sidetap run --app <app> --their-lang .. --my-lang ..`.
- [ ] While the remote party speaks, confirm you hear the translation and
      **not** their original voice.
- [ ] In the gaps between translations, confirm their original is audible
      again.
- [ ] During one long translated sentence, confirm their original stays
      silent for its **entire** length - no burst of it between synthesis
      chunks. `FakeAudioSink` never stalls, so this is the one regression the
      automated suite structurally cannot catch.
- [ ] Kill the network **in the middle of a long translated sentence**, not
      between them. Confirm you hear nothing at all briefly - that is the duck
      holding shut across the gap - and then their raw voice returns within
      about two seconds. Silence that does NOT end is the failure this whole
      module exists to prevent: it leaves the other party talking to someone
      who cannot hear them. Nothing in the automated suite can reach this,
      because no fake sink stalls.
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

## Mute, and how it composes with bypass

`m` is the toggle a user is most likely to leave on and forget, which is why
it is the one that has to look engaged. None of this is reachable from the
test suite: the suite can prove the class is set and the colours differ, but
not that the fill is legible in your terminal or that a click lands.

- [ ] Press `m`. Confirm the `Mute out` key in the footer fills with amber and
      its label stays readable — a theme whose key colour is close to the fill
      can leave the letter present but invisible.
- [ ] Confirm `Bypass` did **not** light, and neither did `Drop backlog` or
      `Quit`.
- [ ] **Click** the `Bypass` label with the mouse. Confirm it toggles exactly
      as `b` does, and lights.
- [ ] Press `m`, then `b`, then `b`. Confirm `Mute out` stays lit the whole
      way through and that you are **still muted** at the end. An earlier
      build cleared mute here silently, so the user came back audible to a
      call they thought they had stepped out of.
- [ ] Press `b`, then `m` while bypassed. Confirm the remote party hears no
      translated speech at any point — bypass's third effect must survive the
      mute key. Only the key's appearance should change.
- [ ] Mute during continuous speech for ~30 s, then unmute. Confirm nothing
      replays. Synthesis keeps filling the queue while muted, and the lag cap
      only bounds it at 20 s rather than preventing it.
- [ ] Narrow the terminal until the footer scrolls, then toggle. Confirm the
      lit key survives the reflow — Footer rebuilds its keys on layout
      changes, and the class is re-applied by polling rather than held.

## Voice selection

Which voice is speaking can only be checked by ear. The suite pins the table
and proves the flag reaches `DirectionConfig`; it cannot tell you the audio
coming out of your headphones is the voice you asked for.

- [ ] Run with no voice flags at all. Confirm you hear a male voice and the
      remote party hears a female one (for an `en-US` / `ru-RU` pair) — the
      built-in defaults are not uniform, and this is the baseline any later
      change to the table has to preserve.
- [ ] Add `--voice-in-gender female --voice-out-gender male` and confirm both
      directions swap. Both, not one: the flags are written out separately and
      a copy-paste slip in either one is invisible to everything else.
- [ ] Confirm `--voice-in en-US-Chirp3-HD-Kore --voice-in-gender male` exits
      immediately with a usage message, before the graph is touched or any
      cloud call is made.

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
      **stops** within about half a second rather than playing to its end:
      flush() clears the in-progress utterance as well as the queue, and what
      you still hear is the 441 ms experiment 2 measured already sitting
      inside pw-cat, which cannot be recalled.

## Committing early (`LocalAgreementSegmenter`)

Everything below needs someone willing to talk for half a minute without
pausing. Nothing in the test suite can reach any of it.

- [ ] **A monologue keeps being spoken, instead of stopping.** Have the other
      party talk continuously for 30 s. The first sentence arrives at the same
      time it always did - what should change is that translation keeps coming
      every few seconds afterwards, rather than going silent until they stop
      and then delivering a wall of speech. Compare directly against
      `--no-early-commit`.
- [ ] **The duck does not flap between clauses.** This is the check that
      decides whether the feature ships on by default. Listen for the original
      bleeding through *inside* the monologue, in the gap between one
      committed clause and the next. It should not be audible at all. If it
      is, `Playout.expect_continuation` is not being armed.
- [ ] **Ordinary conversation is unchanged.** Normal turn-taking produces no
      interim results, so nothing should sound or read differently from
      before. If short sentences start arriving in fragments, the segmenter is
      committing on one hypothesis instead of two.
- [ ] **The transcript reads as clauses, not as a jumble.** A monologue should
      render as several rows whose text joins back into the whole utterance,
      with no word repeated and none missing. Word order across a clause
      boundary is the quality cost the design named, and this is the only
      place it can be judged.
- [ ] **Nothing is spoken twice, and the log says why if it is.** This is the
      check that found the worst regression this feature has had: Chirp
      re-windows a long hypothesis mid-utterance, and the first version read
      that as a dead stream and re-spoke whole clauses - roughly 15% of a
      five-minute call. `segment.py` now aligns every candidate against what
      it has actually spoken, and logs at **warning** ("shares no words with
      the N already spoken") the one case where a repeat can still reach the
      ear. Grep the session log for that line after any long monologue: with
      no warnings and a repeat audible, the alignment is wrong; with warnings
      and no repeat, a stream restarted, which is benign. A repeat of a few
      words under that warning is expected too: a re-window over fewer than
      `MIN_ANCHOR` (8) spoken words is repeated by design.
- [ ] **Nothing new is dropped, and the log says what was.** When a hypothesis
      re-windows back past where the spoken text began, `segment.py` anchors
      on the spoken run inside it and drops the words in front, logging at
      **warning** ("dropped N leading word(s) in front of a run of M already
      spoken"). This is the one path that can lose speech rather than repeat
      it, and nobody hears a word that was never played, so check each line
      against the transcript: the N dropped words should be ones from before
      the run, not new speech. M far above 8 with N small is a real
      re-window; M near 8 with a clause dropped is a false anchor, and means
      `MIN_ANCHOR` is too low. The same N on consecutive lines is one
      re-window, logged again at each interim until its final.
- [ ] **The end of a monologue releases the duck promptly.** When the speaker
      stops, the original should become audible again within a second or so -
      not after the 2 s bound, which would mean the final never cleared the
      hold.
- [ ] **A translator or synthesis failure mid-monologue does not silence the
      call.** The duck must reopen within about two seconds of the last audio
      actually played. If the far end goes quiet for longer than that, the
      hold is being armed on a failure path.

## Lag and drops

- [ ] Have the remote party talk continuously for two minutes.
- [ ] Confirm the lag figure rises and then stops at the cap rather than
      growing without bound.
- [ ] Confirm the dropped count increments and the transcript's Markdown marks
      the dropped utterances as "not spoken".

## Restoration

- [ ] Ctrl-C. Confirm the application's audio returns to your speakers and
      `wpctl status` shows no leftover `sidetap_duck`.
- [ ] Repeat, but close the terminal window instead of pressing Ctrl-C.
      Confirm the same: audio back on your speakers, no leftover duck.
- [ ] Repeat, but `kill -9` the process instead. Confirm call audio is gone,
      then run `sidetap doctor --repair` and confirm it comes back.
- [ ] Set the call app to play on a device that is **not** the default
      output, then run a call. Confirm the original is ducked under the
      translation, and that after Ctrl-C the app plays on that device again
      and nowhere else.
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

- [ ] Speak one long sentence, then open that call's `.jsonl` transcript and
      find the utterance's `latency` block. Confirm `tts_ms` reads close to
      200 ms while `tts_total_ms` is much larger - the TUI pane only ever
      shows `tts_ms`, so the jsonl is the one place both are visible
      together. A small gap between them means streaming did not happen for
      that utterance.
- [ ] Measure glass-to-glass: have the remote party say a short sentence and
      time from the end of their speech to the start of the translation.
      Target is under 2.5 s per direction.
- [ ] Time the gap at a 4-minute stream rotation boundary. Under
      full-replacement routing the user hears nothing at all during the
      reconnect, so measure whether it can swallow a sentence.
- [ ] Run a 30-minute call. Confirm no gap at the 4-minute stream rotations and
      no drift in playout.
