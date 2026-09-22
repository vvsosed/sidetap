# sidetap

Real-time two-way voice interpretation for any call on Linux — Zoom, Viber,
Telegram, Discord, Slack huddles — captured straight from **PipeWire** instead
of integrating with each platform's API. The remote party speaks their
language and you hear yours; you speak yours and they hear theirs. Neither
side installs anything or changes platform.

It is a cascaded pipeline run twice, once per direction, entirely on Google
Cloud: Speech-to-Text v2 (Chirp 3, streaming) → Cloud Translation v3 →
Text-to-Speech (Chirp 3 HD, streaming synthesis).

## Why PipeWire

A PipeWire output port can feed several input ports at once. That means
sidetap can tap the messenger's audio *in addition to* its existing link to
your speakers, and inject translated speech through a virtual microphone the
messenger selects as an ordinary input device. Neither tap needs the
messenger's cooperation or an API integration — the same mechanism works for
every application PipeWire can see.

On PulseAudio the alternatives are worse: capture the whole sink monitor and
mix in every notification chime, or move the app's stream to a null sink and
stop hearing the call yourself.

## Requirements

- Linux with PipeWire >= 0.3.60 (needed for `target.object` and
  `stream.capture.sink`).
- A GCP project with the Speech-to-Text, Cloud Translation and Text-to-Speech
  APIs enabled, and credentials for it — either
  `gcloud auth application-default login`, or
  `GOOGLE_APPLICATION_CREDENTIALS` pointed at a service account key.
- `webrtcvad-wheels`, installed by `uv sync` below. It gates silence out of
  what gets billed; see **Cost** for why a missing install is expensive
  rather than merely degraded.

Install PipeWire's CLI utilities if they aren't already present:

```bash
# Arch
sudo pacman -S pipewire pipewire-audio wireplumber

# Debian/Ubuntu
sudo apt install pipewire-bin pipewire-audio wireplumber
```

Then, from the repository root:

```bash
uv sync
```

`uv` is the only supported way to install and run this project — never `pip
install` or activate `.venv` by hand; `uv run` does both, from the lockfile.

## Setup (one time)

sidetap needs a permanent virtual microphone: a runtime-created device gets a
different identity every session, so Zoom and Viber quietly lose the saved
selection and fall back to your real microphone — the remote party then hears
your untranslated voice with nothing on screen saying so. So the device lives
in a config file, installed once:

```bash
sidetap doctor --install
systemctl --user restart pipewire pipewire-pulse
```

Both steps are needed: the first writes
`~/.config/pipewire/pipewire.conf.d/90-sidetap-mic.conf` (it never overwrites
an existing one), the second is what actually loads it. Confirm with:

```bash
sidetap doctor
```

which should report every check passing — PipeWire's version, the required
`pw-*`/`wpctl` binaries, the virtual mic, a live `pw-link`, `webrtcvad`, your
credentials, and (unless you pass `--no-api-check`) one cheap real call to
each of the three Google APIs, so a disabled API fails in a second here
rather than two minutes into a live conversation.

## Start your call first, then run `sidetap devices`

**This is the single most common first-run confusion.** An application does
not appear anywhere in the PipeWire graph until it actually starts an audio
stream — Zoom, for instance, creates one only once the meeting itself starts,
not when the app launches. Running `devices` before that shows no application
at all.

```bash
$ sidetap devices

=== OUTPUT DEVICES (sinks) ===
  serial=62     ... Analog Stereo [default]
      node.name = alsa_output.pci-....analog-stereo

=== INPUT DEVICES (sources / microphones) ===
  serial=60     USB Microphone Mono [default]
      node.name = alsa_input.usb-....mono-fallback

=== APPLICATIONS CURRENTLY PLAYING AUDIO ===
  (none - start your Zoom/Viber call, then run this again)
```

Start the call, then run it again:

```
=== APPLICATIONS CURRENTLY PLAYING AUDIO ===
  serial=2328   zoom.real  binary=zoom  pid=3553
      --app 'zoom'
```

That last line is the flag value to pass to `sidetap run`.

## Running a call

```bash
uv run sidetap run \
  --app zoom \
  --their-lang ru-RU \
  --my-lang en-US \
  --project my-gcp-project \
  --phrase "Volodymyr" --phrase "sidetap"
```

`--app` matches a substring of the name/binary shown by `devices`.
`--their-lang`/`--my-lang` are BCP-47 codes (`ru-RU`, `en-US`, `uk-UA`, ...);
`--voice-in`/`--voice-out` pick the Chirp 3 HD voice each side hears, defaulted
from a small built-in table — pass one explicitly for a language pair not in
it, or the run fails fast rather than guessing.
`--voice-in-gender`/`--voice-out-gender` take `male` or `female` and pick from
that same table, so you do not have to know that the voices are named after
moons. Naming a voice and asking for a gender in the *same* direction is
rejected at parse time rather than one quietly winning. The built-in defaults
are deliberately not uniform: English defaults male and every other language
defaults female, so in an `en-US ↔ ru-RU` call the two directions sound
different without either flag. `--phrase` boosts recognition
of a name or term that would otherwise get mangled; repeat it as needed.
`--project` can be omitted if `GOOGLE_CLOUD_PROJECT` is set. `--no-tui` drops
the dashboard for plain console logging, useful over SSH or in CI.
`LocalAgreementSegmenter` commits a monologue clause by clause instead of
waiting for one final, but it only engages after roughly 11 s of continuous
speech, so it changes nothing for ordinary turn-taking; `--no-early-commit`
falls back to the old one-clause-per-final behaviour, and the flag exists so
the two can be compared on a real call.

### What you hear, what they hear

Routing is **full replacement**, not an overlay: you never hear the remote
party's raw voice at the same time as the translation, and they never hear
your raw voice at all. Concretely —

- While the remote party is speaking, their original is ducked to silence
  (via a loopback node sidetap owns, `wpctl set-volume`d to 0) and you hear
  only the translation. In the gaps between utterances their original plays
  normally.
- **If synthesis stalls mid-sentence you hear nothing from anyone** until it
  resumes, for at most two seconds. The duck stays shut across the gap
  deliberately: letting the original back in for a moment mid-sentence is
  more jarring than a short silence. Past two seconds sidetap gives the
  sentence up and their voice returns, because a duck that stays shut leaves
  you talking to someone who cannot hear you.
- Your voice is recognised, translated and synthesised, and *only* the
  synthesised result reaches the messenger, through the virtual microphone.
  Your real microphone is never linked into the call outside of bypass.

Because there is no overlay, **this is half-duplex**: the interpretation only
starts once your sentence has finished (finals-only, v1), so the natural
cadence is "speak, pause, let it interpret," on both sides. Talking
continuously without pausing works against the pipeline rather than with it —
see **Known limitations**.

### Hotkeys

| Key | Action |
|---|---|
| `b` | Bypass |
| `m` | Mute out |
| `f` | Drop backlog |
| `q` | Quit |

Every key in the footer is clickable as well as typeable. The two that hold a
state — `Bypass` and `Mute out` — fill with amber while they are engaged, so
"am I still muted?" is answered by looking rather than by remembering. The
lit key is drawn from the pipeline's own state, not from what the dashboard
believes it asked for, so a toggle that failed to apply stays unlit.

**Bypass (`b`) has three effects, and all three matter** — describing only one
is how a user ends up with translated speech talking over the unmediated
conversation it was meant to replace:

1. The duck opens and stays open: you hear the remote party's own voice,
   unducked.
2. Your real microphone is linked straight into the virtual mic: they hear
   your own voice, untranslated.
3. Playout on both directions is suppressed: no translated speech plays over
   either of the above.

Recognition keeps running underneath bypass so the transcript stays
continuous, but nothing queued during bypass gets spoken afterward — toggling
`b` off does not replay a backlog of everything said while it was on, because
the queue is thrown away on the way in *and* on the way out. Both edges
matter: nothing upstream knows a playout is suppressed, so synthesis carries
on filling the queue the whole time bypass is on.

`m` (mute) stops sending your translated voice without leaving the call —
recognition and translation keep running, only your OUT playout is
suppressed, and unmuting does not play back what accumulated while muted, for
the same reason.

**Mute and bypass compose rather than overwrite each other.** Bypass
suppresses your outbound playout for its own reasons (effect 3 above), so
pressing `m` while bypassed changes what you come back *to*, not what bypass
is doing right now — and leaving bypass restores your mute instead of
silently clearing it. The `Mute out` key stays lit throughout, which is the
point: the state that survives a bypass is the one you are most likely to
forget you set.

`f` (drop backlog) clears every queued-but-not-yet-started utterance on both
directions, **and cuts the one in progress short too** — `Playout.flush()`
drops the in-flight utterance along with the queue. What you still hear
afterwards is only the audio already handed to `pw-cat`, which is past this
process's control and plays out regardless: experiment 2 measured **+299 ms**
of steady-state buffering at the 16 KiB pipe sidetap sets. Expect a short tail
after pressing it.

## Output

Ctrl-C stops the session and writes `transcripts/<session>.jsonl` and
`transcripts/<session>.md` — a bilingual transcript, both directions in
chronological order. The `.jsonl` is opened in append mode and flushed after
every final unit, so an unclean exit still leaves everything up to that
moment on disk; an utterance the listener did not fully hear is written too
rather than silently missing, marked in the Markdown with why:
`_(not spoken)_` when the lag cap or bypass discarded it,
`_(not spoken: synthesis stalled)_` when the producer went quiet before a
single chunk was played, and `_(cut short before the end)_` when it was
playing and stopped.

## Cost

The TUI's running total is a **spend estimate to catch a runaway session, not
an invoice.** Google's per-character prices render dynamically on its pricing
pages and third-party trackers disagree with each other, so the rates in
`sidetap/cost.py` are a starting point to be reconfirmed against Google's live
pricing before you rely on them for anything but a sanity check.

Computed from the constants actually in the code — 100 ms blocks,
`KEEPALIVE_S = 2.0`, both directions running, $0.016/minute for
Speech-to-Text:

| call | STT per hour |
|---|---|
| idle — nobody speaking | **~$0.10** |
| ordinary conversation | **~$1.10** |
| both parties talking continuously | **~$1.92** |

The middle row is not half of the bottom row. The silence gate passes a 500 ms
tail after every utterance so the recognition engine can finalise it, so
billed audio always runs somewhat above raw speech time — fed a pattern that
is 40% speech, the real `webrtcvad` gate (not an assumption) passes about 58%
of wall clock. Translation and synthesis add to this in proportion to words
actually spoken rather than to call length, and at v1's rates are small next
to STT for an ordinary call.

The idle figure is roughly 20x lower than the talking figure **because of the
silence gate** — which is exactly why `sidetap doctor` fails loudly rather
than degrading quietly when `webrtcvad` is missing: without it, every
direction streams its silence too, all the time, and the idle row becomes the
bottom row.

## Known limitations

These are real, current limitations found during development — not
aspirational TODOs.

- **The drop-backlog hotkey leaves a short tail.** It does cut the sentence in
  progress — that is what `Playout.flush()` does — but roughly 300 ms is
  already inside `pw-cat` and plays out regardless, so the silence is not
  immediate. See **Hotkeys** above.
- **Half-duplex cadence is required, not optional.** Full-replacement routing
  means there is no overlay to fall back on. `LocalAgreementSegmenter` (on by
  default; `--no-early-commit` restores the old behaviour) commits a
  monologue clause by clause instead of waiting for a single final, but it
  only acts on interim hypotheses and only once roughly 11 s of continuous
  speech has passed — so sustained speech from both parties without pausing
  still pushes the translation further and further behind rather than keeping
  pace, until the lag cap (default 20 s) starts dropping the oldest queued
  utterances.
- **Cloud Translation cannot be pinned to `europe-west3`.** Unlike
  Speech-to-Text, Translation only accepts `global` or `us-central1`
  (Experiment 3); `--mt-region` defaults to `global`. Translation LLM (the
  default `--mt-model`) also measured ~195 ms slower than NMT and occasionally
  over its own latency budget, in exchange for a real but modest quality gain
  that shows up on idiom rather than plain sentences — it falls back to NMT
  automatically on error, and `--mt-model general/nmt` switches it by hand.
- **Felt latency is dominated by ASR and MT, not synthesis.** The same wait
  for a finalised result that sets the cadence above also sets the latency
  floor: nothing downstream of recognition starts until Chirp 3 declares a
  result final. Synthesis itself is not the bottleneck —
  `DirectionPipeline._speak` already starts playout at time-to-first-chunk
  rather than the whole utterance (271 ms saved on a short sentence, 2109 ms
  on a long one, Experiment 4) — but that gain sits downstream of the wait
  above. `segment.py`'s `LocalAgreementSegmenter` now cuts that wait, but only
  where Chirp makes it possible: short utterances produce no interim results
  at all, so turn-taking is unchanged and the gain is confined to monologues,
  where a measured 29.3 s wait becomes a commit roughly every 5 s
  (`docs/experiments/06-interim-cadence.md`). `--no-early-commit` restores the
  old behaviour.
- **`aggressiveness = 2`** in the silence gate is applied identically to both
  directions, but it is tuned (per its own docstring) for a raw room
  microphone; the remote direction arrives already compressed, AGC'd and
  likely noise-suppressed by the far end, and whether it wants a different
  value has not been measured.
- **This is a v1 built and reviewed by one person against one setup.** It has
  not been run against a real call with a second human on the other end,
  only against fakes, fixtures and short local checks — see
  `docs/manual-smoke.md` for what that still leaves unverified.

## More detail

- `docs/superpowers/specs/` — the design.
- `docs/superpowers/plans/` — the implementation plan.
- `docs/experiments/` — the measurements this document draws its numbers from.
- `docs/manual-smoke.md` — a checklist of what the automated test suite
  cannot verify (it runs with no audio hardware, network or credentials).
- `docs/initial_research/` — the research spike that preceded this package.
  Reference material only, not part of the codebase.
- `CLAUDE.md` — architecture and the invariants worth knowing before changing
  `routing.py`, `asr.py` or `playout.py`.
