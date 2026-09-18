import json

from sidetap.ports import LinkResult
from sidetap.routing import (
    DUCK_NODE,
    VIRTMIC_CONFIG,
    VIRTMIC_SINK,
    VIRTMIC_SOURCE,
    Journal,
    LinkRef,
    Router,
    duck_loopback_spec,
    resolve,
)
from tests.conftest import FakeGraphSource, FakeLinker, FakeLoopbackFactory, load_graph


def test_the_duck_loopback_presents_a_sink_we_can_route_into():
    spec = duck_loopback_spec(target_sink="alsa_output.pci-0000_00_1f.3.analog-stereo")
    capture = dict(spec.capture_props)
    playback = dict(spec.playback_props)
    assert capture["node.name"] == DUCK_NODE
    assert capture["media.class"] == "Audio/Sink"
    # The playback side must land on the real speakers, or the user hears
    # nothing at all once the app is re-routed.
    assert playback["target.object"] == "alsa_output.pci-0000_00_1f.3.analog-stereo"


def test_link_refs_round_trip_through_json():
    ref = LinkRef(src_serial=10, src_port="output_FL", dst_serial=20, dst_port="playback_FL")
    assert LinkRef.from_dict(json.loads(json.dumps(ref.to_dict()))) == ref


def test_journal_round_trips_through_a_file(tmp_path):
    path = tmp_path / "journal.json"
    journal = Journal(
        broken=(LinkRef(1, "a", 2, "b"),),
        made=(LinkRef(1, "a", 3, "c"),),
    )
    journal.save(path)
    assert Journal.load(path) == journal


def test_loading_a_missing_journal_gives_an_empty_one(tmp_path):
    assert Journal.load(tmp_path / "nope.json") == Journal()


def test_loading_a_corrupt_journal_gives_an_empty_one(tmp_path):
    # A half-written file from a kill -9 must not stop the next run.
    path = tmp_path / "journal.json"
    path.write_text("{ not json")
    assert Journal.load(path) == Journal()


def test_resolve_finds_port_ids_by_serial_and_name(routing_graph):
    node = routing_graph.by_class("Stream/Output/Audio")[0]
    port = routing_graph.ports_of(node.id, "out")[0]
    ref = LinkRef(
        src_serial=node.serial, src_port=port.name, dst_serial=node.serial, dst_port=port.name
    )
    assert resolve(routing_graph, ref) == (port.id, port.id)


def test_resolve_returns_none_when_the_node_is_gone(routing_graph):
    # Serials are never recycled, so a missing one means the stream ended.
    ref = LinkRef(src_serial=999999, src_port="x", dst_serial=1, dst_port="y")
    assert resolve(routing_graph, ref) is None


def test_engage_journals_before_it_touches_the_graph(tmp_path, routing_graph):
    """The journal must be durable before the unlink, not after.

    A crash in between is exactly the case it exists for.
    """
    path = tmp_path / "journal.json"
    writes = []

    class WatchingLinker(FakeLinker):
        def unlink(self, src, dst):
            writes.append(("unlink", path.exists()))
            return super().unlink(src, dst)

    linker = WatchingLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=path,
    )
    router.engage(app_pattern="zoom", capture_node_name=None)

    assert writes, "no unlink happened at all"
    assert all(existed for _, existed in writes)


def test_engage_creates_the_duck_loopback(tmp_path, routing_graph):
    loopbacks = FakeLoopbackFactory()
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=loopbacks,
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom", capture_node_name=None)
    assert len(loopbacks.specs) == 1


def test_restore_terminates_the_duck_loopback(tmp_path, routing_graph):
    """A leaked pw-loopback is a leaked duck node - restore() must kill it.

    Not covered by any other test here: none of them inspect the loopback
    process restore() is handed, only the links it produces.
    """
    loopbacks = FakeLoopbackFactory()
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=loopbacks,
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom", capture_node_name=None)
    router.restore()
    assert loopbacks.processes[0].terminated


def test_restore_relinks_what_was_broken_and_breaks_what_was_made(tmp_path, routing_graph):
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom", capture_node_name=None)
    made = len(linker.links)
    broken = len(linker.unlinks)

    linker.links.clear()
    linker.unlinks.clear()
    router.restore()

    assert len(linker.links) == broken
    assert len(linker.unlinks) == made


def test_restore_clears_the_journal(tmp_path, routing_graph):
    path = tmp_path / "j.json"
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=path,
    )
    router.engage(app_pattern="zoom", capture_node_name=None)
    router.restore()
    assert Journal.load(path) == Journal()


def test_restore_is_idempotent(tmp_path, routing_graph):
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom", capture_node_name=None)
    router.restore()
    linker.links.clear()
    router.restore()
    assert linker.links == []


def test_repair_replays_a_journal_from_a_dead_session(tmp_path, routing_graph):
    """kill -9 leaves the graph broken and the journal behind."""
    path = tmp_path / "j.json"
    node = routing_graph.by_class("Stream/Output/Audio")[0]
    port = routing_graph.ports_of(node.id, "out")[0]
    Journal(
        broken=(LinkRef(node.serial, port.name, node.serial, port.name),), made=()
    ).save(path)

    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=path,
    )
    assert router.repair() is True
    assert linker.links == [(port.id, port.id)]
    assert Journal.load(path) == Journal()


def test_repair_with_no_journal_reports_nothing_to_do(tmp_path, routing_graph):
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "absent.json",
    )
    assert router.repair() is False


def test_a_stale_journal_entry_is_skipped_not_fatal(tmp_path, routing_graph):
    # The application exited before repair ran; its serial is gone forever.
    path = tmp_path / "j.json"
    Journal(broken=(LinkRef(999999, "a", 999998, "b"),), made=()).save(path)
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=path,
    )
    assert router.repair() is True
    assert linker.links == []
    assert Journal.load(path) == Journal()


def test_engage_pairs_channels_rather_than_crossing_them(tmp_path, routing_graph):
    """FL to FL, FR to FR - the way WirePlumber linked them in the first place.

    Journalling a cross-product would make restore CREATE links that never
    existed, leaving the graph worse than sidetap found it.
    """
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom", capture_node_name=None)

    journal = Journal.load(tmp_path / "j.json")
    assert [(r.src_port, r.dst_port) for r in journal.broken] == [
        ("output_FL", "playback_FL"),
        ("output_FR", "playback_FR"),
    ]
    assert [(r.src_port, r.dst_port) for r in journal.made] == [
        ("output_FL", "playback_FL"),
        ("output_FR", "playback_FR"),
    ]


def test_poll_routes_a_stream_that_appeared_after_engage(tmp_path, routing_graph):
    """A restarted stream is otherwise autoconnected to the speakers.

    Unducked and unjournalled, so the original plays over the translation for
    the rest of the call and restore() cannot put it back.
    """
    from dataclasses import replace

    from sidetap.graph import PLAYBACK_STREAM, PwNode, PwPort

    empty = replace(
        routing_graph,
        nodes=tuple(n for n in routing_graph.nodes if n.media_class != PLAYBACK_STREAM),
    )
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(empty, routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom", capture_node_name=None)
    assert linker.links == [], "nothing was playing yet"

    assert router.poll_once() == 1
    assert linker.links, "the stream that appeared later was never routed"
    assert Journal.load(tmp_path / "j.json").made


def test_poll_does_not_reroute_the_same_stream(tmp_path, routing_graph):
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom", capture_node_name=None)
    before = len(linker.links)
    assert router.poll_once() == 0
    assert len(linker.links) == before


def test_poll_before_engage_does_nothing(tmp_path, routing_graph):
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    assert router.poll_once() == 0
    assert linker.links == []


def test_engage_records_both_the_ducks_serial_and_its_id(tmp_path, routing_graph):
    """They are different numbers and are used for different things.

    The journal needs the serial, because ids are recycled over time. wpctl
    needs the id, because that is what it resolves against. Feeding a serial
    to wpctl makes the duck silently never close, and no fake-based test can
    see that - FakeVolumeControl records whatever int it is handed.
    """
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom", capture_node_name=None)

    duck = routing_graph.node_by_name(DUCK_NODE)
    assert router.duck_serial == duck.serial
    assert router.duck_id == duck.id
    assert duck.serial != duck.id, "fixture must keep them distinct to be meaningful"


def test_ports_are_ordered_by_name_so_index_pairing_is_stable(routing_graph):
    """engage() pairs ports by index, so ports_of()'s ordering is load-bearing.

    graph.py sorts by port name. The fixtures happen to list FL before FR, so
    a test that only checked the fixture's own order would pass whether or not
    sorting happened at all - this asserts the sort explicitly, because a
    silent reordering would cross channels while re-routing live call audio.
    """
    stream = next(
        n for n in routing_graph.by_class("Stream/Output/Audio") if n.matches("zoom")
    )
    names = [p.name for p in routing_graph.ports_of(stream.id, "out")]
    assert names == sorted(names)
    assert names == ["output_FL", "output_FR"]


def test_engage_leaves_sidetaps_own_sinks_alone(tmp_path, routing_graph):
    """Only the default sink is unlinked, never every sink in the graph."""
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom", capture_node_name=None)

    journal = Journal.load(tmp_path / "j.json")
    # 1400 is sidetap_tts_sink: touching it would cut the virtual mic.
    assert all(r.dst_serial != 1400 for r in journal.broken)
    assert all(r.dst_serial == 1001 for r in journal.broken)


def test_the_virtmic_config_declares_both_halves():
    assert VIRTMIC_SINK in VIRTMIC_CONFIG
    assert VIRTMIC_SOURCE in VIRTMIC_CONFIG
    assert "Audio/Sink" in VIRTMIC_CONFIG
    assert "Audio/Source" in VIRTMIC_CONFIG
    assert "libpipewire-module-loopback" in VIRTMIC_CONFIG
