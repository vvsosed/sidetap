import pytest
from google.api_core import exceptions as gexc

from sidetap.translate import (
    NMT_MODEL,
    TRANSLATION_LLM_MODEL,
    GoogleTranslator,
    TranslateConfig,
    model_path,
    parent_path,
    short_code,
)


class FakeTranslationClient:
    def __init__(self, fail_models=(), text="привет", error=RuntimeError, fail_first=0):
        self.fail_models = set(fail_models)
        self.text = text
        # Exception type raised for a fail_models match, or for one of the
        # leading `fail_first` calls regardless of which model was asked for.
        self.error = error
        self.fail_first = fail_first
        self.requests = []

    def translate_text(self, request):
        self.requests.append(request)
        model_should_fail = any(request["model"].endswith(m) for m in self.fail_models)
        call_should_fail = len(self.requests) <= self.fail_first
        if model_should_fail or call_should_fail:
            raise self.error("model not available in this region")

        class Translation:
            translated_text = self.text

        class Response:
            translations = [Translation()]

        return Response()


def _config(**kwargs) -> TranslateConfig:
    base = dict(project_id="proj", region="europe-west3")
    base.update(kwargs)
    return TranslateConfig(**base)


def test_parent_path_is_project_and_location():
    assert parent_path("proj", "europe-west3") == "projects/proj/locations/europe-west3"


def test_model_path_hangs_off_the_parent():
    assert model_path("proj", "europe-west3", NMT_MODEL) == (
        "projects/proj/locations/europe-west3/models/general/nmt"
    )


@pytest.mark.parametrize(
    "code,expected",
    [
        ("en-US", "en"),
        ("ru-RU", "ru"),
        ("uk-UA", "uk"),
        ("ru", "ru"),
        # zh-TW is itself a distinct supported code (Traditional); bare "zh"
        # means Simplified, so this must NOT fold down like the others.
        ("zh-Hant-TW", "zh-TW"),
        ("zh-CN", "zh-CN"),
        # nb (Norwegian Bokmal) is not a supported code at all - a bare
        # strip would 400. The API wants "no".
        ("nb-NO", "no"),
        # fr-CA and pt-PT are distinct supported codes; stripping the region
        # would silently switch the variant (France <-> Canada/Brazil).
        ("fr-CA", "fr-CA"),
        ("pt-PT", "pt-PT"),
        ("pt-BR", "pt"),
        # Some tooling emits underscores instead of hyphens.
        ("ru_RU", "ru"),
    ],
)
def test_short_code_strips_the_region(code, expected):
    # Cloud Translation wants "ru", Speech-to-Text wants "ru-RU". Passing the
    # BCP-47 code straight through is a 400 for most languages - but not
    # zh-TW/fr-CA/pt-PT, which are themselves distinct supported codes and
    # must survive intact rather than being folded down.
    assert short_code(code) == expected


def test_translate_sends_short_codes_and_plain_text():
    client = FakeTranslationClient()
    translator = GoogleTranslator(_config(), client)
    assert translator.translate("hello", "en-US", "ru-RU") == "привет"

    request = client.requests[0]
    assert request["contents"] == ["hello"]
    assert request["source_language_code"] == "en"
    assert request["target_language_code"] == "ru"
    assert request["mime_type"] == "text/plain"


def test_the_preferred_model_is_used_first():
    client = FakeTranslationClient()
    GoogleTranslator(_config(), client).translate("hello", "en-US", "ru-RU")
    assert client.requests[0]["model"].endswith(TRANSLATION_LLM_MODEL)


def test_an_unavailable_model_falls_back_to_nmt():
    client = FakeTranslationClient(fail_models=[TRANSLATION_LLM_MODEL])
    translator = GoogleTranslator(_config(), client)
    assert translator.translate("hello", "en-US", "ru-RU") == "привет"
    assert client.requests[1]["model"].endswith(NMT_MODEL)


def test_the_fallback_is_sticky():
    """One probe, not one per utterance.

    Retrying a model that is not available in this region on every utterance
    would add its full round-trip to every single translation. A bare
    RuntimeError no longer sticks by design - only a PERMANENT_ERRORS type
    like NotFound means "this model isn't available here".
    """
    client = FakeTranslationClient(fail_models=[TRANSLATION_LLM_MODEL], error=gexc.NotFound)
    translator = GoogleTranslator(_config(), client)
    translator.translate("hello", "en-US", "ru-RU")
    translator.translate("again", "en-US", "ru-RU")

    models = [r["model"] for r in client.requests]
    assert models[0].endswith(TRANSLATION_LLM_MODEL)
    assert models[1].endswith(NMT_MODEL)
    assert models[2].endswith(NMT_MODEL)
    assert len(client.requests) == 3


def test_a_transient_failure_does_not_downgrade_the_session():
    # ServiceUnavailable, DeadlineExceeded and the like say nothing about
    # whether the model is available - only that this one call didn't land.
    # Downgrading the whole session on that would silently cost idiom
    # quality for every later utterance over one blip.
    client = FakeTranslationClient(error=gexc.ServiceUnavailable, fail_first=1)
    translator = GoogleTranslator(_config(), client)

    assert translator.translate("hello", "en-US", "ru-RU") == "привет"
    assert translator.translate("again", "en-US", "ru-RU") == "привет"

    models = [r["model"] for r in client.requests]
    assert models[0].endswith(TRANSLATION_LLM_MODEL)  # first attempt, fails
    assert models[1].endswith(NMT_MODEL)  # one-off fallback for that utterance
    assert models[2].endswith(TRANSLATION_LLM_MODEL)  # NOT stuck on NMT
    assert len(client.requests) == 3


def test_on_downgrade_fires_once_for_permanent_and_never_for_transient():
    permanent_client = FakeTranslationClient(fail_models=[TRANSLATION_LLM_MODEL], error=gexc.NotFound)
    permanent_downgrades = []
    permanent = GoogleTranslator(_config(), permanent_client, on_downgrade=permanent_downgrades.append)
    permanent.translate("hello", "en-US", "ru-RU")
    permanent.translate("again", "en-US", "ru-RU")
    assert permanent_downgrades == [NMT_MODEL]

    transient_client = FakeTranslationClient(error=gexc.ServiceUnavailable, fail_first=1)
    transient_downgrades = []
    transient = GoogleTranslator(_config(), transient_client, on_downgrade=transient_downgrades.append)
    transient.translate("hello", "en-US", "ru-RU")
    assert transient_downgrades == []


def test_nmt_failing_propagates():
    # Nothing left to fall back to; the pipeline's retry policy owns this.
    client = FakeTranslationClient(fail_models=[TRANSLATION_LLM_MODEL, NMT_MODEL])
    with pytest.raises(RuntimeError):
        GoogleTranslator(_config(), client).translate("hello", "en-US", "ru-RU")


def test_configuring_nmt_directly_skips_the_probe():
    client = FakeTranslationClient()
    translator = GoogleTranslator(_config(model=NMT_MODEL), client)
    translator.translate("hello", "en-US", "ru-RU")
    assert client.requests[0]["model"].endswith(NMT_MODEL)
    assert len(client.requests) == 1


def test_empty_text_is_not_sent():
    client = FakeTranslationClient()
    assert GoogleTranslator(_config(), client).translate("  ", "en-US", "ru-RU") == ""
    assert client.requests == []
