"""A device's recordings: ownership and the soft delete (CLD-04; docs/cloud.md)."""

from uuid import UUID

from sqlalchemy import ScalarSelect, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

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
from snapbak_backend.services import jobs, retention
from snapbak_backend.services.failures import CloudFailure

# Rows under a recording that its soft delete marks too.
UNDER = (Upload, TranscriptionJob, Speaker, TranscriptLine, CaptionTrack, Meeting)


async def owned(session: AsyncSession, device_id: UUID, recording_id: UUID) -> Recording:
    """The device's live recording; another device's, a deleted or an unknown one is 404, so a guessed ID learns
    nothing."""
    found = await session.scalar(
        select(Recording).where(
            Recording.id == recording_id, Recording.device_id == device_id, Recording.deleted_at.is_(None)
        )
    )
    if found is None:
        raise CloudFailure(404, "not_found")
    return found


async def lock(session: AsyncSession, recording_id: UUID | ScalarSelect[UUID]) -> Recording:
    """The recording's row, locked and read fresh: the one lock every write under a recording takes first (the
    recording is the aggregate root; docs/cloud.md, Locks). FOR NO KEY UPDATE: no write changes the recording's key,
    and the key checks of rows that point at it don't wait. Writers of one recording take turns, so no two of them
    wait on each other's rows; the caller checks `deleted_at` after it. `recording_id` may be a subquery, such as
    an upload's recording, so the lock costs no extra read."""
    found = await session.scalar(
        select(Recording)
        .where(Recording.id == recording_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if found is None:
        raise CloudFailure(404, "not_found")
    return found


async def locked(session: AsyncSession, device_id: UUID, recording_id: UUID) -> Recording:
    """The device's live recording, locked (see `lock`); another device's, a trashed or an unknown one is 404."""
    found = await lock(session, recording_id)
    if found.device_id != device_id or found.deleted_at is not None:
        raise CloudFailure(404, "not_found")
    return found


async def owned_row[Row: (Upload, TranscriptionJob, Speaker, TranscriptLine)](
    session: AsyncSession, device_id: UUID, model: type[Row], row_id: UUID
) -> Row:
    """A live row under one of the device's live recordings, or 404 as for `owned`."""
    found = await session.scalar(
        select(model)
        .join(Recording, Recording.id == model.recording_id)
        .where(
            model.id == row_id,
            model.deleted_at.is_(None),
            Recording.device_id == device_id,
            Recording.deleted_at.is_(None),
        )
    )
    if found is None:
        raise CloudFailure(404, "not_found")
    return found


async def live_lines(session: AsyncSession, recording_id: UUID) -> list[TranscriptLine]:
    """The recording's lines that aren't deleted, in order."""
    lines = await session.scalars(
        select(TranscriptLine)
        .where(TranscriptLine.recording_id == recording_id, TranscriptLine.deleted_at.is_(None))
        .order_by(TranscriptLine.idx)
    )
    return list(lines)


async def speakers_of(session: AsyncSession, recording_id: UUID) -> dict[UUID, Speaker]:
    """The recording's live speakers by ID."""
    rows = await session.scalars(
        select(Speaker).where(Speaker.recording_id == recording_id, Speaker.deleted_at.is_(None))
    )
    return {speaker.id: speaker for speaker in rows}


async def soft_delete(session: AsyncSession, recording_id: UUID) -> TranscriptionJob | None:
    """Marks the recording and every row under it in the caller's transaction; its share is disabled so the page
    answers 404. S3 objects stay (Q15). A job still queued fails `internal` here and gives its minutes back today;
    if a submit is waiting to retry, its next try deletes the vendor job that already accepted. A job already
    processing sees the mark and fails itself at its merge, deleting the vendors' copies. The failed job is
    returned, for the caller to publish after the commit. The caller holds the recording's lock (`lock`)."""
    live_share = Share.recording_id == recording_id, Share.deleted_at.is_(None)
    waiting = await session.scalar(
        select(TranscriptionJob).where(
            TranscriptionJob.recording_id == recording_id,
            TranscriptionJob.state == "queued",
            TranscriptionJob.deleted_at.is_(None),
        )
    )
    failed = waiting if waiting is not None and await jobs.fail(session, waiting, "internal") else None
    now = func.now()
    await session.execute(update(Recording).where(Recording.id == recording_id).values(deleted_at=now))
    for model in UNDER:
        live = model.recording_id == recording_id, model.deleted_at.is_(None)
        await session.execute(update(model).where(*live).values(deleted_at=now))
    # A meeting's own rows hang off its meeting_id, not the recording.
    meeting_ids = select(Meeting.id).where(Meeting.recording_id == recording_id)
    for model in (MeetingPin, MeetingChunk):
        await session.execute(
            update(model).where(model.meeting_id.in_(meeting_ids), model.deleted_at.is_(None)).values(deleted_at=now)
        )
    await session.execute(update(Share).where(*live_share).values(deleted_at=now, state="disabled"))
    await retention.on_recording_deleted(session, recording_id)
    return failed


async def restore(session: AsyncSession, recording: Recording) -> None:
    """Undoes `soft_delete` inside the undo window (D28): every row it marked, by the one time it marked them with,
    is live again; a row deleted on its own before stays deleted. The share stays turned off, and a job the trash
    failed stays failed. The caller holds the recording's lock and commits."""
    marked = recording.deleted_at
    meeting_ids = select(Meeting.id).where(Meeting.recording_id == recording.id)
    for model in (*UNDER, Share):
        trashed = model.recording_id == recording.id, model.deleted_at == marked
        await session.execute(update(model).where(*trashed).values(deleted_at=None))
    for model in (MeetingPin, MeetingChunk):
        trashed = model.meeting_id.in_(meeting_ids), model.deleted_at == marked
        await session.execute(update(model).where(*trashed).values(deleted_at=None))
    await session.execute(update(Recording).where(Recording.id == recording.id).values(deleted_at=None))


def speaker_of(speaker: Speaker | None) -> str:
    """A speaker as the minutes, Ask and the share page name them: their chosen name, else their label."""
    if speaker is None:
        return ""
    name = speaker.name or speaker.label
    return name
