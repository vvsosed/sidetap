import pytest

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
    def __init__(self, fail_models=(), text="привет"):
        self.fail_models = set(fail_models)
        self.text = text
        self.requests = []

    def translate_text(self, request):
        self.requests.append(request)
        if any(request["model"].endswith(m) for m in self.fail_models):
            raise RuntimeError("model not available in this region")

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
    [("en-US", "en"), ("ru-RU", "ru"), ("uk-UA", "uk"), ("ru", "ru")],
)
def test_short_code_strips_the_region(code, expected):
    # Cloud Translation wants "ru", Speech-to-Text wants "ru-RU". Passing the
    # BCP-47 code straight through is a 400.
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
    would add its full round-trip to every single translation.
    """
    client = FakeTranslationClient(fail_models=[TRANSLATION_LLM_MODEL])
    translator = GoogleTranslator(_config(), client)
    translator.translate("hello", "en-US", "ru-RU")
    translator.translate("again", "en-US", "ru-RU")

    models = [r["model"] for r in client.requests]
    assert models[0].endswith(TRANSLATION_LLM_MODEL)
    assert models[1].endswith(NMT_MODEL)
    assert models[2].endswith(NMT_MODEL)
    assert len(client.requests) == 3


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
