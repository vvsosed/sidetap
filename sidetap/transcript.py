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

    NOTE: do NOT infer a duration from `t_end - t`. Chirp 3 gives no word
    timestamps in streaming mode (setting enable_word_time_offsets is a fatal
    InvalidArgument), so an utterance's only timestamp is its end offset, and
    the two fields mean different things depending on how the row was cut:

      - A whole utterance - every row under --no-early-commit, and every row
        for an utterance LocalAgreementSegmenter committed nothing early for,
        which is all of ordinary turn-taking conversation - carries that one
        end offset in BOTH fields, so `t_end - t` is zero and every such row
        would read as instantaneous.
      - A clause committed early carries the PREVIOUS clause's end offset in
        `t`, so `t_end - t` is the gap between two recognition hypotheses
        (~5 s, whatever Chirp's interim cadence was), not how long the clause
        took to say.

    The duration of the SPOKEN audio is derivable from the synthesised PCM
    instead, via Translated.audio_s.
    """
    return {
        "t": record.unit.t_start,
        "t_end": record.unit.t_end,
        "direction": record.direction.value,
        "source": record.unit.text,
        "target": record.target_text,
        "dropped": record.dropped,
        "truncated": record.truncated,
        "latency": {
            "asr_ms": record.latency.asr_ms,
            "mt_ms": record.latency.mt_ms,
            "tts_ms": record.latency.tts_ms,
            "tts_total_ms": record.latency.tts_total_ms,
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
        if record.dropped:
            # Bypass and the lag cap both produce a dropped record, so this
            # must not name either mechanism specifically - "backlog
            # dropped" used to read as the lag cap even when the user had
            # pressed bypass and the backlog never fired.
            suffix = "  _(not spoken)_"
        elif record.truncated and record.latency.tts_ms == 0.0:
            # Playout gave up before it ever accepted anything: nothing was
            # heard. "Cut short" implies a beginning this utterance never
            # had - tts_ms == 0.0 is exactly the signal that distinguishes
            # it from the case below, since the jsonl already carries that
            # distinction and the markdown otherwise renders both the same.
            suffix = "  _(not spoken: synthesis stalled)_"
        elif record.truncated:
            suffix = "  _(cut short before the end)_"
        else:
            suffix = ""
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
