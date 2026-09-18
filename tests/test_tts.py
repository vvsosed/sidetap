import pytest

from sidetap.tts import ChirpSynthesizer, TtsConfig, tts_endpoint, voice_language


class FakeTtsClient:
    def __init__(self, chunks=(b"\x01\x02", b"\x03\x04"), error=None):
        self.chunks = list(chunks)
        self.error = error
        self.requests = None

    def streaming_synthesize(self, requests):
        self.requests = list(requests)
        if self.error is not None:
            raise self.error
        for chunk in self.chunks:
            class Response:
                audio_content = chunk

            yield Response()


def _config(**kwargs) -> TtsConfig:
    base = dict(region="eu")
    base.update(kwargs)
    return TtsConfig(**base)


def test_endpoint_uses_the_eu_multi_region():
    # Frankfurt is not available as a TTS single-region; the spec pins TTS to
    # the eu multi-region while STT and Translation sit in europe-west3.
    assert tts_endpoint("eu") == "eu-texttospeech.googleapis.com"
    assert tts_endpoint("global") == "texttospeech.googleapis.com"


@pytest.mark.parametrize(
    "voice,expected",
    [
        ("ru-RU-Chirp3-HD-Kore", "ru-RU"),
        ("en-US-Chirp3-HD-Charon", "en-US"),
        ("uk-UA-Chirp3-HD-Aoede", "uk-UA"),
    ],
)
def test_voice_language_is_derived_from_the_voice_name(voice, expected):
    # The API needs both, and deriving avoids a config where they disagree.
    assert voice_language(voice) == expected


def test_the_first_request_is_config_and_the_second_is_text():
    client = FakeTtsClient()
    synth = ChirpSynthesizer(_config(), client)
    list(synth.synthesize("привет", "ru-RU-Chirp3-HD-Kore"))

    assert client.requests[0].streaming_config.voice.name == "ru-RU-Chirp3-HD-Kore"
    assert client.requests[0].streaming_config.voice.language_code == "ru-RU"
    assert client.requests[1].input.text == "привет"


def test_audio_chunks_are_yielded_in_order():
    client = FakeTtsClient(chunks=(b"\x01", b"\x02", b"\x03"))
    synth = ChirpSynthesizer(_config(), client)
    assert list(synth.synthesize("hi", "en-US-Chirp3-HD-Charon")) == [b"\x01", b"\x02", b"\x03"]


def test_empty_chunks_are_skipped():
    # The API can emit a trailing empty frame; writing it is harmless but
    # confuses playout's "did this carry speech" accounting.
    client = FakeTtsClient(chunks=(b"\x01", b"", b"\x02"))
    synth = ChirpSynthesizer(_config(), client)
    assert list(synth.synthesize("hi", "en-US-Chirp3-HD-Charon")) == [b"\x01", b"\x02"]


def test_empty_text_makes_no_call():
    client = FakeTtsClient()
    synth = ChirpSynthesizer(_config(), client)
    assert list(synth.synthesize("   ", "ru-RU-Chirp3-HD-Kore")) == []
    assert client.requests is None


def test_the_output_rate_matches_the_playout_contract():
    from sidetap.types import TTS_RATE

    client = FakeTtsClient()
    synth = ChirpSynthesizer(_config(), client)
    list(synth.synthesize("hi", "ru-RU-Chirp3-HD-Kore"))
    audio_config = client.requests[0].streaming_config.streaming_audio_config
    assert audio_config.sample_rate_hertz == TTS_RATE
