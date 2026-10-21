"""What `test_cloud.py` (`just test-cloud`) and the transcript eval (`eval_transcript.py`) share: one file through both
real vendors (`heard`: the file goes to the bucket, `AssemblyAiPyannoteStrategy` runs both, their answers come back
with the seconds it took; costs money), the vendors' answers kept as fixtures (`save_answers`), and the word error
rate and percentile helpers (docs/cloud.md)."""

import asyncio
import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from snapbak_backend.config.settings import load
from snapbak_backend.services.signatures import assemblyai_header
from snapbak_backend.services.storage import Storage
from snapbak_backend.transcription.assemblyai_pyannote import AssemblyAiPyannoteStrategy, call
from snapbak_backend.transcription.strategy import VENDORS, JobInput

POLL_S = 2
GOAL_S, CAP_S = 60, 120
WORD = re.compile(r"[\w']+")
CONTENT_TYPES = {".m4a": "audio/mp4", ".wav": "audio/wav"}


def word_error_rate(reference: str, said: str) -> float:
    """Word-level edit distance over the reference's length, case and punctuation ignored."""
    ref = WORD.findall(reference.lower())
    hyp = WORD.findall(said.lower())
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, word in enumerate(ref, 1):
        current = [i]
        for j, guess in enumerate(hyp, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (word != guess)))
        previous = current
    return previous[-1] / len(ref)


def percentile(values: list[float], share: int) -> float:
    ordered = sorted(values)
    index = max(math.ceil(share / 100 * len(ordered)) - 1, 0)
    return ordered[index]


async def heard(
    strategy: AssemblyAiPyannoteStrategy, storage: Storage, audio: Path, language: str = "auto"
) -> tuple[float, dict[str, Any], dict[str, Any]]:
    """One file through both vendors, asked for status every 2 s: the seconds from submit to both answers fetched,
    AssemblyAI's `GET /v2/transcript` body and pyannote's output. AssemblyAI's copy is deleted after, and the audio
    in the bucket is tagged for the bucket's day-later delete, whether the vendors answered or not."""
    config = load(os.environ).cloud
    assert config is not None
    job_id, recording_id = uuid.uuid4(), uuid.uuid4()
    key = f"test-cloud/{job_id}{audio.suffix}"
    await storage.upload(str(audio), key, CONTENT_TYPES[audio.suffix])
    try:
        hook = f"{config.public_url}/v1/webhooks"
        job = JobInput(
            job_id=job_id,
            recording_id=recording_id,
            audio_url=storage.presign_get(key, 7200),
            duration_ms=0,
            language=language,
            webhooks={"assemblyai": f"{hook}/assemblyai?job={job_id}", "pyannote": f"{hook}/pyannote?job={job_id}"},
            webhook_auth=assemblyai_header(config.webhook_secret, str(job_id)),
        )
        started = time.monotonic()
        refs = await strategy.submit(job, {})
        finished: dict[str, float] = {}
        while len(finished) < len(VENDORS):
            await asyncio.sleep(POLL_S)
            for vendor in VENDORS:
                if vendor in finished:
                    continue
                status = await strategy.status(refs, vendor)
                if status.state != "running":
                    finished[vendor] = time.monotonic() - started
                    print(f"  {vendor}: {status.state} {status.failure or ''} after {finished[vendor]:.1f} s")
        transcript = await call(strategy.assemblyai, "GET", f"/v2/transcript/{refs['assemblyai']['id']}")
        output = await storage.get_json(refs["pyannote"]["rawKey"]) or {}
        elapsed = time.monotonic() - started
        deleted = await strategy.assemblyai.delete(f"/v2/transcript/{refs['assemblyai']['id']}")
        print(f"  answered after {elapsed:.1f} s; AssemblyAI delete answered {deleted.status_code}")
        return elapsed, transcript, output
    finally:
        await storage.supersede([key])


def save_answers(folder: Path, transcript: dict[str, Any], output: dict[str, Any]) -> None:
    """The vendors' answers as `assemblyai.json` and `pyannote.json` in `folder`: the fake strategy's fixtures and the
    replay tier's recorded rows."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "assemblyai.json").write_text(json.dumps(transcript, ensure_ascii=False, indent=2) + "\n")
    (folder / "pyannote.json").write_text(json.dumps(output, indent=2) + "\n")
