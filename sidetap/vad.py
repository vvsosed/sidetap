"""Voice activity gating.

Silence costs money on a metered engine, but cutting it off entirely stops the
engine from finalising utterances. We pass speech plus a short tail.
"""

from __future__ import annotations

import logging
from typing import Callable

from .types import TARGET_RATE

log = logging.getLogger(__name__)

# webrtcvad accepts 10, 20 or 30 ms frames only.
FRAME_MS = 20
FRAME_BYTES = TARGET_RATE * 2 * FRAME_MS // 1000
SILENCE_TAIL_BLOCKS = 5

SpeechDetector = Callable[[bytes], bool]


def webrtc_detector(aggressiveness: int = 2) -> SpeechDetector | None:
    """Real detector, or None when webrtcvad is unavailable.

    `aggressiveness` runs 0-3, higher filtering more non-speech. 2 is a
    middle setting chosen for meeting audio, where fans and keyboards are
    common but clipping a quiet speaker costs more than a little extra
    streamed silence. It is a starting point, not a measured optimum.

    Note the returned closure is stateful: webrtcvad adapts to the noise
    floor across calls, so give each track its own detector rather than
    sharing one.
    """
    try:
        import webrtcvad
    except ImportError:
        log.warning(
            "webrtcvad not installed - silence will be streamed too, which "
            "costs more. Run: uv sync"
        )
        return None

    vad = webrtcvad.Vad(aggressiveness)

    def detect(pcm: bytes) -> bool:
        return any(
            vad.is_speech(pcm[i : i + FRAME_BYTES], TARGET_RATE)
            for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)
        )

    return detect


class SilenceGate:
    def __init__(
        self,
        detector: SpeechDetector | None,
        tail_blocks: int = SILENCE_TAIL_BLOCKS,
    ):
        self._detect = detector
        self._tail_blocks = tail_blocks
        self._silence_run = 0

    def allows(self, pcm: bytes) -> bool:
        if self._detect is None:
            return True
        if self._detect(pcm):
            self._silence_run = 0
            return True
        self._silence_run += 1
        # Past the tail we send nothing. Keeping the stream alive through the
        # silence is EngineWorker's job (google.py:KEEPALIVE_S), not the
        # gate's - blocks stop arriving entirely when the tapped node goes
        # away, and the gate never sees that.
        return self._silence_run <= self._tail_blocks
