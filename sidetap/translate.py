"""Google Cloud Translation v3, one call per unit.

No batching: at utterance granularity it would only add lag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Better on conversational register, and roughly cost-equivalent to NMT
# ($10 in + $10 out vs $20 per 1M characters). Its region and language-pair
# coverage is narrower, which is what the fallback below exists for - see
# docs/experiments/03-translation-llm.md.
TRANSLATION_LLM_MODEL = "general/translation-llm"
NMT_MODEL = "general/nmt"


def parent_path(project_id: str, region: str) -> str:
    return f"projects/{project_id}/locations/{region}"


def model_path(project_id: str, region: str, model: str) -> str:
    return f"{parent_path(project_id, region)}/models/{model}"


def short_code(language_code: str) -> str:
    """BCP-47 -> the bare code Cloud Translation expects.

    Speech-to-Text wants "ru-RU"; Translation wants "ru" and rejects the
    regioned form with a 400. One config value feeds both, so the conversion
    lives here rather than in every caller.
    """
    return language_code.split("-")[0].lower()


@dataclass(frozen=True)
class TranslateConfig:
    project_id: str
    # NOT europe-west3. Cloud Translation rejects it outright ("Must be
    # 'us-central1' or 'global'") - measured, not guessed. STT does accept
    # europe-west3, which is why these are separate settings rather than one
    # shared region. See docs/experiments/03-translation-llm.md.
    region: str = "global"
    model: str = TRANSLATION_LLM_MODEL


class GoogleTranslator:
    def __init__(self, config: TranslateConfig, client):
        self._config = config
        self._client = client
        # Set once the preferred model has proved unavailable. Retrying it per
        # utterance would add a full failed round-trip to every translation.
        self._model = config.model

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
                request=self._request(text, src, tgt, self._model)
            )
        except Exception as exc:
            if self._model == NMT_MODEL:
                raise  # nothing left to fall back to
            log.warning(
                "translation model %r unavailable (%s); falling back to %s for "
                "the rest of this session",
                self._model,
                exc,
                NMT_MODEL,
            )
            self._model = NMT_MODEL
            response = self._client.translate_text(
                request=self._request(text, src, tgt, self._model)
            )

        return response.translations[0].translated_text


def build_translator(config: TranslateConfig) -> GoogleTranslator:
    """Google's SDK is imported lazily so the suite needs no credentials."""
    from google.cloud import translate

    return GoogleTranslator(config, translate.TranslationServiceClient())
