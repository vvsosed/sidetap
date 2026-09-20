"""Google Cloud Text-to-Speech, Chirp 3: HD streaming synthesis.

One fixed voice per direction - no cloning, which costs roughly 600 ms of
time-to-first-audio for a v1 that does not need it.

`synthesize` is a true incremental generator and the pipeline exploits it:
DirectionPipeline._speak appends each chunk to a Playout utterance as it
arrives, so speech starts at time-to-first-chunk rather than at full
synthesis wall time. Measured (docs/experiments/04-tts-streaming.md), that
is the difference between 194 ms and 465 ms on a short utterance, and
between 230 ms and 2339 ms on a long one.

The shape that makes it safe: the first chunk is always 200 ms of audio,
every chunk after it is 240 ms arriving every ~30 ms, and production runs
4.7-7.1x faster than playback - so simulated early playout never dipped
below a 200 ms buffer margin across nine runs. Playout still waits for
START_BUFFER_S before starting, which costs ~30 ms and doubles that margin.

An earlier version of this docstring claimed early playout would force the
lag cap to estimate the backlog rather than measure it. It does not: at
those ratios, counting only bytes that have arrived understates the backlog
by a fraction of one utterance and self-corrects within about a second.

The first call after construction costs ~543 ms against a ~267 ms warm
median, which is why Session.setup() performs a throwaway synthesis while
the duck's loopback node is still registering.
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


# Measured against Chirp 3 HD streaming, not taken from the documentation:
# 2.1 and 4.0 are both rejected, even though the API's own error message says
# "ensure that speaking_rate is in the range [0.25, 4.0]". Validate against
# what the service does, not what it claims.
MIN_SPEAKING_RATE = 0.25
MAX_SPEAKING_RATE = 2.0

# gRPC sets no deadline of its own, so a hung streaming_synthesize call parks
# DirectionPipeline._speak inside `for chunk in chunks` forever: the results
# queue behind it grows unbounded, and the tts health marker stays green
# because Health is only set on the loop's exit paths, none of which run
# while parked. asr.py guards its own streaming call the identical way
# (STREAM_TIMEOUT_S there); this is that same guard for tts.py.
#
# 30s, not a tight fit to real synthesis time: the slowest run measured
# (docs/experiments/04-tts-streaming.md) was 2339 ms for 16.6 s of audio, so
# 30s is roughly 13x that worst case. It stays generous even at
# MIN_SPEAKING_RATE (0.25, the setting that produces the *longest* audio):
# scaling that same 16.6s utterance to a ~4x longer output (~66s of audio)
# and dividing by the slowest observed synthesis-to-audio ratio (4.7x, not
# the 7.1x this particular run hit) still lands at ~14s, under half the
# budget. This is a stuck-RPC backstop, not a latency control - playout's own
# STARVE_LIMIT_TICKS already gives up on an utterance after 2s of no chunks,
# so a synthesis still running at 30s has long since stopped being useful to
# anyone. The timeout exists to free the worker thread and surface the
# failure, not to salvage the sentence.
STREAM_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class TtsConfig:
    region: str = "eu"
    # 1.0 is neutral, and the default stays neutral because the useful value
    # depends on the language pair. Measured on real utterances, Russian takes
    # 1.23x as long to speak as the English it was translated from - so at 1.0
    # the output is structurally longer than the input, and a continuous
    # speaker builds a backlog no amount of waiting will drain. At 1.3 the same
    # Russian comes out at 0.86x the English and the backlog drains instead.
    # A pair whose target language is more compact needs no adjustment at all,
    # which is why guessing a global default would be wrong.
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
