"""MOM-04's budget end to end, on real vendors and the real model (MEETINGS.md 8: P95 stop to minutes at most 3 min
for a 30 min meeting). It costs money, so it runs on demand only (docs/bench.md, "Stop to minutes").

`uv run scripts/stop_to_minutes.py --budget-usd USD [--runs 5]`. Each run takes 30 minutes of AMI's EN2002a
(CC BY 4.0, cached by `just eval-transcript`), encoded as the app records a meeting (16 kHz mono AAC, 32 kbps), and
times what happens after Stop: the upload to the bucket, both vendors (`AssemblyAiPyannoteStrategy`, asked every
2 s as the worker's webhooks would answer), the merge into transcript lines, and the minutes (`minutes.run` on the
server's polish model: one call per section, the first section's landing timed too). It prints each stage and the
P95 of the totals and of the first section, writes
`~/.snapbak-bench/stop-to-minutes.json`, charges the spend ledger bench.py shares, and exits 1 over the budget.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Any
from uuid import uuid4

from eval_transcript import CACHE, HOUR_MS, PRICE_PER_HOUR
from vendor_run import heard, percentile

from bench import Budget, Metered, append_ledger, ledger_total
from snapbak_backend.ai.chain import Tiers
from snapbak_backend.ai.providers.factory import make_provider
from snapbak_backend.config.settings import load
from snapbak_backend.db.models import Speaker, TranscriptLine
from snapbak_backend.schemas.cloud import MinutesOut
from snapbak_backend.services import minutes
from snapbak_backend.services.storage import Storage, s3_client
from snapbak_backend.transcription.assemblyai_pyannote import AssemblyAiPyannoteStrategy
from snapbak_backend.transcription.merge import merge, text

SOURCE = CACHE / "EN2002a.Mix-Headset.wav"
MEETING_MS = 30 * 60 * 1000
START_MS = 2 * 60 * 1000
BUDGET_S = 180
LEDGER = Path.home() / ".snapbak-bench" / "spend.jsonl"
OUT = Path.home() / ".snapbak-bench" / "stop-to-minutes.json"
# A 30 min meeting's minutes: ten section calls of about 10,000 tokens in each (replay.md), about 4 cents.
MINUTES_USD = 0.05


def recorded_meeting(folder: Path) -> Path:
    """The 30 minutes as the app writes them: cut from the WAV, then AAC at 16 kHz mono, 32 kbps (afconvert)."""
    clip, m4a = folder / "meeting.wav", folder / "meeting.m4a"
    with wave.open(str(SOURCE), "rb") as source, wave.open(str(clip), "wb") as out:
        rate = source.getframerate()
        source.setpos(START_MS * rate // 1000)
        out.setparams(source.getparams())
        out.writeframes(source.readframes(MEETING_MS * rate // 1000))
    subprocess.run(["afconvert", "-f", "m4af", "-d", "aac@16000", "-c", "1", "-b", "32000", clip, m4a], check=True)
    return m4a


def lines_of(transcript: dict, output: dict) -> tuple[list[TranscriptLine], dict]:
    """The vendors' answers merged into transcript lines and speakers, as `merge_and_store` stores them."""
    merged = merge(transcript, output.get("exclusiveDiarization") or [], "en")
    recording = uuid4()
    speakers: dict = {}
    made: list[TranscriptLine] = []
    for idx, words in enumerate(merged.lines):
        label = words[0].speaker
        if label not in speakers:
            speakers[label] = Speaker(id=uuid4(), recording_id=recording, label=label, vendor_label=label, name=None)
        line = TranscriptLine(
            id=uuid4(),
            recording_id=recording,
            speaker_id=speakers[label].id,
            idx=idx,
            start_ms=words[0].s,
            end_ms=words[-1].e,
            text=text(words, merged.language),
            words=[],
        )
        made.append(line)
    return made, {speaker.id: speaker for speaker in speakers.values()}


async def one(strategy: AssemblyAiPyannoteStrategy, storage: Storage, tiers: Tiers, audio: Path) -> dict[str, float]:
    started = time.monotonic()
    vendors_s, transcript, output = await heard(strategy, storage, audio, "en")
    heard_s = time.monotonic() - started
    lines, speakers = lines_of(transcript, output)
    merged_s = time.monotonic() - started
    saves: list[tuple[float, int]] = []

    async def saved(doc: MinutesOut) -> None:
        saves.append((time.monotonic() - started, len(doc.pending)))

    doc, _ = await minutes.run(tiers, "EN2002a", [], lines, speakers, minutes.empty(list(minutes.CALLS)), saved)
    code = minutes.failure(doc)
    ended = f"failed:{code}" if code else f"ready, {len(doc.incomplete)} sections incomplete"
    total_s = time.monotonic() - started
    row = {
        "upload s": round(heard_s - vendors_s, 1),
        "vendors s": round(vendors_s, 1),
        "merge s": round(merged_s - heard_s, 1),
        "first section s": round(saves[0][0] if saves else total_s, 1),
        "minutes s": round(total_s - merged_s, 1),
        "total s": round(total_s, 1),
        "lines": float(len(lines)),
    }
    timing = stages(merged_s, saves)
    print(f"  {ended}: " + ", ".join(f"{name} {value}" for name, value in {**row, **timing}.items()))
    return row


def stages(merged_at: float, saves: list[tuple[float, int]]) -> dict[str, Any]:
    """Each section's time from its save and the one before it (the sections land in `minutes.CALLS` order, `pending`
    shrinking by one), the detail guard's after the last section, and the slowest section by name."""
    marks = [merged_at, *(at for at, _ in saves[: len(minutes.CALLS)])]
    took = {name: marks[n + 1] - marks[n] for n, name in enumerate(minutes.CALLS[: len(marks) - 1])}
    slowest = max(took, key=lambda name: took[name]) if took else "-"
    guard = saves[-1][0] - saves[len(minutes.CALLS) - 1][0] if len(saves) > len(minutes.CALLS) else 0.0
    return {"slowest": slowest, "slowest s": round(took.get(slowest, 0.0), 1), "guard s": round(guard, 1)}


def verdict(rows: list[dict[str, float]]) -> tuple[bool, dict[str, float]]:
    """Whether the runs' P95 stop to all sections is inside BUDGET_S, and the summary; no run is no pass."""
    totals = [row["total s"] for row in rows]
    firsts = [row["first section s"] for row in rows]
    summary = {"runs": float(len(rows)), "p95 total s": percentile(totals, 95) if totals else float("nan")}
    summary["p95 first section s"] = percentile(firsts, 95) if firsts else float("nan")
    return bool(totals) and summary["p95 total s"] <= BUDGET_S, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--budget-usd", type=float, required=True, help="send nothing that would take the ledger past this"
    )
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    options = parser.parse_args(argv)
    config = load(os.environ)
    cloud = config.cloud
    if cloud is None or not cloud.assemblyai_key or not cloud.pyannote_key:
        raise SystemExit("Set the cloud settings and both vendor keys (SETUP.md); the vendors fetch from the bucket.")
    if not SOURCE.is_file():
        raise SystemExit(f"Run `just eval-transcript` once: it caches {SOURCE.name}.")
    per_run = MEETING_MS / HOUR_MS * PRICE_PER_HOUR + MINUTES_USD
    budget = Budget(options.budget_usd, ledger_total(options.ledger))
    folder = CACHE / "stop-to-minutes"
    folder.mkdir(exist_ok=True)
    audio = recorded_meeting(folder)
    provider = make_provider(config.polish, config.keys, config.temperature)
    metered = Metered(provider, budget)
    # The worker's own tiers: its fallback joins a stuck section (minutes.SECTION_HEDGE_S), so the path is the one
    # MOM-04 measures.
    backup = Metered(make_provider(config.fallback, config.keys), budget) if config.fallback else None
    tiers = Tiers(polish=metered, command=metered, fallback=backup)
    rows: list[dict[str, float]] = []
    vendor_spent = 0.0

    async def runs() -> None:
        nonlocal vendor_spent
        storage = Storage(s3_client(cloud), cloud.bucket)
        strategy = AssemblyAiPyannoteStrategy(cloud.assemblyai_key, cloud.pyannote_key, storage)
        try:
            for n in range(options.runs):
                if budget.before + budget.spent + vendor_spent + per_run > options.budget_usd:
                    print("budget reached")
                    return
                vendor_spent += per_run - MINUTES_USD
                print(f"run {n + 1}")
                rows.append(await one(strategy, storage, tiers, audio))
        finally:
            await strategy.aclose()
            await provider.aclose()
            if backup is not None:
                await backup.aclose()

    try:
        asyncio.run(runs())
    finally:
        append_ledger(options.ledger, budget.spent + vendor_spent, len(rows), f"stop-to-minutes {config.polish}")
    passing, summary = verdict(rows)
    OUT.write_text(json.dumps({"runs": rows, "summary": summary}, indent=1) + "\n")
    print(
        f"P95 stop to all sections: {summary['p95 total s']} s, to the first section "
        f"{summary['p95 first section s']} s, over {len(rows)} runs (budget {BUDGET_S} s); "
        f"$ {budget.spent + vendor_spent:.3f}"
    )
    return 0 if passing else 1


if __name__ == "__main__":
    sys.exit(main())
