"""S3 multipart uploads: start, fresh part URLs, complete (CLD-02, D9, Q41; docs/cloud.md). The app PUTs the parts
straight to S3 with the presigned URLs; complete is its completion webhook. `kind: "audio"` feeds the transcript;
`kind: "share"` is a share's next video, in the shares bucket (12 LLD)."""

import contextlib
import math
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from snapbak_backend.api import cloud_deps
from snapbak_backend.api.cloud_deps import Caller, Session
from snapbak_backend.api.logged import LoggedRoute
from snapbak_backend.api.v1.shares import ref
from snapbak_backend.config.settings import SHARE_MAX_BYTES
from snapbak_backend.db.models import Share, TranscriptionJob, Upload
from snapbak_backend.schemas.cloud import (
    AnyUpload,
    CompleteRequest,
    CompleteResponse,
    PartsRequest,
    PartUrl,
    ShareCompleteResponse,
    ShareUploadRequest,
    UploadResponse,
)
from snapbak_backend.services import jobs, recordings, shares
from snapbak_backend.services.cloud import Cloud
from snapbak_backend.services.events import publish
from snapbak_backend.services.failures import CloudFailure
from snapbak_backend.services.storage import BadParts, NoSuchUpload, Storage

router = APIRouter(route_class=LoggedRoute)

# 8 MiB parts cover 80 GB within S3's 10,000 parts, so the size never changes.
PART_SIZE = 8 * 1024 * 1024
# Part URLs last an hour: URLs signed with rotating task credentials die with them anyway (RESEARCH, S3).
URL_TTL_S = 3600
# Only refuses nonsense sizes: 200 MB is about 9 h at 48 kbps, far past the 2 h limit.
AUDIO_CAP = 200_000_000


def urls(storage: Storage, upload: Upload, numbers: list[int]) -> UploadResponse:
    expires = datetime.now(UTC) + timedelta(seconds=URL_TTL_S)
    signed = [PartUrl(n=n, url=storage.presign_part(upload.s3_key, upload.upload_id, n, URL_TTL_S)) for n in numbers]
    return UploadResponse(uploadId=upload.id, partSize=upload.part_size, expiresAt=expires, urls=signed)


@router.post("/v1/recordings/{recording_id}/uploads", status_code=201)
async def start(recording_id: UUID, body: AnyUpload, device: Caller, db: Session, request: Request) -> UploadResponse:
    cap = SHARE_MAX_BYTES if body.kind == "share" else AUDIO_CAP
    if body.bytes > cap:
        raise CloudFailure(413, "upload_too_large")
    await recordings.locked(db, device.id, recording_id)
    cloud = cloud_deps.cloud(request)
    upload_id = uuid.uuid4()
    upload = Upload(
        id=upload_id,
        recording_id=recording_id,
        kind=body.kind,
        part_size=PART_SIZE,
        bytes=body.bytes,
        content_type=body.contentType,
    )
    cache = None
    if isinstance(body, ShareUploadRequest):
        share = await shares.for_upload(db, recording_id, body.shareId)
        await shares.abort_open_uploads(db, cloud.storage, share)
        upload.share_id = share.id
        upload.duration_ms, upload.width, upload.height = body.durationMs, body.width, body.height
        upload.s3_key = shares.key(share.id, share.version + 1, "video.mp4")
        cache = shares.IMMUTABLE
    else:
        upload.s3_key = f"audio/{recording_id}/{upload_id}.m4a"
    storage = cloud.storage
    upload.upload_id = await storage.create_multipart(upload.s3_key, body.contentType, cache)
    db.add(upload)
    await db.commit()
    count = math.ceil(body.bytes / PART_SIZE)
    every = list(range(1, count + 1))
    answer = urls(storage, upload, every)
    return answer


@router.post("/v1/uploads/{upload_id}/parts")
async def parts(upload_id: UUID, body: PartsRequest, device: Caller, db: Session, request: Request) -> UploadResponse:
    """Fresh URLs for the parts still missing, on resume or after a 403."""
    upload = await recordings.owned_row(db, device.id, Upload, upload_id)
    if upload.state == "aborted":
        raise CloudFailure(409, "upload_state")
    count = math.ceil(upload.bytes / upload.part_size)
    if any(not 1 <= n <= count for n in body.parts):
        raise CloudFailure(422, "invalid")
    asked = sorted(set(body.parts))
    answer = urls(cloud_deps.cloud(request).storage, upload, asked)
    return answer


@router.post("/v1/uploads/{upload_id}/complete")
async def complete(
    upload_id: UUID, body: CompleteRequest, device: Caller, db: Session, request: Request
) -> CompleteResponse | ShareCompleteResponse:
    # The recording's lock first, then the upload read under it: a newer start may have aborted it, or another
    # complete finished it. A trashed recording's upload is not found.
    await recordings.lock(db, select(Upload.recording_id).where(Upload.id == upload_id).scalar_subquery())
    upload = await recordings.owned_row(db, device.id, Upload, upload_id)
    cloud = cloud_deps.cloud(request)
    share = None if upload.kind != "share" else await shares.of_upload(db, upload)
    if upload.state == "aborted":
        raise CloudFailure(409, "upload_state")
    if upload.state == "complete":
        again = await answered(db, cloud, upload, share)
        return again
    storage = cloud.storage
    listed = [(part.n, part.etag) for part in body.parts]
    # An earlier complete may have succeeded and lost its answer; the object's size tells.
    try:
        with contextlib.suppress(NoSuchUpload):
            await storage.complete(upload.s3_key, upload.upload_id, listed)
    except BadParts as error:
        # The upload stays open: the app can complete again with the right ETags, or send the parts again.
        raise CloudFailure(422, "invalid") from error
    size = await storage.size(upload.s3_key)
    if size != upload.bytes:
        upload.state = "aborted"
        await db.commit()
        raise CloudFailure(422, "upload_size_mismatch")
    upload.parts = [{"n": n, "etag": etag} for n, etag in sorted(listed)]
    upload.state = "complete"
    upload.completed_at = func.now()
    if share is not None:
        shared = await share_completed(db, cloud, upload, share)
        return shared
    job = await db.scalar(
        select(TranscriptionJob).where(
            TranscriptionJob.recording_id == upload.recording_id,
            TranscriptionJob.state == "queued",
            TranscriptionJob.upload_id.is_(None),
            TranscriptionJob.deleted_at.is_(None),
        )
    )
    if job is not None and not await jobs.advance(db, job, upload_id=upload.id):
        job = None
    await db.commit()
    if job is not None:
        await publish(cloud.redis, job.id, job.version)
        await jobs.enqueue_start(cloud.redis, job.id)
    return CompleteResponse(uploadId=upload.id, state="complete", jobId=None if job is None else job.id)


async def share_completed(db: Session, cloud: Cloud, upload: Upload, share: Share) -> ShareCompleteResponse:
    """Points the locked share at this version, then tags what it replaced for the bucket rule to delete."""
    poster = shares.key(share.id, shares.version_of(upload), "poster.jpg")
    has_poster = await cloud.storage.size(poster) is not None
    shares.adopt(share, upload, has_poster)
    await db.commit()
    await shares.retire(cloud.storage, share)
    shared = share_answer(upload, share, cloud)
    return shared


def share_answer(upload: Upload, share: Share, cloud: Cloud) -> ShareCompleteResponse:
    made = ShareCompleteResponse(uploadId=upload.id, state="complete", share=ref(share, cloud.config.share_url))
    return made


async def answered(
    db: Session, cloud: Cloud, upload: Upload, share: Share | None
) -> CompleteResponse | ShareCompleteResponse:
    """A retried complete answers as the first one did: the share it updated, after tagging again what the first
    may have failed to; or the job this audio feeds."""
    if share is not None:
        await db.commit()
        await shares.retire(cloud.storage, share)
        shared = share_answer(upload, share, cloud)
        return shared
    job_id = await db.scalar(select(TranscriptionJob.id).where(TranscriptionJob.upload_id == upload.id))
    fed = CompleteResponse(uploadId=upload.id, state="complete", jobId=job_id)
    return fed
