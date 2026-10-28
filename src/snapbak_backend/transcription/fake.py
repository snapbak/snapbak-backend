"""Replays recorded vendor JSON, so the real merge runs in every test and no vendor is called (D19, Q112;
docs/cloud.md). Fixtures live in `tests/fixtures/transcription/<name>/`: `assemblyai.json` (the
`GET /v2/transcript` body), `pyannote.json` (the webhook `output`) and an optional `fixture.toml` with `delay_ms`
and `failure` (a vendor's failure reason)."""

import json
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from snapbak_backend.transcription.merge import Merged, merge
from snapbak_backend.transcription.strategy import VENDORS, JobInput, Refs, Vendor, VendorStatus

FIXTURES = Path(__file__).parents[3] / "tests" / "fixtures" / "transcription"
VOICEPRINT = "ZmFrZS12b2ljZXByaW50"  # base64 of "fake-voiceprint"

type Enqueue = Callable[..., Awaitable[Any]]


class FakeTranscriptionStrategy:
    name = "fake"

    def __init__(self, fixture: str, enqueue: Enqueue) -> None:
        folder = FIXTURES / fixture
        if not folder.is_dir():
            raise ValueError(f"Unknown transcription fixture '{fixture}'; see {FIXTURES}.")
        self.transcript = json.loads((folder / "assemblyai.json").read_text())
        self.output = json.loads((folder / "pyannote.json").read_text())
        rerun = folder / "rerun.json"
        self.rerun = json.loads(rerun.read_text()) if rerun.is_file() else self.output
        self.prints: list[str] = []
        toml = folder / "fixture.toml"
        settings = tomllib.loads(toml.read_text()) if toml.is_file() else {}
        self.delay_s = settings.get("delay_ms", 0) / 1000
        self.failure = settings.get("failure")
        self.enqueue = enqueue

    async def submit(self, job: JobInput, refs: Refs) -> Refs:
        """Fake refs, and each vendor's webhook after the fixture's delay, so the webhook path runs."""
        made: Refs = {**refs, "language": job.language}
        for vendor in VENDORS:
            if vendor in made:
                continue
            made[vendor] = {"id": f"fake-{job.job_id}", "jobId": f"fake-{job.job_id}", "status": "running"}
            await self.enqueue(
                "on_vendor_webhook", job.job_id, vendor, _job_id=f"hook:{vendor}:{job.job_id}", _defer_by=self.delay_s
            )
        return made

    async def status(self, refs: Refs, vendor: Vendor) -> VendorStatus:
        if self.failure is not None and vendor == "pyannote":
            return VendorStatus("failed", self.failure)
        return VendorStatus("done")

    async def result(self, refs: Refs) -> Merged:
        merged = merge(self.transcript, await self.diarization(refs), refs["language"])
        return merged

    async def diarization(self, refs: Refs) -> list[dict[str, Any]]:
        return self.output.get("exclusiveDiarization") or []

    async def rediarize(self, audio_url: str, count: int) -> str:
        return f"fake-rerun-{count}"

    async def rediarization(self, vendor_job: str) -> list[dict[str, Any]] | None:
        """The fixture's turns again, or `rerun.json`'s when the fixture has one (a rerun that hears other voices)."""
        return self.rerun.get("exclusiveDiarization") or []

    async def identify(self, audio_url: str, prints: dict[str, str]) -> str:
        self.prints = list(prints)
        return "fake-identify"

    async def identification(self, vendor_job: str) -> dict[str, Any] | None:
        """The rerun's turns, each voice matching only its own speaker's voiceprint: nothing is one voice."""
        voices = [{"speaker": label, "match": label, "confidence": {label: 90}} for label in self.prints]
        return {"exclusiveDiarization": await self.rediarization(vendor_job), "voiceprints": voices}

    async def voiceprint(self, clip_url: str, webhook: str) -> str:
        """Calls back as pyannoteAI's webhook would."""
        query = parse_qs(urlparse(webhook).query)
        job, speaker = query["job"][0], query["speaker"][0]
        vendor_job = f"fake-vp-{speaker}"
        await self.enqueue("on_voiceprint", job, speaker, vendor_job, _job_id=f"vp:{speaker}:{vendor_job}")
        return vendor_job

    async def voiceprint_output(self, vendor_job: str) -> str | None:
        return VOICEPRINT

    async def cleanup(self, refs: Refs) -> None:
        pass

    async def aclose(self) -> None:
        pass
