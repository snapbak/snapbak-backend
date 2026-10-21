"""The span of each speaker that becomes its voiceprint, and the clip cut from it (TRX-08; docs/cloud.md). Made
only with the device's consent, one per speaker per recording."""

import asyncio
from pathlib import Path
from typing import Any

from snapbak_backend.transcription.merge import turns_of

JOIN_MS = 300
LONGEST_MS = 30_000  # pyannoteAI's limit for a voiceprint sample
SHORTEST_MS = 3_000


def spans(diarization: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    """Per speaker, `(start_ms, length_ms)` of its longest run of turns less than 300 ms apart, clipped to 30 s.
    A speaker whose longest run is under 3 s gets none. The longest run without cross-talk is the cleanest span the
    data can name."""
    runs: dict[str, tuple[int, int]] = {}  # the speaker's current run
    best: dict[str, tuple[int, int]] = {}
    for turn in turns_of(diarization):
        start, end = runs.get(turn.speaker, (turn.start, turn.end))
        if turn.start - end >= JOIN_MS:
            start = turn.start
        runs[turn.speaker] = (start, max(end, turn.end))
        run_start, run_end = runs[turn.speaker]
        kept = best.get(turn.speaker, (0, 0))
        if run_end - run_start > kept[1] - kept[0]:
            best[turn.speaker] = (run_start, run_end)
    chosen = {
        speaker: (start, min(end - start, LONGEST_MS))
        for speaker, (start, end) in best.items()
        if end - start >= SHORTEST_MS
    }
    return chosen


async def cut(audio: Path, start_ms: int, length_ms: int, out: Path) -> None:
    """A mono 16 kHz WAV of `length_ms` from `start_ms`, through ffmpeg (in the worker image)."""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-t",
        f"{length_ms / 1000:.3f}",
        "-i",
        str(audio),
        "-ac",
        "1",
        "-ar",
        "16000",
        str(out),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, error = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg exited {process.returncode}: {error.decode()[:200]}")
