"""The merge of a 2 h transcript (about 18,000 words, 2,000 turns) stays under its 1 s budget (HLD, Budgets)."""

import statistics
import time
from typing import Any

from snapbak_backend.transcription.merge import merge

HOURS_2_MS = 7_200_000
WORDS = 18_000
TURNS = 2_000
BUDGET_S = 1.0


def long_2h() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A synthetic 2 h transcript: evenly spaced words, a sentence every 12, three speakers taking turns."""
    step = HOURS_2_MS // WORDS
    words = [
        {
            "text": f"word{n}." if n % 12 == 11 else f"word{n}",
            "start": n * step,
            "end": n * step + step - 40,
            "confidence": 0.9,
            "speaker": "Priya" if n % 3 == 0 else "B",
        }
        for n in range(WORDS)
    ]
    span = HOURS_2_MS / TURNS / 1000
    turns = [{"speaker": f"SPEAKER_0{n % 3}", "start": n * span, "end": (n + 1) * span} for n in range(TURNS)]
    return {"words": words, "language_code": "en", "audio_duration": 7200}, turns


def test_trx01_merge_of_2_hours_takes_under_a_second() -> None:
    transcript, turns = long_2h()
    merged = merge(transcript, turns, "auto")  # warm-up
    times = []
    for _ in range(5):
        started = time.perf_counter()
        merged = merge(transcript, turns, "auto")
        times.append(time.perf_counter() - started)
    assert sum(len(line) for line in merged.lines) == WORDS
    median = statistics.median(times)
    print(f"merge 2 h: median {median * 1000:.0f} ms of 5")
    assert median < BUDGET_S
