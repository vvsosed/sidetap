"""Google Cloud Text-to-Speech, Chirp 3 HD streaming synthesis.

One fixed voice per direction - no cloning, which would add ~600 ms to
time-to-first-audio.

`synthesize` is a true incremental generator: DirectionPipeline._speak appends
each chunk to a Playout utterance as it arrives, so speech starts at
time-to-first-chunk rather than at full synthesis time - 194 vs 465 ms on a
short utterance, 230 vs 2339 ms on a long one
(docs/experiments/04-tts-streaming.md).

That is safe because the first chunk is always 200 ms of audio, later chunks
are 240 ms every ~30 ms, and synthesis runs 4.7-7.1x faster than playback, so
simulated early playout never dropped below a 200 ms margin. The lag cap,
counting only bytes that have arrived, understates the backlog by at most a
fraction of an utterance.

The first call costs ~543 ms against a ~267 ms warm median, hence the warm-up
synthesis in Session.setup().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator

from .types import TTS_RATE

log = logging.getLogger(__name__)


def tts_endpoint(region: str) -> str:
    """Chirp 3 HD has no Frankfurt single-region, so the default is eu."""
    if region == "global":
        return "texttospeech.googleapis.com"
    return f"{region}-texttospeech.googleapis.com"


def voice_language(voice_name: str) -> str:
    """"ru-RU-Chirp3-HD-Kore" -> "ru-RU".

    The API wants both the voice name and its language code. Deriving one from
    the other removes a config in which they can disagree.
    """
    parts = voice_name.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else voice_name


# Measured, not documented: 2.1 and 4.0 are both rejected, although the API's
# own error message claims [0.25, 4.0].
MIN_SPEAKING_RATE = 0.25
MAX_SPEAKING_RATE = 2.0

# Here rather than beside run.py's voice table so cli.py can validate without
# importing run, which stays lazy so `doctor` and `devices` skip the pipeline.
#
# Two values, not SsmlVoiceGender's four: ListVoices reports every Chirp 3 HD
# voice as MALE or FEMALE (docs/experiments/05-voice-gender.md).
GENDERS = ("male", "female")

# gRPC sets no deadline of its own, so a hung streaming_synthesize would park
# _speak forever, its results queue growing and the tts marker still green.
# The same guard as asr.py's STREAM_TIMEOUT_S.
#
# A stuck-RPC backstop, not a latency control: the slowest measured synthesis
# took 2339 ms for 16.6 s of audio, and even at MIN_SPEAKING_RATE the worst
# case stays near 14 s. Playout gives up after 2 s without chunks anyway; this
# frees the worker thread and surfaces the failure.
STREAM_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class TtsConfig:
    region: str = "eu"
    # 1.0 is neutral and stays the default, because the useful value depends
    # on the language pair: Russian runs 1.23x as long as the English it came
    # from, so at 1.0 a continuous speaker's backlog never drains, while at
    # 1.3 it does. A more compact target language needs no adjustment.
    speaking_rate: float = 1.0


class ChirpSynthesizer:
    def __init__(self, config: TtsConfig, client):
        self._config = config
        self._client = client

    def synthesize(
        self, text: str, voice: str, speaking_rate: float | None = None
    ) -> Iterator[bytes]:
        text = text.strip()
        if not text:
            return

        from google.cloud import texttospeech as tts

        streaming_config = tts.StreamingSynthesizeConfig(
            voice=tts.VoiceSelectionParams(
                name=voice, language_code=voice_language(voice)
            ),
            streaming_audio_config=tts.StreamingAudioConfig(
                audio_encoding=tts.AudioEncoding.PCM,
                sample_rate_hertz=TTS_RATE,
                speaking_rate=(
                    self._config.speaking_rate
                    if speaking_rate is None
                    else speaking_rate
                ),
            ),
        )

        def requests():
            yield tts.StreamingSynthesizeRequest(streaming_config=streaming_config)
            yield tts.StreamingSynthesizeRequest(
                input=tts.StreamingSynthesisInput(text=text)
            )

        for response in self._client.streaming_synthesize(
            requests(), timeout=STREAM_TIMEOUT_S
        ):
            if response.audio_content:
                yield response.audio_content


def build_synthesizer(config: TtsConfig) -> ChirpSynthesizer:
    """Google's SDK is imported lazily so the suite needs no credentials."""
    from google.api_core.client_options import ClientOptions
    from google.cloud import texttospeech as tts

    client = tts.TextToSpeechClient(
        client_options=ClientOptions(api_endpoint=tts_endpoint(config.region))
    )
    return ChirpSynthesizer(config, client)
