"""The end of the 30-day undo window (D28): a trashed meeting's S3 objects are superseded (the bucket's rule deletes
them a day later) and its rows, vectors included, are hard-deleted. Rows newer than the window stay for restore. A
recording that was never a meeting stays soft-deleted for good (Q101, "soft forever")."""

import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, delete, select

from snapbak_backend.ai.chain import elapsed_ms
from snapbak_backend.db.models import (
    CaptionTrack,
    Meeting,
    MeetingChunk,
    MeetingPin,
    Recording,
    Share,
    Speaker,
    TranscriptionJob,
    TranscriptLine,
    Upload,
)
from snapbak_backend.services.cloud import Cloud
from snapbak_backend.services.metrics import task_log

UNDO_DAYS = 30
BATCH = 100


def closed(now: datetime) -> ColumnElement[bool]:
    """The recording's undo window closed (D28): trashed more than UNDO_DAYS ago."""
    return Recording.deleted_at.is_not(None) & (Recording.deleted_at < now - timedelta(days=UNDO_DAYS))


async def expired(cloud: Cloud, now: datetime) -> list[UUID]:
    """The meetings whose undo window closed, oldest first, a batch at a time. Only meetings end their window
    (D28); every other recording stays soft-deleted (Q101)."""
    async with cloud.sessions() as db:
        rows = await db.scalars(
            select(Recording.id)
            .where(closed(now), Recording.id.in_(select(Meeting.recording_id)))
            .order_by(Recording.deleted_at)
            .limit(BATCH)
        )
        found = list(rows)
    return found


async def keys_of(cloud: Cloud, recording_id: UUID) -> list[str]:
    """Every S3 object the recording owns: its uploads, its shares' video and poster, and the vendors' raw and clip
    folders."""
    keys: list[str] = []
    async with cloud.sessions() as db:
        uploads = await db.scalars(select(Upload.s3_key).where(Upload.recording_id == recording_id))
        keys.extend(key for key in uploads if key)
        shares = await db.execute(select(Share.s3_key, Share.poster_s3_key).where(Share.recording_id == recording_id))
        keys.extend(key for row in shares.all() for key in row if key)
        jobs = await db.scalars(select(TranscriptionJob.id).where(TranscriptionJob.recording_id == recording_id))
    for prefix in [f"raw/{recording_id}/"] + [f"clips/{job_id}/" for job_id in jobs]:
        keys.extend(await cloud.storage.keys(prefix))
    return keys


async def purge_one(cloud: Cloud, recording_id: UUID, now: datetime) -> bool:
    """One recording gone for good, in one transaction under its lock: still trashed past the window (a restore
    that won the lock keeps it), its S3 objects superseded, then its rows deleted, children first. Nothing is
    deleted when superseding fails, so the next pass tries again; True when it was purged."""
    keys = await keys_of(cloud, recording_id)
    meetings = select(Meeting.id).where(Meeting.recording_id == recording_id)
    async with cloud.sessions() as db:
        found = await db.scalar(select(Recording.id).where(Recording.id == recording_id, closed(now)).with_for_update())
        if found is None:
            return False
        if keys:
            await cloud.storage.supersede(keys)
        await db.execute(delete(MeetingChunk).where(MeetingChunk.meeting_id.in_(meetings)))
        await db.execute(delete(MeetingPin).where(MeetingPin.meeting_id.in_(meetings)))
        for model in (Meeting, TranscriptLine, CaptionTrack, Speaker, TranscriptionJob, Upload, Share):
            await db.execute(delete(model).where(model.recording_id == recording_id))
        await db.execute(delete(Recording).where(Recording.id == recording_id))
        await db.commit()
    return True


async def purge_all(cloud: Cloud) -> int:
    """Batches until the expired are gone, so a day with more than a batch carries nothing over; how many. A
    batch with a recording that could not go ends the pass, which tries again the next day."""
    total = 0
    while True:
        done = await purge_once(cloud)
        total += done
        if done < BATCH:
            return total


async def purge_once(cloud: Cloud) -> int:
    """One pass over the expired meetings; how many went. One that fails is logged and left for the next pass, so
    it never blocks the rest. Superseded objects answer until the bucket's rule takes them a day later."""
    now = datetime.now(UTC)
    done = 0
    for recording_id in await expired(cloud, now):
        started = time.monotonic()
        try:
            purged = await purge_one(cloud, recording_id, now)
        except Exception:
            task_log("purge", str(recording_id), "-", elapsed_ms(started), "failed")
            continue
        task_log("purge", str(recording_id), "-", elapsed_ms(started), "purged" if purged else "restored")
        done += purged
    return done
