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

**Result.** _(fill in: per-model latency, output text, any error)_

**Consequence.** If Translation LLM errors or is unavailable, set
`TranslateConfig.model` default to `general/nmt` in Task 16 and note it in
`README.md`. If it works but is more than ~150 ms slower than NMT, keep it
anyway — the quality matters more at this granularity — but record the figure.
