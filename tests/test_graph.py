import json
from pathlib import Path

import pytest

from sidetap.graph import PLAYBACK_STREAM, SINK, SOURCE, parse_graph

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def idle():
    return parse_graph((FIXTURES / "pw_dump_idle.json").read_text())


@pytest.fixture
def zoom():
    return parse_graph((FIXTURES / "pw_dump_zoom_active.json").read_text())


def test_nodes_are_identified_by_serial_not_id(idle):
    sink = idle.node_by_name("alsa_output.pci-0000_00_1f.3.analog-stereo")

    # Ids get recycled when nodes come and go; serials do not. Anything that
    # targets a node must use the serial.
    assert sink.id == 40
    assert sink.serial == 1001


def test_nodes_without_a_media_class_are_skipped(idle):
    assert idle.node_by_name("node-without-media-class") is None


def test_filters_by_media_class(idle, zoom):
    assert [n.serial for n in idle.by_class(SINK)] == [1001]
    assert [n.serial for n in zoom.by_class(PLAYBACK_STREAM)] == [1204, 1250]


def test_reads_defaults_from_metadata(idle):
    assert idle.default_sink == "alsa_output.pci-0000_00_1f.3.analog-stereo"
    assert idle.default_source == "alsa_input.pci-0000_00_1f.3.analog-stereo"


def test_find_matches_on_substring_of_binary(zoom):
    node = zoom.find("zoom", PLAYBACK_STREAM)

    assert node.serial == 1204
    assert node.label == "ZOOM VoiceEngine"


def test_find_prefers_an_exact_node_name(idle):
    node = idle.find("alsa_input.pci-0000_00_1f.3.analog-stereo", SOURCE)

    assert node.serial == 1002


def test_find_returns_none_when_nothing_matches(idle):
    assert idle.find("obs-studio", PLAYBACK_STREAM) is None


def test_ports_are_filtered_by_node_and_direction(zoom):
    outs = zoom.ports_of(55, "out")

    assert [p.name for p in outs] == ["output_FL", "output_FR"]
    assert zoom.ports_of(55, "in") == ()


def test_matching_is_case_insensitive(zoom):
    assert zoom.find("ZOOM", PLAYBACK_STREAM) is not None
    assert zoom.find("Spotify", PLAYBACK_STREAM).app_binary == "spotify"


def test_serial_falls_back_to_id_with_a_warning(caplog):
    # Degrading is better than crashing, but it must not happen silently:
    # a recycled id can point pw-record at the wrong stream mid-meeting.
    dump = json.dumps(
        [
            {
                "id": 77,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "props": {
                        "media.class": "Audio/Sink",
                        "node.name": "sink-without-serial",
                    }
                },
            }
        ]
    )

    graph = parse_graph(dump)

    assert graph.node_by_name("sink-without-serial").serial == 77
    assert "no object.serial" in caplog.text


def test_empty_dump_yields_an_empty_graph():
    graph = parse_graph("[]")

    assert graph.nodes == ()
    assert graph.ports == ()
    assert graph.default_sink is None
    assert graph.default_source is None


def test_parses_a_real_pw_dump():
    """Guards against pw-dump's schema differing from our hand-written fixtures.

    Every other fixture here was written by hand from the same assumptions the
    parser was written from, so they cannot catch a wrong assumption - they
    share it. This one is a real capture (scrubbed: username, hostname,
    machine-id, pids, device serials and card names are replaced, but every
    object, key and type is intact), taken with an application streaming audio
    so the Stream/Output/Audio case is covered too.
    """
    graph = parse_graph((FIXTURES / "pw_dump_real.json").read_text())

    assert graph.by_class(SINK), "expected at least one sink"
    assert graph.by_class(SOURCE), "expected at least one source"
    assert graph.default_sink is not None
    assert graph.default_source is not None
    assert all(n.serial for n in graph.nodes)
    assert all(p.direction in ("in", "out") for p in graph.ports)
    # The reason --app works at all: an application's audio stream is a node
    # with this media.class, and it only exists while the app is playing.
    assert graph.by_class(PLAYBACK_STREAM), "expected an application stream"


def test_the_real_fixture_carries_no_identifying_data():
    """The fixture is committed to a public repo; a mis-scrub is permanent."""
    text = (FIXTURES / "pw_dump_real.json").read_text()

    assert "/home/" not in text
    for key in (
        "application.process.user",
        "application.process.host",
        "application.process.machine-id",
        "device.serial",
        "pipewire.sec.pid",
        "user-name",
        "host-name",
    ):
        for line in text.splitlines():
            if f'"{key}"' in line:
                assert any(
                    token in line
                    for token in ('"user"', '"host"', "0000", "redacted", ": 1000")
                ), f"{key} looks unscrubbed: {line.strip()}"


def test_links_are_parsed_from_a_real_dump():
    """The router breaks and restores these, so they must come from pw-dump."""
    graph = parse_graph((FIXTURES / "pw_dump_real.json").read_text())
    assert len(graph.links) == 2
    link = next(l for l in graph.links if l.id == 93)
    assert (link.output_node, link.output_port, link.input_node, link.input_port) == (
        95, 91, 62, 56,
    )
    assert link.serial == 2138


def test_a_link_with_its_endpoints_only_in_props_is_still_parsed():
    dump = json.dumps([
        {"id": 7, "type": "PipeWire:Interface:Link",
         "info": {"props": {"link.output.node": 1, "link.output.port": 2,
                            "link.input.node": 3, "link.input.port": 4,
                            "object.serial": 70}}},
        {"id": 8, "type": "PipeWire:Interface:Link", "info": {"props": {}}},
    ])
    (link,) = parse_graph(dump).links
    ends = (link.output_node, link.output_port, link.input_node, link.input_port)
    assert ends == (1, 2, 3, 4)
    assert link.serial == 70


def test_sidetaps_own_nodes_are_recognised_by_name():
    graph = parse_graph((FIXTURES / "pw_dump_routing.json").read_text())
    own = {n.name for n in graph.nodes if n.is_sidetap}
    assert own == {"sidetap_duck", "sidetap_tts_sink", "sidetap_virtmic"}
