import pytest
from google.api_core import exceptions as gexc

from sidetap.tts import ChirpSynthesizer, TtsConfig, tts_endpoint, voice_language


class FakeTtsClient:
    def __init__(self, chunks=(b"\x01\x02", b"\x03\x04"), error=None):
        self.chunks = list(chunks)
        self.error = error
        self.requests = None
        self.timeout = None

    def streaming_synthesize(self, requests, timeout=None):
        self.requests = list(requests)
        self.timeout = timeout
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


def test_the_speaking_rate_reaches_the_streaming_config():
    """Dropped here, the flag would be silently inert.

    Nothing downstream would notice: audio still arrives, just at the wrong
    length, and the backlog it was meant to drain keeps growing.
    """
    sent = {}

    class Client:
        def streaming_synthesize(self, requests, timeout=None):
            first = next(iter(requests))
            sent["rate"] = first.streaming_config.streaming_audio_config.speaking_rate
            return iter(())

    synth = ChirpSynthesizer(TtsConfig(speaking_rate=1.3), Client())
    list(synth.synthesize("привет", "ru-RU-Chirp3-HD-Kore"))
    assert sent["rate"] == pytest.approx(1.3)


# --- deadline on the streaming call -----------------------------------------
#
# Without a deadline, a hung streaming_synthesize call parks
# DirectionPipeline._speak's `for chunk in chunks` loop forever: the direction's
# results queue backs up unbounded and the tts health marker stays green,
# because Health is only set on the loop's exit paths, none of which run while
# parked. asr.py already guards its own streaming call the identical way
# (STREAM_TIMEOUT_S there); this is tts.py's copy of that guard.


def test_a_deadline_is_passed_to_streaming_synthesize():
    from sidetap.tts import STREAM_TIMEOUT_S

    client = FakeTtsClient()
    synth = ChirpSynthesizer(_config(), client)
    list(synth.synthesize("hi", "en-US-Chirp3-HD-Charon"))

    assert client.timeout == STREAM_TIMEOUT_S


def test_a_deadline_exceeded_mid_stream_still_yields_the_chunks_already_received():
    """The exception must propagate out of the generator, not be swallowed -
    and whatever arrived before it must still come out first.

    This is what lets DirectionPipeline._speak keep audio already accepted by
    playout: it appends each chunk as it is yielded, so a generator that
    forwards the earlier chunk before raising is what makes "what had already
    arrived is still spoken" true up in the pipeline, rather than an
    assumption about a layer this test does not touch.
    """

    class Client:
        def streaming_synthesize(self, requests, timeout=None):
            class Response:
                audio_content = b"\x01\x02"

            yield Response()
            raise gexc.DeadlineExceeded("synthesis stalled")

    synth = ChirpSynthesizer(_config(), Client())
    chunks = synth.synthesize("hi", "en-US-Chirp3-HD-Charon")

    assert next(chunks) == b"\x01\x02"
    with pytest.raises(gexc.DeadlineExceeded):
        next(chunks)
