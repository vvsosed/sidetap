"""A running spend estimate.

Deliberately an estimate, not an invoice. Google's per-character prices render
dynamically on its pricing pages and third-party trackers disagree, so these
are a starting point to be reconfirmed at build time - good enough to notice a
runaway session, not to reconcile a bill.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rates:
    stt_per_minute: float = 0.016
    mt_per_million_chars: float = 20.0
    tts_per_million_chars: float = 30.0

    def translation_usd(self, chars: int) -> float:
        return chars * self.mt_per_million_chars / 1_000_000

    def synthesis_usd(self, chars: int) -> float:
        return chars * self.tts_per_million_chars / 1_000_000

    def recognition_usd(self, seconds: float) -> float:
        return seconds / 60 * self.stt_per_minute
