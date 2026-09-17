import random
import struct

from sidetap.types import BLOCK_BYTES
from sidetap.vad import SILENCE_TAIL_BLOCKS, SilenceGate, webrtc_detector

SPEECH = b"\x01" * BLOCK_BYTES
QUIET = b"\x00" * BLOCK_BYTES


def detector(pcm: bytes) -> bool:
    """Stand-in for webrtcvad: non-zero bytes count as speech."""
    return pcm != QUIET


def test_passes_everything_when_no_detector_is_available():
    gate = SilenceGate(detector=None)

    assert all(gate.allows(QUIET) for _ in range(100))


def test_passes_speech():
    gate = SilenceGate(detector=detector)

    assert gate.allows(SPEECH) is True


def test_passes_a_silence_tail_then_stops():
    gate = SilenceGate(detector=detector)
    gate.allows(SPEECH)

    passed = [gate.allows(QUIET) for _ in range(SILENCE_TAIL_BLOCKS + 3)]

    # The tail lets the engine finalise the utterance; after that we stop
    # paying to transmit dead air.
    assert passed[:SILENCE_TAIL_BLOCKS] == [True] * SILENCE_TAIL_BLOCKS
    assert passed[SILENCE_TAIL_BLOCKS:] == [False, False, False]


def test_tail_resets_when_speech_resumes():
    gate = SilenceGate(detector=detector)
    gate.allows(SPEECH)
    for _ in range(SILENCE_TAIL_BLOCKS + 2):
        gate.allows(QUIET)

    assert gate.allows(SPEECH) is True
    assert gate.allows(QUIET) is True  # tail counter went back to zero


def test_frame_size_divides_a_block_evenly():
    from sidetap.vad import FRAME_BYTES

    # webrtcvad only accepts 10, 20 or 30 ms frames, so a 100 ms block has to
    # split into whole frames or the last one is silently dropped.
    assert BLOCK_BYTES % FRAME_BYTES == 0


def noisy_block() -> bytes:
    """A deterministic block of loud noise, which a VAD should hear as speech."""
    rng = random.Random(0)
    samples = BLOCK_BYTES // 2
    return struct.pack(
        f"<{samples}h", *(rng.randint(-20000, 20000) for _ in range(samples))
    )


def test_real_detector_hears_speech_in_a_loud_block():
    # The stand-in detector used above never runs webrtc_detector's
    # frame-slicing loop. This does, and it has to be the positive case:
    # asserting only that silence is quiet would pass even if the loop
    # never ran, since any([]) is False.
    detect = webrtc_detector()
    assert detect is not None, "webrtcvad-wheels is a hard dependency"

    assert detect(noisy_block()) is True


def test_real_detector_reports_silence_as_quiet():
    # A FRESH detector on purpose: webrtcvad.Vad carries adaptive state
    # across calls, so reusing the instance from the test above could report
    # this silence as speech on hangover.
    detect = webrtc_detector()
    assert detect is not None

    assert detect(b"\x00" * BLOCK_BYTES) is False
