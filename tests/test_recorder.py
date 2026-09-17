from sidetap.recorder import build_argv, format_properties


def test_always_requests_the_audio_contract():
    argv = build_argv(node_name="sidetap.mic.abc", media_name="sidetap mic")

    # PipeWire does the resampling and downmixing for us. Nothing downstream
    # is allowed to assume any other format.
    assert "--rate" in argv and argv[argv.index("--rate") + 1] == "16000"
    assert "--channels" in argv and argv[argv.index("--channels") + 1] == "1"
    assert "--format" in argv and argv[argv.index("--format") + 1] == "s16"
    assert argv[-1] == "-"
    assert "--raw" in argv


def test_property_values_stay_quoted():
    # An unquoted space here splits the property in two and pw-record drops it.
    props = format_properties({"media.name": "sidetap remote"})

    assert props == '{ media.name="sidetap remote" }'


def test_media_name_with_a_space_survives_argv_construction():
    argv = build_argv(node_name="sidetap.remote.abc", media_name="sidetap remote")
    properties = argv[argv.index("--properties") + 1]

    assert 'media.name="sidetap remote"' in properties


def test_sink_monitor_capture_sets_capture_sink():
    argv = build_argv(
        node_name="sidetap.remote.abc",
        media_name="sidetap remote",
        target=1001,
        capture_sink=True,
    )
    properties = argv[argv.index("--properties") + 1]

    # The Linux equivalent of WASAPI loopback: attach to the sink's monitor
    # ports rather than expecting it to be a source.
    assert 'stream.capture.sink="true"' in properties
    assert argv[argv.index("--target") + 1] == "1001"


def test_app_tap_disables_autoconnect():
    argv = build_argv(
        node_name="sidetap.remote.abc",
        media_name="sidetap remote",
        autoconnect=False,
    )
    properties = argv[argv.index("--properties") + 1]

    # Otherwise WirePlumber helpfully links our capture node to the default
    # microphone and we record the wrong thing.
    assert 'node.autoconnect="false"' in properties
    assert "--target" not in argv


def test_latency_is_configurable():
    argv = build_argv(
        node_name="n", media_name="m", latency="250ms"
    )

    assert argv[argv.index("--latency") + 1] == "250ms"


import io

from sidetap.recorder import Recorder, RecorderSpec, read_blocks
from sidetap.types import BLOCK_BYTES
from tests.conftest import ChunkedBytesIO


def test_reads_whole_blocks():
    stream = io.BytesIO(b"\x01" * (BLOCK_BYTES * 3))

    blocks = list(read_blocks(stream))

    assert len(blocks) == 3
    assert all(len(b) == BLOCK_BYTES for b in blocks)


def test_reassembles_short_reads():
    # A real pipe hands back 700 bytes when you ask for 3200.
    stream = ChunkedBytesIO(b"\x01" * (BLOCK_BYTES * 2), max_read=700)

    blocks = list(read_blocks(stream))

    assert len(blocks) == 2
    assert all(len(b) == BLOCK_BYTES for b in blocks)


def test_drops_a_partial_trailing_block():
    stream = io.BytesIO(b"\x01" * (BLOCK_BYTES + 100))

    blocks = list(read_blocks(stream))

    # Downstream code is entitled to assume every chunk is exactly one block.
    assert len(blocks) == 1


def test_empty_stream_yields_nothing():
    assert list(read_blocks(io.BytesIO(b""))) == []


def test_recorder_spawns_with_its_own_argv(fake_launcher):
    recorder = Recorder(RecorderSpec(track="mic", target=1002), fake_launcher)

    recorder.start()

    assert len(fake_launcher.calls) == 1
    argv = fake_launcher.calls[0]
    assert argv[argv.index("--target") + 1] == "1002"
    assert recorder.node_name in argv[argv.index("--properties") + 1]


def test_recorder_node_names_are_unique():
    a = Recorder(RecorderSpec(track="mic"), None)
    b = Recorder(RecorderSpec(track="mic"), None)

    assert a.node_name != b.node_name
    assert a.node_name.startswith("sidetap.mic.")


def test_recorder_yields_blocks_from_the_process(fake_launcher):
    fake_launcher.script = b"\x02" * (BLOCK_BYTES * 2)
    recorder = Recorder(RecorderSpec(track="remote"), fake_launcher)
    recorder.start()

    assert len(list(recorder.blocks())) == 2


def test_recorder_reports_a_dead_process(fake_launcher):
    recorder = Recorder(RecorderSpec(track="mic"), fake_launcher)
    recorder.start()

    assert recorder.failure() is None

    fake_launcher.processes[0].die(returncode=1, stderr="no such target")

    # A silently dead pw-record means the track goes quiet for the rest of the
    # meeting while we keep claiming to record.
    assert recorder.failure() == "no such target"


def test_stop_terminates_a_running_process(fake_launcher):
    recorder = Recorder(RecorderSpec(track="mic"), fake_launcher)
    recorder.start()

    recorder.stop()

    assert fake_launcher.processes[0].terminated is True


def test_stop_is_a_no_op_once_the_process_has_exited(fake_launcher):
    recorder = Recorder(RecorderSpec(track="mic"), fake_launcher)
    recorder.start()
    fake_launcher.processes[0].die(returncode=1, stderr="gone")

    recorder.stop()

    # Already dead. Signalling again is pointless, and against a real process
    # group it could reach a recycled pid.
    assert fake_launcher.processes[0].terminated is False


def test_stop_before_start_does_not_raise(fake_launcher):
    recorder = Recorder(RecorderSpec(track="mic"), fake_launcher)

    recorder.stop()

    assert fake_launcher.processes == []
