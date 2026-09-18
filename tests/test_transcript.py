import json

from sidetap.transcript import LABELS, BilingualTranscript, hhmmss, render_markdown
from sidetap.types import Direction, Latency, Record, Unit


def _record(direction=Direction.IN, source="привет", target="hello", t=1.0, dropped=False):
    return Record(
        unit=Unit(direction=direction, text=source, t_start=t, t_end=t + 1),
        target_text=target,
        latency=Latency(asr_ms=100.0, mt_ms=50.0, tts_ms=250.0),
        dropped=dropped,
    )


def test_labels_name_the_parties_not_the_directions():
    assert LABELS[Direction.IN] == "Them"
    assert LABELS[Direction.OUT] == "You"


def test_hhmmss_formats_hours():
    assert hhmmss(0) == "00:00:00"
    assert hhmmss(3661) == "01:01:01"


def test_jsonl_is_written_per_final(tmp_path):
    transcript = BilingualTranscript(tmp_path, session="s")
    transcript.write(_record())

    lines = (tmp_path / "s.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["direction"] == "in"
    assert row["source"] == "привет"
    assert row["target"] == "hello"
    assert row["latency"]["total_ms"] == 400.0


def test_jsonl_is_flushed_before_close(tmp_path):
    """An unclean exit must still leave everything transcribed on disk."""
    transcript = BilingualTranscript(tmp_path, session="s")
    transcript.write(_record())
    assert (tmp_path / "s.jsonl").read_text(encoding="utf-8").strip() != ""


def test_dropped_records_are_marked(tmp_path):
    transcript = BilingualTranscript(tmp_path, session="s")
    transcript.write(_record(dropped=True))
    row = json.loads((tmp_path / "s.jsonl").read_text(encoding="utf-8"))
    assert row["dropped"] is True


def test_markdown_is_only_written_at_close(tmp_path):
    transcript = BilingualTranscript(tmp_path, session="s")
    transcript.write(_record())
    assert not (tmp_path / "s.md").exists()
    transcript.close()
    assert (tmp_path / "s.md").exists()


def test_markdown_groups_consecutive_turns_by_speaker():
    records = [
        _record(Direction.IN, "раз", "one", t=1.0),
        _record(Direction.IN, "два", "two", t=2.0),
        _record(Direction.OUT, "three", "три", t=3.0),
    ]
    markdown = render_markdown("s", records)
    assert markdown.count("**Them**") == 1
    assert markdown.count("**You**") == 1


def test_markdown_orders_by_time_across_directions():
    records = [
        _record(Direction.OUT, "second", "второй", t=5.0),
        _record(Direction.IN, "первый", "first", t=1.0),
    ]
    markdown = render_markdown("s", records)
    assert markdown.index("первый") < markdown.index("second")


def test_markdown_shows_both_languages():
    markdown = render_markdown("s", [_record(Direction.IN, "привет", "hello")])
    assert "hello" in markdown
    assert "привет" in markdown


def test_markdown_marks_a_dropped_utterance():
    markdown = render_markdown("s", [_record(dropped=True)])
    assert "not spoken" in markdown.lower()


def test_a_write_after_close_is_ignored_rather_than_raising(tmp_path):
    """shutdown() restores the graph even when a worker will not stop.

    So a playout thread can outlive close() and still call on_dropped. Raising
    there prints a daemon-thread traceback over the last thing the user reads.
    """
    transcript = BilingualTranscript(tmp_path, session="s")
    transcript.close()
    transcript.write(_record())  # must not raise
    assert (tmp_path / "s.jsonl").read_text(encoding="utf-8") == ""


def test_close_is_idempotent_and_keeps_the_first_rendering(tmp_path):
    transcript = BilingualTranscript(tmp_path, session="s")
    transcript.write(_record(source="привет"))
    assert transcript.close() == transcript.close() == tmp_path / "s.md"
    assert "привет" in (tmp_path / "s.md").read_text(encoding="utf-8")


def test_close_returns_the_markdown_path(tmp_path):
    transcript = BilingualTranscript(tmp_path, session="s")
    assert transcript.close() == tmp_path / "s.md"


def test_the_session_name_has_sub_second_resolution(tmp_path):
    # meetscribe used one-second resolution and two runs launched in the same
    # second appended into one file.
    a = BilingualTranscript(tmp_path).session
    b = BilingualTranscript(tmp_path).session
    assert a != b
