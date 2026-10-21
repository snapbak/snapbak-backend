"""Strategy #1: AssemblyAI Universal-3.5 Pro for words, times, fillers, language and names, pyannoteAI Precision-2
for who spoke when and voiceprints (D10; docs/cloud.md). Hand-written httpx clients, like ai/providers."""

import asyncio
import contextlib
from typing import Any

import httpx

from snapbak_backend.services.storage import Storage
from snapbak_backend.transcription.languages import assemblyai_fields
from snapbak_backend.transcription.merge import Merged, merge
from snapbak_backend.transcription.strategy import (
    JobInput,
    Refs,
    Vendor,
    VendorError,
    VendorRefused,
    VendorStatus,
    VendorUnavailable,
    assemblyai_failure,
)

ASSEMBLYAI = "https://api.assemblyai.com"
PYANNOTE = "https://api.pyannote.ai"
TIMEOUT_S = 30
# Asking again helps for these; any other 4xx is final.
RETRIED = {429, 500, 502, 503, 504}
WEBHOOK_HEADER = "X-Snapbak-Webhook"


async def call(client: httpx.AsyncClient, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """One request; its JSON, or VendorUnavailable (ask again later) or VendorRefused (final)."""
    try:
        response = await client.request(method, path, json=body, timeout=TIMEOUT_S)
    except httpx.HTTPError as error:
        raise VendorUnavailable(f"{client.base_url.host}: {type(error).__name__}") from error
    if response.status_code in RETRIED:
        raise VendorUnavailable(f"{client.base_url.host}: {response.status_code}")
    if response.is_error:
        raise VendorRefused(f"{client.base_url.host}: {response.status_code}")
    try:
        answer = response.json()
    except ValueError as error:
        raise VendorUnavailable(f"{client.base_url.host}: not JSON") from error
    return answer


def assemblyai_request(job: JobInput) -> dict[str, Any]:
    """`speaker_labels` is needed for speaker identification and is used only to map names; identification runs
    without a `speakers` list, which `just test-cloud` checks (RESEARCH.md)."""
    request = {
        "audio_url": job.audio_url,
        **assemblyai_fields(job.language),
        "punctuate": True,
        "format_text": True,
        "disfluencies": True,
        "speaker_labels": True,
        "speech_understanding": {"request": {"speaker_identification": {"speaker_type": "name"}}},
        "webhook_url": job.webhooks["assemblyai"],
        "webhook_auth_header_name": WEBHOOK_HEADER,
        "webhook_auth_header_value": job.webhook_auth,
    }
    return request


def pyannote_request(job: JobInput) -> dict[str, Any]:
    """`exclusive` adds turns with no overlap, which the merge uses; no speaker count is known, so none is sent."""
    request = {"url": job.audio_url, "model": "precision-2", "exclusive": True, "webhook": job.webhooks["pyannote"]}
    return request


def raw_key(job: JobInput) -> str:
    """Where pyannote's diarization output is kept, since pyannoteAI deletes it after 24 h."""
    return f"raw/{job.recording_id}/{job.job_id}/pyannote-diarize.json"


class AssemblyAiPyannoteStrategy:
    name = "assemblyai+pyannote"

    def __init__(self, assemblyai_key: str, pyannote_key: str, storage: Storage) -> None:
        self.assemblyai = httpx.AsyncClient(base_url=ASSEMBLYAI, headers={"authorization": assemblyai_key})
        self.pyannote = httpx.AsyncClient(base_url=PYANNOTE, headers={"Authorization": f"Bearer {pyannote_key}"})
        self.storage = storage

    async def submit(self, job: JobInput, refs: Refs) -> Refs:
        """Both vendors at once, skipping one an earlier try already started. If one fails, the error carries the
        refs of the one that accepted: the retry won't pay for it twice, and a failed job deletes it."""
        made: Refs = {**refs, "language": job.language}
        asks = {}
        if "assemblyai" not in made:
            asks["assemblyai"] = call(self.assemblyai, "POST", "/v2/transcript", assemblyai_request(job))
        if "pyannote" not in made:
            asks["pyannote"] = call(self.pyannote, "POST", "/v1/diarize", pyannote_request(job))
        answers = await asyncio.gather(*asks.values(), return_exceptions=True)
        errors: list[VendorError] = []
        for vendor, answer in zip(asks, answers, strict=True):
            try:
                if isinstance(answer, BaseException):
                    raise answer
                if vendor == "assemblyai":
                    made["assemblyai"] = {"id": answer["id"], "status": "running"}
                else:
                    made["pyannote"] = {"jobId": answer["jobId"], "status": "running", "rawKey": raw_key(job)}
            except VendorError as error:
                errors.append(error)
            except Exception as error:
                # Any other surprise from one vendor is an outage too, so the other's refs aren't lost.
                errors.append(VendorUnavailable(f"{vendor}: {type(error).__name__}"))
        if errors:
            # A refusal is final, so it wins over an outage.
            failed = next((error for error in errors if isinstance(error, VendorRefused)), errors[0])
            failed.refs = made
            raise failed
        return made

    async def status(self, refs: Refs, vendor: Vendor) -> VendorStatus:
        if vendor == "assemblyai":
            transcript = await call(self.assemblyai, "GET", f"/v2/transcript/{refs['assemblyai']['id']}")
            if transcript["status"] == "completed":
                return VendorStatus("done")
            if transcript["status"] == "error":
                reason = assemblyai_failure(transcript.get("error") or "")
                return VendorStatus("failed", reason)
            return VendorStatus("running")
        if await self.storage.get_json(refs["pyannote"]["rawKey"]) is not None:
            return VendorStatus("done")
        job = await call(self.pyannote, "GET", f"/v1/jobs/{refs['pyannote']['jobId']}")
        if job["status"] == "succeeded":
            await self.storage.put_json(refs["pyannote"]["rawKey"], job["output"])
            return VendorStatus("done")
        if job["status"] in ("failed", "canceled"):
            return VendorStatus("failed", "vendor_error")
        return VendorStatus("running")

    async def diarization(self, refs: Refs) -> list[dict[str, Any]]:
        output = await self.storage.get_json(refs["pyannote"]["rawKey"])
        if output is None:
            raise VendorUnavailable("pyannote output not stored yet")
        return output.get("exclusiveDiarization") or []

    async def result(self, refs: Refs) -> Merged:
        transcript = await call(self.assemblyai, "GET", f"/v2/transcript/{refs['assemblyai']['id']}")
        turns = await self.diarization(refs)
        merged = merge(transcript, turns, refs["language"])
        return merged

    async def voiceprint(self, clip_url: str, webhook: str) -> str:
        started = await call(self.pyannote, "POST", "/v1/voiceprint", {"url": clip_url, "webhook": webhook})
        return started["jobId"]

    async def voiceprint_output(self, vendor_job: str) -> str | None:
        job = await call(self.pyannote, "GET", f"/v1/jobs/{vendor_job}")
        if job["status"] != "succeeded":
            return None
        return job["output"]["voiceprint"]

    async def cleanup(self, refs: Refs) -> None:
        """AssemblyAI keeps a transcript until it is deleted; pyannoteAI deletes its output after 24 h itself."""
        transcript = refs.get("assemblyai")
        if transcript is None:
            return
        with contextlib.suppress(VendorError):
            await call(self.assemblyai, "DELETE", f"/v2/transcript/{transcript['id']}")

    async def aclose(self) -> None:
        await self.assemblyai.aclose()
        await self.pyannote.aclose()
