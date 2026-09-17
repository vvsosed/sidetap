"""Every fake must satisfy the Protocol it stands in for.

runtime_checkable only checks method names, not signatures, so this is a
shallow guard. It still catches the common failure: a fake that drifts after
its Protocol gains a method.
"""

from sidetap.ports import (
    AudioSink,
    Clock,
    GraphSource,
    Linker,
    LoopbackFactory,
    ManagedProcess,
    ProcessLauncher,
    Recognizer,
    Synthesizer,
    Translator,
    Unlinker,
    VolumeControl,
    WritableProcess,
)
from tests.conftest import (
    FakeAudioSink,
    FakeClock,
    FakeGraphSource,
    FakeLauncher,
    FakeLinker,
    FakeLoopbackFactory,
    FakeProcess,
    FakeRecognizer,
    FakeSynthesizer,
    FakeTranslator,
    FakeVolumeControl,
    FakeWritableProcess,
)

# NOTE: the Segmenter protocol check lives in Task 15, which creates the only
# implementation. Importing sidetap.segment here would break collection of the
# whole suite for the eleven tasks in between.


def test_fakes_satisfy_their_protocols(idle_graph):
    assert isinstance(FakeGraphSource(idle_graph), GraphSource)
    assert isinstance(FakeLauncher(), ProcessLauncher)
    assert isinstance(FakeLinker(), Linker)
    assert isinstance(FakeLinker(), Unlinker)
    assert isinstance(FakeClock(), Clock)
    assert isinstance(FakeVolumeControl(), VolumeControl)
    assert isinstance(FakeLoopbackFactory(), LoopbackFactory)
    assert isinstance(FakeAudioSink(), AudioSink)
    assert isinstance(FakeTranslator(), Translator)
    assert isinstance(FakeSynthesizer(), Synthesizer)
    assert isinstance(FakeRecognizer([]), Recognizer)


def test_fake_processes_satisfy_their_protocols():
    import io

    assert isinstance(FakeProcess(io.BytesIO(b"")), ManagedProcess)
    assert isinstance(FakeWritableProcess(), WritableProcess)
