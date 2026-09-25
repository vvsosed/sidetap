import json
import logging
import os

import pytest

from sidetap.ports import LinkResult
from sidetap.routing import (
    DUCK_NODE,
    VIRTMIC_CAPTURE_DESCRIPTION,
    VIRTMIC_CONFIG,
    VIRTMIC_DESCRIPTION,
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
    router.engage(app_pattern="zoom")

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
    router.engage(app_pattern="zoom")
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
    router.engage(app_pattern="zoom")
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
    router.engage(app_pattern="zoom")
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
    router.engage(app_pattern="zoom")
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
    router.engage(app_pattern="zoom")
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
    router.engage(app_pattern="zoom")

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
    router.engage(app_pattern="zoom")
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
    router.engage(app_pattern="zoom")
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
    router.engage(app_pattern="zoom")

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
    router.engage(app_pattern="zoom")

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


# --- Quality review follow-ups -------------------------------------------


def test_a_failed_route_is_not_marked_routed_and_is_retried(tmp_path, routing_graph):
    """A FAILED apply must not be recorded as routed.

    tap.py's AppTap deliberately leaves a failed pair unrecorded "so it is
    retried" - Router must behave the same way. Recording it anyway would
    mean: if the unlink from the speakers succeeds but the link into the
    duck fails, the stream ends up connected to nothing at all, and
    poll_once() would never try again because the serial already looks done.
    """
    linker = FakeLinker(result=LinkResult.FAILED)
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    attempts_after_engage = len(linker.links) + len(linker.unlinks)
    assert attempts_after_engage > 0, "engage() should still have tried"

    # If the stream had been (wrongly) marked routed, this call would make no
    # further linker calls at all.
    router.poll_once()
    attempts_after_poll = len(linker.links) + len(linker.unlinks)
    assert attempts_after_poll > attempts_after_engage, "a failed route was never retried"


def test_a_failed_route_succeeds_once_the_transient_failure_clears(tmp_path, routing_graph):
    linker = FakeLinker(result=LinkResult.FAILED)
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")

    linker.result = LinkResult.LINKED
    assert router.poll_once() == 1


def test_restore_keeps_the_journal_when_a_link_fails(tmp_path, routing_graph):
    """A journal that survives one failed restore is recoverable; one that

    gets erased anyway is not. A transient pw-link timeout during a normal
    exit must not both fail to restore the graph AND destroy the only record
    that could repair it at next startup.
    """
    path = tmp_path / "j.json"
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=path,
    )
    router.engage(app_pattern="zoom")
    before = Journal.load(path)
    assert not before.is_empty()

    linker.result = LinkResult.FAILED
    router.restore()

    assert Journal.load(path) == before, "a failed restore must not erase the journal"


def test_restore_logs_an_error_when_it_cannot_fully_restore(tmp_path, routing_graph, caplog):
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    linker.result = LinkResult.FAILED
    with caplog.at_level(logging.ERROR):
        router.restore()
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_journal_save_is_atomic_a_failed_write_does_not_corrupt_the_previous_file(
    tmp_path, monkeypatch
):
    """_route() does a load-modify-save of the ACCUMULATED journal on every

    call, so a torn write on the second or later call would destroy the
    record of mutations that are already applied and live, not just the one
    being added. save() must write elsewhere and swap the file in atomically.
    """
    path = tmp_path / "journal.json"
    original = Journal(broken=(LinkRef(1, "a", 2, "b"),))
    original.save(path)
    original_bytes = path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    newer = Journal(broken=(LinkRef(9, "x", 9, "y"),))
    with pytest.raises(OSError):
        newer.save(path)

    assert path.read_bytes() == original_bytes, "a failed save corrupted the live journal"


def test_loading_a_corrupt_journal_logs_an_error(tmp_path, caplog):
    # Unlike a merely missing journal, a file that exists but cannot be
    # parsed can only mean a write was interrupted - possibly the only record
    # of a link that is still live. That must not be silent.
    path = tmp_path / "journal.json"
    path.write_text("{ not json")
    with caplog.at_level(logging.ERROR):
        Journal.load(path)
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_loading_a_missing_journal_does_not_log_an_error(tmp_path, caplog):
    # The common case - no session has ever journalled here - must stay
    # quiet, or every ordinary startup would print a scary error.
    with caplog.at_level(logging.WARNING):
        Journal.load(tmp_path / "nope.json")
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_route_holds_the_lock_while_reading_the_graph(tmp_path, routing_graph):
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=FakeLinker(),
        unlinker=FakeLinker(),
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    held_during_read = []

    class SpyGraph:
        def snapshot(self):
            acquired = router._lock.acquire(blocking=False)
            held_during_read.append(acquired)
            if acquired:
                router._lock.release()
            return routing_graph

    router._graph = SpyGraph()
    router.engage(app_pattern="zoom")
    assert held_during_read == [False], "engage() must hold the lock while reading the graph"


def test_restore_holds_the_lock_while_reading_the_graph(tmp_path, routing_graph):
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=FakeLinker(),
        unlinker=FakeLinker(),
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")

    held_during_read = []

    class SpyGraph:
        def snapshot(self):
            acquired = router._lock.acquire(blocking=False)
            held_during_read.append(acquired)
            if acquired:
                router._lock.release()
            return routing_graph

    router._graph = SpyGraph()
    router.restore()
    assert held_during_read == [False], "restore() must hold the lock while reading the graph"


def test_repair_holds_the_lock_while_reading_the_graph(tmp_path, routing_graph):
    router = Router(
        graph=None,
        linker=FakeLinker(),
        unlinker=FakeLinker(),
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "absent.json",
    )
    held_during_read = []

    class SpyGraph:
        def snapshot(self):
            acquired = router._lock.acquire(blocking=False)
            held_during_read.append(acquired)
            if acquired:
                router._lock.release()
            return routing_graph

    router._graph = SpyGraph()
    router.repair()
    assert held_during_read == [False], "repair() must hold the lock while reading the graph"


def test_engage_twice_raises_instead_of_leaking_a_second_duck(tmp_path, routing_graph):
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=FakeLinker(),
        unlinker=FakeLinker(),
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    with pytest.raises(RuntimeError):
        router.engage(app_pattern="zoom")


def test_repair_warns_about_an_orphaned_duck_node(tmp_path, routing_graph, caplog):
    """A kill -9 leaves pw-loopback running under start_new_session=True.

    repair() has no PID to kill it with - the journal records links, not
    processes - so the best it can do is tell the user how to find it by
    hand.
    """
    router = Router(
        graph=FakeGraphSource(routing_graph),  # already contains sidetap_duck
        linker=FakeLinker(),
        unlinker=FakeLinker(),
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "absent.json",
    )
    with caplog.at_level(logging.WARNING):
        router.repair()
    assert any("pkill" in r.message for r in caplog.records)


def test_the_virtmic_config_distinguishes_the_two_nodes():
    """Both nodes showing the same description in a volume UI invites

    picking the wrong one - the symptom would be the remote party hearing
    nothing, with no error anywhere to explain why.
    """
    assert VIRTMIC_CAPTURE_DESCRIPTION != VIRTMIC_DESCRIPTION
    _, after_capture = VIRTMIC_CONFIG.split("capture.props")
    capture_section, playback_section = after_capture.split("playback.props")
    assert VIRTMIC_CAPTURE_DESCRIPTION in capture_section
    assert VIRTMIC_DESCRIPTION not in capture_section
    assert VIRTMIC_DESCRIPTION in playback_section


def test_restore_terminates_the_duck_even_when_no_stream_was_ever_routed(
    tmp_path, idle_graph
):
    """Starting sidetap before the call and quitting before it begins.

    Nothing matches the app pattern, so the journal stays empty for the whole
    session. restore() used to return early on that and skip the loopback it
    created in engage(), orphaning a pw-loopback whose PID no later run can
    recover - and the next engage() then adds a SECOND node called
    sidetap_duck, after which node_by_name picks one of them arbitrarily.
    """
    loopbacks = FakeLoopbackFactory()
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(idle_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=loopbacks,
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="nothing-is-playing")
    assert not (tmp_path / "j.json").exists() or Journal.load(
        tmp_path / "j.json"
    ).is_empty()
    router.restore()
    assert loopbacks.processes[0].terminated, "the duck outlived the session"


def test_a_persistently_failing_stream_does_not_grow_the_journal(
    tmp_path, routing_graph
):
    """The retry is deliberate; re-recording the same refs each time is not.

    A failing stream is kept out of _routed so every poll retries it. Appending
    its refs unconditionally meant a full JSON rewrite per poll, under the lock
    shutdown needs to restore the graph, and every duplicate replayed again on
    the way out.
    """
    linker = FakeLinker(result=LinkResult.FAILED)
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    after_engage = Journal.load(tmp_path / "j.json")
    for _ in range(5):
        router.poll_once()
    after_polls = Journal.load(tmp_path / "j.json")

    assert after_engage.broken, "the fixture must produce refs or this proves nothing"
    assert len(after_polls.broken) == len(after_engage.broken)
    assert len(after_polls.made) == len(after_engage.made)


def test_a_failed_journal_write_leaves_no_temp_file_behind(tmp_path, monkeypatch):
    """The temp file is the whole mechanism; a stray one accumulates silently."""
    path = tmp_path / "j.json"
    Journal(broken=(LinkRef(1, "a", 2, "b"),)).save(path)

    def boom(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr("sidetap.routing.os.replace", boom)
    with pytest.raises(OSError):
        Journal(broken=(LinkRef(3, "c", 4, "d"),)).save(path)

    assert Journal.load(path).broken == (LinkRef(1, "a", 2, "b"),)
    assert list(tmp_path.glob("*.tmp")) == []


def test_has_routed_is_false_until_a_stream_is_actually_rewired(
    tmp_path, idle_graph, routing_graph
):
    """It arms the no-audio alarm, so it must mean "the app is playing"."""
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(idle_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    assert router.has_routed is False, "nothing in the idle graph is playing"

    linker2 = FakeLinker()
    playing = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker2,
        unlinker=linker2,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "k.json",
    )
    playing.engage(app_pattern="zoom")
    assert playing.has_routed is True


# --- Routing from the links that actually exist --------------------------

from dataclasses import replace  # noqa: E402

from sidetap.graph import PLAYBACK_STREAM, SINK, PwLink, PwNode, PwPort  # noqa: E402
from tests.conftest import REAL_NEW_DUCK_NAME  # noqa: E402

HEADSET = PwNode(41, 1003, "alsa_output.headset", "Headset", SINK)
HEADSET_PORTS = (
    PwPort(411, 41, "playback_FL", "in"),
    PwPort(412, 41, "playback_FR", "in"),
)
TO_HEADSET = (PwLink(95, 1601, 55, 551, 41, 411), PwLink(96, 1602, 55, 552, 41, 412))


def _with_headset(graph, links):
    """The routing fixture plus a non-default headset, with the given links."""
    return replace(
        graph,
        nodes=graph.nodes + (HEADSET,),
        ports=graph.ports + HEADSET_PORTS,
        links=links,
    )


def _router(tmp_path, *snapshots, linker=None):
    linker = linker or FakeLinker()
    router = Router(
        graph=FakeGraphSource(*snapshots),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    return router, linker


def test_engage_breaks_the_link_the_app_really_has_not_the_default_sink(
    tmp_path, routing_graph
):
    """Zoom set to play on a headset that is not the default output.

    Assuming the default sink left the headset link live - the original under
    every translation - and restore() then linked Zoom to the speakers, a link
    that never existed, and cleared the journal.
    """
    graph = _with_headset(routing_graph, TO_HEADSET)
    router, linker = _router(tmp_path, graph)
    router.engage(app_pattern="zoom")

    assert sorted(linker.unlinks) == [(551, 411), (552, 412)]
    assert {r.dst_serial for r in Journal.load(tmp_path / "j.json").broken} == {1003}

    linker.links.clear()
    router.restore()
    assert (551, 401) not in linker.links and (552, 402) not in linker.links
    to_headset = sorted(pair for pair in linker.links if pair[1] in (411, 412))
    assert to_headset == [(551, 411), (552, 412)]


def test_a_stream_not_yet_linked_anywhere_journals_nothing_to_restore(
    tmp_path, routing_graph
):
    graph = replace(routing_graph, links=())
    router, linker = _router(tmp_path, graph)
    router.engage(app_pattern="zoom")

    assert linker.unlinks == []
    assert sorted(linker.links) == [(551, 701), (552, 702)]
    assert Journal.load(tmp_path / "j.json").broken == ()
    assert router.has_routed is True


def test_a_speaker_link_that_appears_after_routing_is_broken_too(tmp_path, routing_graph):
    """WirePlumber linking the stream once it is already in the duck.

    Skipping routed streams entirely left that link live for the rest of the
    call.
    """
    unlinked = replace(routing_graph, links=())
    router, linker = _router(tmp_path, unlinked, routing_graph)
    router.engage(app_pattern="zoom")
    linker.links.clear()

    assert router.poll_once() == 0, "already routed; nothing newly routed"
    assert sorted(linker.unlinks) == [(551, 401), (552, 402)]
    assert linker.links == [], "the duck links were already made"
    assert {r.dst_serial for r in Journal.load(tmp_path / "j.json").broken} == {1001}


def test_a_link_already_broken_is_not_broken_again_from_a_stale_snapshot(
    tmp_path, routing_graph
):
    router, linker = _router(tmp_path, routing_graph)
    router.engage(app_pattern="zoom")
    calls = (len(linker.links), len(linker.unlinks))

    router.poll_once()
    router.poll_once()
    assert (len(linker.links), len(linker.unlinks)) == calls


def test_a_re_created_link_is_broken_again(tmp_path, routing_graph):
    """Same ports, new link: its serial differs from the one we removed."""
    recreated = replace(
        routing_graph,
        links=tuple(replace(link, id=link.id + 100, serial=link.serial + 100)
                    for link in routing_graph.links),
    )
    router, linker = _router(tmp_path, routing_graph, recreated)
    router.engage(app_pattern="zoom")
    linker.unlinks.clear()

    router.poll_once()
    assert sorted(linker.unlinks) == [(551, 401), (552, 402)]
    assert len(Journal.load(tmp_path / "j.json").broken) == 2, "same refs, not duplicated"


def test_a_routed_stream_moved_to_another_sink_is_restored_there_only(
    tmp_path, routing_graph
):
    """Restoring both the old and the new target would play the call twice."""
    moved = _with_headset(routing_graph, TO_HEADSET)
    router, linker = _router(tmp_path, routing_graph, moved)
    router.engage(app_pattern="zoom")
    router.poll_once()

    assert {r.dst_serial for r in Journal.load(tmp_path / "j.json").broken} == {1003}
    linker.links.clear()
    router.restore()
    assert sorted(linker.links) == [(551, 411), (552, 412)]


def test_a_failed_link_into_the_duck_leaves_the_speaker_link_alone(
    tmp_path, routing_graph
):
    """Audible beats silent: never unlink the old path before the new one works."""

    class DuckRefuses(FakeLinker):
        def link(self, src, dst):
            self.links.append((src, dst))
            return LinkResult.FAILED

    router, linker = _router(tmp_path, routing_graph, linker=DuckRefuses())
    router.engage(app_pattern="zoom")

    assert linker.links, "it tried"
    assert linker.unlinks == []
    assert router.has_routed is False


def test_a_stream_without_ports_yet_is_routed_once_they_appear(tmp_path, routing_graph):
    portless = replace(
        routing_graph,
        ports=tuple(p for p in routing_graph.ports if p.node_id != 55),
        links=(),
    )
    router, linker = _router(tmp_path, portless, routing_graph)
    router.engage(app_pattern="zoom")
    assert linker.links == [] and router.has_routed is False

    assert router.poll_once() == 1
    assert sorted(linker.links) == [(551, 701), (552, 702)]


def test_sidetaps_own_sinks_are_never_treated_as_the_apps_output(tmp_path, routing_graph):
    """Links into the TTS sink or a leftover duck are not something to restore."""
    graph = replace(
        routing_graph,
        links=routing_graph.links + (PwLink(97, 1603, 55, 551, 80, 801),),
    )
    router, linker = _router(tmp_path, graph)
    router.engage(app_pattern="zoom")

    assert (551, 801) not in linker.unlinks
    assert all(r.dst_serial != 1400 for r in Journal.load(tmp_path / "j.json").broken)


def test_each_engage_names_its_duck_uniquely(tmp_path, routing_graph, monkeypatch):
    from sidetap import routing

    monkeypatch.setattr(routing, "_new_duck_name", REAL_NEW_DUCK_NAME)
    names = []
    for index in range(2):
        loopbacks = FakeLoopbackFactory()
        router = Router(
            graph=FakeGraphSource(routing_graph),
            linker=FakeLinker(),
            unlinker=FakeLinker(),
            loopbacks=loopbacks,
            journal_path=tmp_path / f"{index}.json",
        )
        router.engage(app_pattern="zoom")
        assert dict(loopbacks.specs[0].capture_props)["node.name"] == router.duck_name
        names.append(router.duck_name)

    assert names[0] != names[1]
    assert all(name.startswith(DUCK_NODE + ".") for name in names)


def test_engage_never_routes_into_a_duck_left_by_an_earlier_session(
    tmp_path, routing_graph, monkeypatch
):
    """A session killed without cleanup can leave its duck at 0% volume.

    With a shared name, the next run routed the call into that leftover and,
    believing its own duck was open, never raised the volume: silence until
    the first translation happened to cycle the duck.
    """
    from sidetap import routing

    monkeypatch.setattr(routing, "_new_duck_name", REAL_NEW_DUCK_NAME)
    router, linker = _router(tmp_path, routing_graph)  # contains a leftover sidetap_duck
    router.engage(app_pattern="zoom")
    assert not any(dst in (701, 702) for _, dst in linker.links)
    assert router.duck_id is None

    own = PwNode(72, 1302, router.duck_name, "sidetap duck", SINK)
    own_ports = (
        PwPort(721, 72, "playback_FL", "in"),
        PwPort(722, 72, "playback_FR", "in"),
    )
    later = replace(
        routing_graph,
        nodes=routing_graph.nodes + (own,),
        ports=routing_graph.ports + own_ports,
    )
    router._graph = FakeGraphSource(later)
    assert router.poll_once() == 1
    assert sorted(linker.links) == [(551, 721), (552, 722)]
    assert router.duck_id == 72


def test_repair_warns_about_a_leftover_duck_whatever_its_suffix(
    tmp_path, routing_graph, caplog
):
    leftover = PwNode(73, 1303, DUCK_NODE + ".1a2b3c4d", "sidetap duck", SINK)
    graph = replace(routing_graph, nodes=routing_graph.nodes + (leftover,))
    router, _ = _router(tmp_path, graph)
    with caplog.at_level(logging.WARNING):
        router.repair()
    assert any(DUCK_NODE + ".1a2b3c4d" in r.message for r in caplog.records)
