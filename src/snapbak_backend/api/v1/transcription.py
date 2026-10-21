"""POST /v1/recordings/{id}/transcription and the long-poll GET /v1/jobs/{jobId} (D8, D16; docs/cloud.md)."""

import asyncio
import contextlib
import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from snapbak_backend.api import cloud_deps
from snapbak_backend.api.cloud_deps import Caller, Session
from snapbak_backend.api.logged import LoggedRoute
from snapbak_backend.db.models import TranscriptionJob
from snapbak_backend.schemas.cloud import JobResponse, TranscriptionRequest, TranscriptionResponse
from snapbak_backend.services import jobs, recordings
from snapbak_backend.services.cloud import Cloud

router = APIRouter(route_class=LoggedRoute)

FINAL = ("ready", "failed")


@router.post("/v1/recordings/{recording_id}/transcription", status_code=201)
async def create(
    recording_id: UUID, body: TranscriptionRequest, device: Caller, db: Session, request: Request
) -> TranscriptionResponse:
    cloud = cloud_deps.cloud(request)
    recording = await recordings.locked(db, device.id, recording_id)
    job = await jobs.create(db, recording, body.language, body.script, cloud.config.transcription)
    if job.upload_id is not None:
        await jobs.enqueue_start(cloud.redis, job.id)
    return TranscriptionResponse(jobId=job.id, state="queued", version=job.version, needsUpload=job.upload_id is None)


async def read(cloud: Cloud, device_id: UUID, job_id: UUID) -> JobResponse:
    """The job as the app sees it, in a session of its own, so a long-poll holds no connection while it waits."""
    async with cloud.sessions() as db:
        job = await recordings.owned_row(db, device_id, TranscriptionJob, job_id)
    answer = JobResponse(
        jobId=job.id,
        recordingId=job.recording_id,
        state=job.state,
        stage=job.stage,
        failure=job.failure_reason,
        language=job.language_detected,
        version=job.version,
    )
    return answer


@router.get("/v1/jobs/{job_id}")
async def job_state(
    job_id: UUID,
    device: Caller,
    request: Request,
    wait: Annotated[int, Query(ge=0, le=25)] = 0,
    since: Annotated[int, Query(ge=0)] = 0,
) -> JobResponse:
    """Answers at once when the job moved past `since` or is final; else waits up to `wait` seconds for a change, and
    answers the current row either way. A wake-up without a change (a reconnect) waits on for the time left."""
    cloud = cloud_deps.cloud(request)
    deadline = time.monotonic() + wait
    with request.app.state.events.watch(job_id) as changed:
        current = await read(cloud, device.id, job_id)
        while current.version <= since and current.state not in FINAL and time.monotonic() < deadline:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), deadline - time.monotonic())
            # Cleared before the read, so a change after it wakes the next wait at once.
            changed.clear()
            current = await read(cloud, device.id, job_id)
    return current
