import threading
from dataclasses import replace

from sidetap.adapters import SystemClock
from sidetap.graph import PwGraph, PwNode, PwPort
from sidetap.ports import LinkResult
from sidetap.tap import GRAPH_ERROR_WARN_AFTER, POLL_INTERVAL_S, AppTap
from tests.conftest import FakeClock, FakeGraphSource, FakeLinker

CAPTURE_NODE = "sidetap.remote.deadbeef"


def with_capture_node(graph: PwGraph) -> PwGraph:
    """Our pw-record node, as it appears once the process is running."""
    node = PwNode(
        id=70,
        serial=2000,
        name=CAPTURE_NODE,
        description="",
        media_class="Stream/Input/Audio",
    )
    port = PwPort(id=700, node_id=70, name="input_MONO", direction="in")
    return replace(graph, nodes=graph.nodes + (node,), ports=graph.ports + (port,))


def make_tap(*snapshots, linker=None, clock=None):
    return AppTap(
        pattern="zoom",
        capture_node_name=CAPTURE_NODE,
        graph=FakeGraphSource(*snapshots),
        linker=linker or FakeLinker(),
        clock=clock or FakeClock(),
    )


def test_links_every_app_channel_into_our_mono_input(zoom_graph):
    linker = FakeLinker()
    tap = make_tap(with_capture_node(zoom_graph), linker=linker)

    assert tap.poll_once() == 2
    # Both of Zoom's channels fan into our single input; PipeWire sums them.
    assert linker.links == [(60, 700), (61, 700)]


def test_ignores_applications_that_do_not_match(zoom_graph):
    linker = FakeLinker()
    tap = make_tap(with_capture_node(zoom_graph), linker=linker)

    tap.poll_once()

    # Spotify's port is 62 and must never be linked.
    assert 62 not in [src for src, _ in linker.links]


def test_waits_for_the_stream_to_appear(idle_graph, zoom_graph):
    linker = FakeLinker()
    tap = make_tap(
        with_capture_node(idle_graph),
        with_capture_node(zoom_graph),
        linker=linker,
    )

    # Zoom has not started its stream yet.
    assert tap.poll_once() == 0
    assert linker.links == []

    # Meeting starts.
    assert tap.poll_once() == 2


def test_does_not_relink_on_every_poll(zoom_graph):
    linker = FakeLinker()
    tap = make_tap(with_capture_node(zoom_graph), linker=linker)

    tap.poll_once()
    tap.poll_once()
    tap.poll_once()

    assert len(linker.links) == 2


def test_does_nothing_until_our_capture_node_exists(zoom_graph):
    linker = FakeLinker()
    tap = make_tap(zoom_graph, linker=linker)  # no capture node in the graph

    assert tap.poll_once() == 0
    assert linker.links == []


def test_already_linked_is_not_treated_as_a_failure(zoom_graph):
    linker = FakeLinker(result=LinkResult.ALREADY_LINKED)
    tap = make_tap(with_capture_node(zoom_graph), linker=linker)

    # Nothing new was created, but nothing went wrong either.
    assert tap.poll_once() == 0
    assert len(linker.links) == 2


def test_records_which_applications_were_tapped(zoom_graph):
    tap = make_tap(with_capture_node(zoom_graph))

    tap.poll_once()

    assert tap.tapped_labels == {"ZOOM VoiceEngine"}


def test_relinks_a_restarted_stream_whose_port_ids_were_recycled(zoom_graph):
    # PipeWire hands a dead stream's port ids to a new one within a poll
    # interval - verified against a live session. The old link died with the
    # old node, so the new stream must be linked even though the ids match.
    linker = FakeLinker()
    restarted = replace(
        zoom_graph,
        nodes=tuple(
            replace(n, serial=1900) if n.id == 55 else n for n in zoom_graph.nodes
        ),
    )
    tap = make_tap(
        with_capture_node(zoom_graph), with_capture_node(restarted), linker=linker
    )

    assert tap.poll_once() == 2
    assert tap.poll_once() == 2
    assert linker.links == [(60, 700), (61, 700), (60, 700), (61, 700)]


def test_a_failed_link_is_retried_next_poll(zoom_graph):
    # pw-link can lose a race with a stream still negotiating its format.
    # That must not poison the pair for the rest of the session.
    linker = FakeLinker(result=LinkResult.FAILED)
    tap = make_tap(with_capture_node(zoom_graph), linker=linker)

    assert tap.poll_once() == 0
    assert tap.poll_once() == 0

    assert len(linker.links) == 4


def test_a_failed_link_warns_once_not_every_poll(zoom_graph, caplog):
    linker = FakeLinker(result=LinkResult.FAILED)
    tap = make_tap(with_capture_node(zoom_graph), linker=linker)

    tap.poll_once()
    tap.poll_once()
    tap.poll_once()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2  # one per port, not one per poll


def test_run_polls_on_the_interval_until_stopped(zoom_graph):
    stop = threading.Event()
    clock = FakeClock()
    graph = FakeGraphSource(with_capture_node(zoom_graph))
    linker = FakeLinker()
    tap = AppTap(
        pattern="zoom",
        capture_node_name=CAPTURE_NODE,
        graph=graph,
        linker=linker,
        clock=clock,
    )

    original_wait = clock.wait

    def wait_and_maybe_stop(event, timeout):
        result = original_wait(event, timeout)
        if len(clock.waited) >= 3:
            stop.set()
        return result

    clock.wait = wait_and_maybe_stop  # type: ignore[method-assign]
    tap.run(stop)

    assert clock.waited == [POLL_INTERVAL_S] * 3
    assert graph.calls == 3


def test_repeated_graph_failures_escalate_to_a_warning(caplog):
    class BrokenGraph:
        def snapshot(self):
            raise RuntimeError("pw-dump exploded")

    stop = threading.Event()
    clock = FakeClock()
    tap = AppTap(
        pattern="zoom",
        capture_node_name=CAPTURE_NODE,
        graph=BrokenGraph(),
        linker=FakeLinker(),
        clock=clock,
    )
    original_wait = clock.wait

    def wait_and_maybe_stop(event, timeout):
        result = original_wait(event, timeout)
        if len(clock.waited) >= GRAPH_ERROR_WARN_AFTER + 3:
            stop.set()
        return result

    clock.wait = wait_and_maybe_stop  # type: ignore[method-assign]
    tap.run(stop)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    # Quiet for a blip, one warning once it is clearly persistent, and not
    # one per poll after that.
    assert len(warnings) == 1
    assert "failing repeatedly" in warnings[0].getMessage()
    assert "pw-dump exploded" in warnings[0].getMessage()


def test_stop_wakes_the_watcher_immediately_instead_of_waiting_out_the_interval(
    idle_graph,
):
    # Regression test for a real shutdown bug: run() used to end its loop
    # with a plain, uninterruptible clock.sleep(interval). The tap spends
    # almost all its time there, so a shutdown request landed while asleep
    # had to wait out the *entire* interval before router.restore() could
    # hand the user's call audio back. A real thread and a real Clock is the
    # only way to prove the fix: with the old sleep(), this test would have
    # to wait out `interval` (here picked deliberately huge) before the
    # thread joined; with wait(), it returns as soon as `stop` is set.
    entered_wait = threading.Event()

    class ProbeClock(SystemClock):
        def wait(self, event: threading.Event, timeout: float) -> bool:
            entered_wait.set()
            return super().wait(event, timeout)

    stop = threading.Event()
    tap = AppTap(
        pattern="zoom",
        capture_node_name=CAPTURE_NODE,
        graph=FakeGraphSource(idle_graph),
        linker=FakeLinker(),
        clock=ProbeClock(),
        interval=5.0,
    )

    thread = threading.Thread(target=tap.run, args=(stop,), daemon=True)
    thread.start()
    assert entered_wait.wait(timeout=1.0), "tap never reached the wait"

    stop.set()
    thread.join(timeout=1.0)

    assert not thread.is_alive(), "tap did not wake promptly when stop was set"
