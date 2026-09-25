"""Google Cloud Translation v3, one call per unit.

No batching: at utterance granularity it would only add lag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

# Needs no network or credentials to import, unlike the SDK client below, so
# it need not be lazy.
from google.api_core import exceptions as gexc

log = logging.getLogger(__name__)

# Better on conversational register and roughly cost-equivalent to NMT ($10 in
# + $10 out vs $20 per 1M characters), but with narrower region and language
# coverage, hence the fallback (docs/experiments/03-translation-llm.md).
TRANSLATION_LLM_MODEL = "general/translation-llm"
NMT_MODEL = "general/nmt"

# The SDK's own deadline for translate_text is 600 s, so a connection that goes
# silent mid-call would hold a direction for ten minutes, twice with the NMT
# retry, while nothing on screen changes. Calls measure 130-370 ms warm
# (docs/experiments/03-translation-llm.md); 5 s only ever cuts off a stall.
TRANSLATE_TIMEOUT_S = 5.0

# Only these mean "this model is not available here". Anything else is
# transient: fall back for this utterance only, so one blip does not silently
# cost idiom quality for the rest of the session.
PERMANENT_ERRORS = (
    gexc.InvalidArgument,
    gexc.NotFound,
    gexc.PermissionDenied,
    gexc.FailedPrecondition,
)


def parent_path(project_id: str, region: str) -> str:
    return f"projects/{project_id}/locations/{region}"


def model_path(project_id: str, region: str, model: str) -> str:
    return f"{parent_path(project_id, region)}/models/{model}"


# Verified against get_supported_languages on 2026-09-18 (196 codes).
# Bare primary subtags Cloud Translation rejects, mapped to the code it wants.
LANGUAGE_ALIASES = {"nb": "no", "nn": "no"}

# Regional variants Cloud Translation treats as distinct codes; stripping the
# region would silently switch variant (fr -> France, pt -> Brazil).
REGIONAL_VARIANTS = {"fr-CA", "pt-PT"}

# Script variants (ms-Arab, pa-Arab, mni-Mtei) are not handled: unlikely in a
# voice call, and whether their bare forms are supported is unverified.
# sr-Latn needs nothing: the API offers only "sr" (Cyrillic), which stripping
# the region already produces.


def short_code(language_code: str) -> str:
    """BCP-47 -> the code Cloud Translation expects.

    Speech-to-Text wants "ru-RU"; Translation wants "ru" and rejects most
    regioned forms with a 400. zh-TW, fr-CA and pt-PT are distinct supported
    codes, and folding them would silently switch variant (zh -> Simplified,
    fr -> France, pt -> Brazil), so they are kept. nb is not supported at
    all; the API wants "no".
    """
    parts = language_code.replace("_", "-").split("-")
    primary = parts[0].lower()
    rest = [part.lower() for part in parts[1:]]

    if primary == "zh":
        if any(tag in ("hant", "tw", "hk", "mo") for tag in rest):
            return "zh-TW"
        return "zh-CN"

    if rest:
        candidate = f"{primary}-{rest[0].upper()}"
        if candidate in REGIONAL_VARIANTS:
            return candidate

    return LANGUAGE_ALIASES.get(primary, primary)


@dataclass(frozen=True)
class TranslateConfig:
    project_id: str
    # NOT europe-west3: Cloud Translation accepts only "global" or
    # "us-central1". STT cannot use it either, for an unrelated reason (see
    # asr.py), which is why the regions are separate settings. See
    # docs/experiments/03-translation-llm.md.
    region: str = "global"
    model: str = TRANSLATION_LLM_MODEL


class GoogleTranslator:
    def __init__(
        self,
        config: TranslateConfig,
        client,
        on_downgrade: Callable[[str], None] | None = None,
    ):
        self._config = config
        self._client = client
        # Downgraded only once the preferred model is PERMANENTLY unavailable
        # (see PERMANENT_ERRORS); a transient error falls back for one
        # utterance without touching this.
        self._model = config.model
        # Fires once, with the new model name, on the sticky downgrade, so the
        # TUI can show it: the next successful NMT call still reports
        # mt=Health.OK.
        self._on_downgrade = on_downgrade

    def _request(self, text: str, src: str, tgt: str, model: str) -> dict:
        return {
            "parent": parent_path(self._config.project_id, self._config.region),
            "contents": [text],
            "mime_type": "text/plain",
            "source_language_code": short_code(src),
            "target_language_code": short_code(tgt),
            "model": model_path(self._config.project_id, self._config.region, model),
        }

    def translate(self, text: str, src: str, tgt: str) -> str:
        text = text.strip()
        if not text:
            return ""

        try:
            response = self._client.translate_text(
                request=self._request(text, src, tgt, self._model),
                timeout=TRANSLATE_TIMEOUT_S,
            )
        except Exception as exc:
            if self._model == NMT_MODEL:
                raise  # nothing left to fall back to

            if isinstance(exc, PERMANENT_ERRORS):
                log.warning(
                    "translation model %r unavailable (%s); falling back to "
                    "%s for the rest of this session",
                    self._model,
                    exc,
                    NMT_MODEL,
                )
                self._model = NMT_MODEL
                if self._on_downgrade is not None:
                    self._on_downgrade(NMT_MODEL)
            else:
                log.warning(
                    "translation model %r failed on this utterance (%s); "
                    "retrying with %s for this utterance only",
                    self._model,
                    exc,
                    NMT_MODEL,
                )

            response = self._client.translate_text(
                request=self._request(text, src, tgt, NMT_MODEL),
                timeout=TRANSLATE_TIMEOUT_S,
            )

        return response.translations[0].translated_text


def build_translator(
    config: TranslateConfig,
    on_downgrade: Callable[[str], None] | None = None,
) -> GoogleTranslator:
    """Google's SDK is imported lazily so the suite needs no credentials.

    `on_downgrade` fires on the sticky fallback to NMT; without it the
    downgrade is invisible outside this module.
    """
    from google.cloud import translate

    return GoogleTranslator(
        config, translate.TranslationServiceClient(), on_downgrade=on_downgrade
    )
