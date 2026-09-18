"""Durable bilingual transcript: append-only JSONL, Markdown at close."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from .types import Direction, Record

log = logging.getLogger(__name__)

LABELS = {Direction.IN: "Them", Direction.OUT: "You"}


def hhmmss(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def record_to_dict(record: Record) -> dict:
    """One JSONL row.

    NOTE: `t` and `t_end` are always EQUAL. Chirp 3 gives no word timestamps
    in streaming mode (setting enable_word_time_offsets is a fatal
    InvalidArgument), so an utterance's only timestamp is its end offset and
    both fields carry it. Do NOT infer duration from `t_end - t` - every
    utterance would read as instantaneous. The duration of the SPOKEN audio is
    derivable from the synthesised PCM instead, via Translated.audio_s.
    """
    return {
        "t": record.unit.t_start,
        "t_end": record.unit.t_end,
        "direction": record.direction.value,
        "source": record.unit.text,
        "target": record.target_text,
        "dropped": record.dropped,
        "latency": {
            "asr_ms": record.latency.asr_ms,
            "mt_ms": record.latency.mt_ms,
            "tts_ms": record.latency.tts_ms,
            "total_ms": record.latency.total_ms,
        },
        "wall_clock": datetime.now(timezone.utc).isoformat(),
    }


def render_markdown(session: str, records: list[Record]) -> str:
    lines = [f"# Interpretation transcript {session}", ""]
    last_label: str | None = None
    for record in sorted(records, key=lambda r: r.unit.t_start):
        label = LABELS[record.direction]
        if label != last_label:
            lines.append("")
            lines.append(f"**{label}** _{hhmmss(record.unit.t_start)}_")
            last_label = label
        suffix = "  _(not spoken: backlog dropped)_" if record.dropped else ""
        lines.append(f"{record.target_text}{suffix}")
        lines.append(f"> {record.unit.text}")
    return "\n".join(lines) + "\n"


class BilingualTranscript:
    def __init__(self, outdir: Path, session: str | None = None):
        outdir.mkdir(parents=True, exist_ok=True)
        # Sub-second resolution: meetscribe used whole seconds and two runs
        # started within the same second appended into one file.
        self.session = session or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.jsonl_path = outdir / f"{self.session}.jsonl"
        self.md_path = outdir / f"{self.session}.md"
        self._lock = threading.Lock()
        self._records: list[Record] = []
        self._closed = False
        self._jsonl = self.jsonl_path.open("a", encoding="utf-8")

    def write(self, record: Record) -> None:
        with self._lock:
            if self._closed:
                # Session.shutdown() joins the workers on a shared 3 s deadline
                # and then restores the graph whether or not they stopped - the
                # graph matters more than a tidy exit. So a playout thread can
                # still be alive here and still call on_dropped. Writing to the
                # closed handle would raise ValueError inside a daemon thread,
                # printing a traceback over the "Saved:" line in headless mode
                # and over the TUI in the other. The record is already in the
                # rendered Markdown either way.
                log.debug("transcript write after close, ignored: %r", record)
                return
            self._records.append(record)
            self._jsonl.write(
                json.dumps(record_to_dict(record), ensure_ascii=False) + "\n"
            )
            # Flushed per record, so a crash keeps everything up to that moment.
            self._jsonl.flush()

    def close(self) -> Path:
        with self._lock:
            if self._closed:
                return self.md_path
            self._closed = True
            self._jsonl.close()
            self.md_path.write_text(
                render_markdown(self.session, self._records), encoding="utf-8"
            )
        return self.md_path
