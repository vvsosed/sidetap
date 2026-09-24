"""Google Cloud Translation v3, one call per unit.

No batching: at utterance granularity it would only add lag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

# Pure Python, no network and no credentials required to import - unlike the
# SDK client below, so this one is fine unlazy. asr.py already does the same
# for the same reason.
from google.api_core import exceptions as gexc

log = logging.getLogger(__name__)

# Better on conversational register, and roughly cost-equivalent to NMT
# ($10 in + $10 out vs $20 per 1M characters). Its region and language-pair
# coverage is narrower, which is what the fallback below exists for - see
# docs/experiments/03-translation-llm.md.
TRANSLATION_LLM_MODEL = "general/translation-llm"
NMT_MODEL = "general/nmt"

# Only these mean "this model is not available here". Everything else -
# ServiceUnavailable, DeadlineExceeded, ResourceExhausted, a dropped
# connection - is transient: fall back for THIS utterance so the turn is not
# dropped, but do NOT downgrade the rest of the session. One blip would
# otherwise cost idiom quality for a whole conversation, invisibly.
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
# Bare primary subtags Cloud Translation does not accept as-is; map to the
# code it does.
LANGUAGE_ALIASES = {"nb": "no", "nn": "no"}

# Regional variants Cloud Translation treats as distinct codes rather than
# folding into the bare primary subtag - stripping the region here would
# silently switch to the wrong variant (fr -> France, pt -> Brazil).
REGIONAL_VARIANTS = {"fr-CA", "pt-PT"}

# Script-variant codes such as ms-Arab, pa-Arab and mni-Mtei are not handled:
# their primary subtags are unlikely to appear in a voice call, and whether
# the bare forms (ms, pa, mni) even exist as supported codes has not been
# verified. sr-Latn is not a supported code either way - the API only offers
# bare "sr" (Cyrillic), which is already what stripping the region produces,
# so it needs no special case.


def short_code(language_code: str) -> str:
    """BCP-47 -> the code Cloud Translation expects.

    Speech-to-Text wants "ru-RU"; Translation wants "ru" and rejects the
    regioned form with a 400 for most languages - but not all of them.
    zh-TW, fr-CA and pt-PT are themselves distinct supported codes, and
    folding them down to the bare primary subtag would silently change the
    variant (zh -> Simplified, fr -> France, pt -> Brazil) rather than
    erroring, so those are preserved. nb (Norwegian Bokmal) isn't a
    supported code at all and gets rejected outright; the API wants "no".
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
    # NOT europe-west3. Cloud Translation rejects it outright ("Must be
    # 'us-central1' or 'global'") - measured, not guessed. STT cannot use it
    # either, for a different reason: chirp_2 does not exist there (see
    # asr.py), so --region is europe-west4. Two services, two unrelated
    # answers, which is why these are separate settings rather than one
    # shared region. See docs/experiments/03-translation-llm.md and its
    # addendum.
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
        # Set once the preferred model has proved PERMANENTLY unavailable -
        # see PERMANENT_ERRORS. A transient error falls back for one
        # utterance without touching this, so a single blip doesn't cost
        # idiom quality for the rest of the session.
        self._model = config.model
        # Fires once, with the new model name, when the sticky downgrade
        # happens. Task 25 wires this to metrics so the TUI can show which
        # model is actually in use - without it the downgrade is invisible:
        # the next successful NMT call still reports mt=Health.OK.
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
                request=self._request(text, src, tgt, self._model)
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
                request=self._request(text, src, tgt, NMT_MODEL)
            )

        return response.translations[0].translated_text


def build_translator(
    config: TranslateConfig,
    on_downgrade: Callable[[str], None] | None = None,
) -> GoogleTranslator:
    """Google's SDK is imported lazily so the suite needs no credentials.

    `on_downgrade` fires when the sticky fallback to NMT happens, so the
    session can surface which model is actually in use. Without a destination
    the downgrade is undetectable from outside this module.
    """
    from google.cloud import translate

    return GoogleTranslator(
        config, translate.TranslationServiceClient(), on_downgrade=on_downgrade
    )
