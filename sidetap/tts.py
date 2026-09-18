"""Google Cloud Text-to-Speech, Chirp 3: HD streaming synthesis.

One fixed voice per direction - no cloning, which costs roughly 600 ms of
time-to-first-audio for a v1 that does not need it.

`synthesize` is a true incremental generator: 6.2 s of Russian arrives as 27
separate chunks, the first within 186-267 ms warm (measured - see
docs/experiments/04-tts-streaming.md). **The pipeline does not currently
exploit that.** DirectionPipeline._speak does `b"".join(synthesize(...))`,
because Playout's lag cap needs an utterance's duration up front to measure
backlog in seconds. So the latency that actually applies is full synthesis
wall time - 637 ms for 3.84 s of audio, 1390 ms for 6.2 s - not
time-to-first-chunk.

Do not "fix" this docstring by claiming early playout. Fix the pipeline, and
accept that the backlog becomes estimated rather than known; the trade-off is
written up in the spec's latency section.

The first call after construction costs ~543 ms against a ~267 ms warm median,
which is why Session.setup() performs a throwaway synthesis while the audio
graph is being rewired.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator

from .types import TTS_RATE

log = logging.getLogger(__name__)


def tts_endpoint(region: str) -> str:
    """Chirp 3 HD has no Frankfurt single-region; the spec pins it to eu."""
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


@dataclass(frozen=True)
class TtsConfig:
    region: str = "eu"


class ChirpSynthesizer:
    def __init__(self, config: TtsConfig, client):
        self._config = config
        self._client = client

    def synthesize(self, text: str, voice: str) -> Iterator[bytes]:
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
            ),
        )

        def requests():
            yield tts.StreamingSynthesizeRequest(streaming_config=streaming_config)
            yield tts.StreamingSynthesizeRequest(
                input=tts.StreamingSynthesisInput(text=text)
            )

        for response in self._client.streaming_synthesize(requests()):
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
