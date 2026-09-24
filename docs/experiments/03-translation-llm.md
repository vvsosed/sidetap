# Experiment 3 — Translation LLM coverage and latency

**Question.** Is the Translation LLM model available for the target language
pair in `europe-west3`, and how does its latency compare to NMT?

**Why it matters.** The spec prefers Translation LLM for conversational
register at rough cost-parity, but its region and language-pair coverage is
narrower than NMT's and is not confirmed for EN<->RU. `translate.py` treats the
model as a config value with NMT fallback, so this decides a default, not a
design.

**Method.** One representative utterance through both models in
`europe-west3`, timed.

**How to run.** Requires live GCP credentials, and costs money.

    export GOOGLE_CLOUD_PROJECT=your-project
    uv run python - <<'PY'
    import os, time
    from google.cloud import translate

    client = translate.TranslationServiceClient()
    parent = f"projects/{os.environ['GOOGLE_CLOUD_PROJECT']}/locations/europe-west3"
    text = "Could we move the deployment to Thursday? I want more time to test."

    for model in ("general/translation-llm", "general/nmt"):
        try:
            start = time.monotonic()
            response = client.translate_text(
                request={
                    "parent": parent,
                    "contents": [text],
                    "mime_type": "text/plain",
                    "source_language_code": "en",
                    "target_language_code": "ru",
                    "model": f"{parent}/models/{model}",
                }
            )
            elapsed = (time.monotonic() - start) * 1000
            print(f"{model}: {elapsed:.0f} ms -> {response.translations[0].translated_text}")
        except Exception as exc:
            print(f"{model}: FAILED -- {type(exc).__name__}: {exc}")
    PY

**Result.** Run 2026-09-18 from central Europe, project `<project-id>`.

**First finding, and the important one: `europe-west3` is not a valid location
for Cloud Translation at all.**

```
400 Invalid 'parent'.; Invalid location name. Unsupported location
'europe-west3'. Must be 'us-central1' or 'global'.
```

Both models, every language pair. The spec's "pin everything to Frankfurt"
plan is therefore impossible for this API — not slow, not degraded, rejected.
Speech-to-Text v2 was checked separately and *does* accept `europe-west3`
(along with `eu`, `us-central1` and `global`), so only Translation moves.

**Second: Translation LLM is available for every pair tested**, including
Ukrainian, which the spec had not asked about.

Medians of three warm calls each, after one warm-up:

| pair | model | `global` | `us-central1` |
|---|---|---|---|
| en→ru | translation-llm | 367 ms | 333 ms |
| en→ru | nmt | 286 ms | **136 ms** |
| ru→en | translation-llm | 358 ms | 238 ms |
| ru→en | nmt | 294 ms | **170 ms** |
| en→uk | translation-llm | 335 ms | 331 ms |
| en→uk | nmt | **134 ms** | 178 ms |

**Third: NMT is roughly 2x faster, and Translation LLM overruns the spec's
budget.** The spec allots MT 100-300 ms. NMT lands at 134-178 ms in
`us-central1`, comfortably inside. Translation LLM lands at 238-333 ms —
at or past the ceiling, about 195 ms slower than NMT on the same pair.

**Fourth: the quality difference is real but modest, and shows up on idiom
rather than on plain sentences.** ru→en is the clearest case:

- input: "Давай перенесём созвон на завтра, сегодня я не успеваю."
- llm: "Let's reschedule the call for tomorrow; I won't be able to fit it in today."
- nmt: "Let's reschedule the call for tomorrow, I don't have time today."

"не успеваю" is "I won't manage it in time", not "I don't have time" — the LLM
catches that, NMT flattens it. On the plainer en→ru sentence the two are
near-indistinguishable ("Можно ли перенести" vs "Можем ли мы перенести").

**Consequence.**

1. `TranslateConfig.region` moves off `europe-west3` — it cannot work there.
   Default `global`; `us-central1` measured marginally faster and more
   consistent and is one flag away. STT stays on `europe-west3`, so the two
   services now need **separate** region settings rather than one shared
   `--region`.
2. Translation LLM stays the default, per this document's own pre-committed
   rule ("if it works but is more than ~150 ms slower than NMT, keep it
   anyway — the quality matters more at this granularity — but record the
   figure"). The figure is ~195 ms, and it is ~13% of the 1.5 s glass-to-glass
   target. `--mt-model general/nmt` switches it, and the runtime fallback
   still covers an outage.
3. Ukrainian works on both models, so `--their-lang uk-UA` is viable without
   further work.

**Consequence.** If Translation LLM errors or is unavailable, set
`TranslateConfig.model` default to `general/nmt` in Task 16 and note it in
`README.md`. If it works but is more than ~150 ms slower than NMT, keep it
anyway — the quality matters more at this granularity — but record the figure.

## Addendum, 2026-09-24 — "STT stays on `europe-west3`" did not survive

Consequence 1 above ends "STT stays on `europe-west3`", and the finding it
rests on — that Speech-to-Text v2 *does* accept `europe-west3` — was true of
the **API location** when it was measured, and is still true of the location.
It stopped being true of sidetap three days later, and the sentence has been
quietly wrong ever since.

What changed is the model, not the region. Measured against the live API on
2026-09-18 and recorded in `sidetap/asr.py`'s module docstring:

    chirp_3  any region      -> 403 "no longer generally available"
    chirp_2  europe-west3    -> 400 "does not exist in this location"
    chirp_2  europe-west4    -> works, and is the closest region that does
    long     europe-west3    -> works for en-US, but 400 for ru-RU

Google withdrew Chirp 3 from general availability after this experiment ran.
The replacement, `chirp_2`, is not served from Frankfurt at all, so `--region`
defaults to `europe-west4` and the "STT stays on `europe-west3`" half of
consequence 1 is void. What consequence 1 was actually *for* — that the two
services need separate region settings — survives intact, and is now true for
two unrelated reasons rather than one.

This is worth stating rather than editing out, because the wrong half
propagated: it reached `CLAUDE.md` as "`europe-west3` works for Speech-to-Text"
and `translate.py` as "STT does accept europe-west3", and both read as
permission to do the one thing `asr.py` warns against. Frankfurt is the
tempting mistake — it is nearest, and `long` works there for `en-US` before
returning 400 for `ru-RU`, so it fails only once audio is already flowing.
Both have been corrected.
