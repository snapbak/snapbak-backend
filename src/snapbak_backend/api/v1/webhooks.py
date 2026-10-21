"""The vendors' webhooks (docs/cloud.md). No device auth: AssemblyAI proves itself with the per-job HMAC Snapbak
gave it, pyannoteAI with its signature. A route checks, keeps what would expire, enqueues the task and answers 204
at once: AssemblyAI needs a 2xx within 10 s. A stray delivery answers 204 too, so the vendor stops retrying."""

import json
import time
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from sqlalchemy import select

from snapbak_backend.api import cloud_deps
from snapbak_backend.db.models import Speaker, TranscriptionJob
from snapbak_backend.services.failures import CloudFailure
from snapbak_backend.services.metrics import task_log
from snapbak_backend.services.signatures import assemblyai_ok, pyannote_ok

router = APIRouter()

NO_CONTENT = 204


def parsed(job: str) -> UUID | None:
    try:
        return UUID(job)
    except ValueError:
        return None


def answered(route: str, job: str, vendor: str, started: float, verdict: str) -> Response:
    task_log(route, job, vendor, int((time.monotonic() - started) * 1000), verdict)
    return Response(status_code=NO_CONTENT)


@router.post("/v1/webhooks/assemblyai")
async def assemblyai(
    request: Request,
    job: Annotated[str, Query()],
    signed: Annotated[str, Header(alias="X-Snapbak-Webhook")] = "",
) -> Response:
    started = time.monotonic()
    cloud = cloud_deps.cloud(request)
    if not assemblyai_ok(cloud.config.webhook_secret, job, signed):
        raise CloudFailure(401, "unauthorized")
    body: dict[str, Any] = json.loads(await request.body() or b"{}")
    job_id = parsed(job)
    async with cloud.sessions() as db:
        found = None if job_id is None else await db.get(TranscriptionJob, job_id)
    refs = {} if found is None else found.vendor_refs.get("assemblyai", {})
    if found is None or refs.get("id") != body.get("transcript_id"):
        return answered("webhook", job, "assemblyai", started, "stray")
    await cloud.redis.enqueue_job("on_vendor_webhook", job_id, "assemblyai", _job_id=f"hook:assemblyai:{job_id}")
    return answered("webhook", job, "assemblyai", started, "enqueued")


@router.post("/v1/webhooks/pyannote")
async def pyannote(
    request: Request,
    job: Annotated[str, Query()],
    kind: Annotated[Literal["diarize", "voiceprint"], Query()],
    speaker: Annotated[UUID | None, Query()] = None,
    timestamp: Annotated[str, Header(alias="X-Request-Timestamp")] = "",
    signature: Annotated[str, Header(alias="X-Signature")] = "",
) -> Response:
    started = time.monotonic()
    cloud = cloud_deps.cloud(request)
    raw = await request.body()
    if not pyannote_ok(cloud.config.pyannote_webhook_secret, timestamp, raw, signature):
        raise CloudFailure(401, "unauthorized")
    body: dict[str, Any] = json.loads(raw or b"{}")
    job_id = parsed(job)
    async with cloud.sessions() as db:
        found = None if job_id is None else await db.get(TranscriptionJob, job_id)
        owner = None
        if found is not None and speaker is not None:
            owner = await db.scalar(
                select(Speaker.id).where(Speaker.id == speaker, Speaker.recording_id == found.recording_id)
            )
    if found is None:
        return answered("webhook", job, "pyannote", started, "stray")
    if kind == "voiceprint":
        # Only the voiceprint job made for this speaker counts, so a signed delivery can't land on another speaker.
        # Before that job is recorded (the webhook beat it), on_voiceprint waits for the record and compares then.
        made = found.vendor_refs.get("voiceprints", {}).get(str(speaker))
        vendor_job = body.get("jobId")
        if owner is None or not vendor_job or made not in (None, vendor_job):
            return answered("webhook", job, "pyannote", started, "stray")
        await cloud.redis.enqueue_job("on_voiceprint", job, str(owner), vendor_job, _job_id=f"vp:{owner}:{vendor_job}")
        return answered("webhook", job, "pyannote", started, "enqueued")
    refs = found.vendor_refs.get("pyannote", {})
    if refs.get("jobId") != body.get("jobId"):
        return answered("webhook", job, "pyannote", started, "stray")
    if body.get("output") is not None:
        # pyannoteAI deletes its output after 24 h; keep it before answering.
        await cloud.storage.put_json(refs["rawKey"], body["output"])
    await cloud.redis.enqueue_job("on_vendor_webhook", job_id, "pyannote", _job_id=f"hook:pyannote:{job_id}")
    return answered("webhook", job, "pyannote", started, "enqueued")
