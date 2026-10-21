"""Transcription job state changes (docs/cloud.md, "Job state transitions"). Every change bumps `version` through a
conditional update on the version the writer read, so of two writers only one wins; the loser reads again. After
the commit the writer publishes the new version."""

import math
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from arq import ArqRedis
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from snapbak_backend.config.settings import MAX_JOB_SECONDS
from snapbak_backend.db.models import LIVE_STATES, Failure, Recording, Script, TranscriptionJob, Upload
from snapbak_backend.schemas.cloud import UTC_FORMAT
from snapbak_backend.services import usage
from snapbak_backend.services.failures import CloudFailure
from snapbak_backend.transcription.languages import accepted


async def advance(session: AsyncSession, job: TranscriptionJob, **values: Any) -> bool:
    """Applies `values` and bumps the version if nobody changed the job since it was read; the caller commits.
    `job` is updated in memory to match."""
    changed = await session.execute(
        update(TranscriptionJob)
        .where(TranscriptionJob.id == job.id, TranscriptionJob.version == job.version)
        .values(**values, version=TranscriptionJob.version + 1, updated_at=func.now())
        .returning(TranscriptionJob.id)
        .execution_options(synchronize_session=False)
    )
    won = changed.first() is not None
    if won:
        # The row already holds these; mirror them without marking the object for another UPDATE.
        for name, value in {**values, "version": job.version + 1}.items():
            set_committed_value(job, name, value)
    return won


async def fail(session: AsyncSession, job: TranscriptionJob, reason: Failure, **values: Any) -> bool:
    """`failed` with `reason` (and any other `values`), and the job's minutes back to its creation day, in the
    caller's transaction."""
    won = await advance(session, job, state="failed", stage=None, failure_reason=reason, **values)
    if won:
        await usage.refund(session, job)
    return won


async def enqueue_start(redis: ArqRedis, job_id: UUID) -> None:
    await redis.enqueue_job("start_transcription", job_id, _job_id=f"start:{job_id}")


async def create(
    session: AsyncSession, recording: Recording, language: str, script: Script, strategy: str
) -> TranscriptionJob:
    """A queued job for the recording, in one transaction with its reserved minutes (D8, CLD-03); the caller holds the
    recording's lock. A job made on audio already uploaded (a retry) comes back with `upload_id` set, for the caller
    to start after the commit."""
    recording_id = recording.id
    if not accepted(language):
        raise CloudFailure(422, "unsupported_language")
    if recording.duration_ms > MAX_JOB_SECONDS * 1000:
        raise CloudFailure(413, "too_long")
    seconds = math.ceil(recording.duration_ms / 1000)
    if not await usage.reserve(session, recording.device_id, seconds):
        await session.rollback()
        raise CloudFailure(429, "daily_limit", resetsAt=usage.resets_at(datetime.now(UTC)).strftime(UTC_FORMAT))
    earlier = await session.scalar(
        select(func.count()).select_from(TranscriptionJob).where(TranscriptionJob.recording_id == recording.id)
    )
    uploaded = await session.scalar(
        select(Upload.id).where(
            Upload.recording_id == recording.id,
            Upload.kind == "audio",
            Upload.state == "complete",
            Upload.deleted_at.is_(None),
        )
    )
    job = TranscriptionJob(
        id=uuid.uuid4(),
        recording_id=recording.id,
        upload_id=uploaded,
        state="queued",
        language_requested=language,
        script=script,
        strategy=strategy,
        vendor_refs={},
        attempt=(earlier or 0) + 1,
        billed_seconds=seconds,
        version=1,
    )
    session.add(job)
    try:
        await session.commit()
    except IntegrityError:
        # The unique partial index: a live or ready job exists. The rollback undoes the reservation too.
        await session.rollback()
        existing = await live_job(session, recording_id)
        ref = {"jobId": str(existing.id), "state": existing.state, "version": existing.version}
        raise CloudFailure(409, "job_exists", job=ref) from None
    return job


async def live_job(session: AsyncSession, recording_id: UUID) -> TranscriptionJob:
    found = await session.scalar(
        select(TranscriptionJob).where(
            TranscriptionJob.recording_id == recording_id, TranscriptionJob.state.in_(LIVE_STATES)
        )
    )
    if found is None:
        # The job that won the race was failed in between; a retry from the app makes a fresh one.
        raise CloudFailure(500, "internal")
    return found
