"""A recording's share link: its slug, the device's limits, its lifecycle, and pointing it at a completed video
(SHR-01, CLD-03, 12 LLD; docs/cloud.md)."""

import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from snapbak_backend.config.settings import SHARES_PER_DEVICE
from snapbak_backend.db.models import Device, Meeting, Recording, Share, Upload
from snapbak_backend.schemas.cloud import Expiry, ShareRequest
from snapbak_backend.services import recordings
from snapbak_backend.services.failures import CloudFailure
from snapbak_backend.services.storage import Storage

# Only these reach the database: anything else on the page route is 404 without a read.
SLUG = re.compile(r"[A-Za-z0-9_-]{16,64}")
SLUG_TRIES = 3
DAYS = {"7d": 7, "30d": 30}
# Each version has its own key, so its bytes never change (12 LLD).
IMMUTABLE = "public, max-age=31536000, immutable"
# Signed URLs, for the poster PUT and the page's GETs, last an hour: task credentials rotate anyway (12 LLD).
SIGNED_S = 3600


def new_slug() -> str:
    """96 random bits, 16 base64url characters: the link's only secret."""
    slug = secrets.token_urlsafe(12)
    return slug


def is_slug(said: str) -> bool:
    return SLUG.fullmatch(said) is not None


def link(base: str, slug: str) -> str:
    return f"{base}/v/{slug}"


def key(share_id: UUID, version: int, name: str) -> str:
    """`shares/{shareId}/v{n}/video.mp4` or `poster.jpg`."""
    return f"shares/{share_id}/v{version}/{name}"


def version_of(upload: Upload) -> int:
    """The version a share upload makes, from its key."""
    version = version_in(upload.s3_key)
    return version


def version_in(name: str) -> int:
    """The version in a key `shares/{shareId}/v{n}/...`."""
    folder = name.split("/")[2]
    version = int(folder.removeprefix("v"))
    return version


def expiry(said: Expiry) -> datetime | None:
    """From the server's clock, so the Mac's clock never matters."""
    if said == "never":
        return None
    later = datetime.now(UTC) + timedelta(days=DAYS[said])
    return later


async def live_of(session: AsyncSession, recording_id: UUID) -> Share | None:
    """The recording's share that is not deleted; there is at most one."""
    found = await session.scalar(select(Share).where(Share.recording_id == recording_id, Share.deleted_at.is_(None)))
    return found


async def open_share(
    session: AsyncSession, device_id: UUID, recording_id: UUID, body: ShareRequest
) -> tuple[Share, bool]:
    """The recording's share, made if it has none, with the title of the video about to be uploaded; and whether it
    was made."""
    # The recording's lock first, as every write under a recording takes; then the device's, so one device's
    # creates take turns and the 100-share count holds across its recordings. In this order a create waiting for a
    # busy recording holds nothing of the device's. FOR NO KEY UPDATE, as the recording's lock: no key changes.
    recording = await recordings.locked(session, device_id, recording_id)
    await session.execute(select(Device.id).where(Device.id == device_id).with_for_update(key_share=True))
    share = await live_of(session, recording_id)
    created = share is None
    if share is None:
        share = await insert_share(session, device_id, recording, body)
    share.title = body.title
    # A recording promoted to a meeting after its first share shows the meeting from the next share on.
    share.type = await share_type(session, recording)
    return share, created


async def live_count(session: AsyncSession, device_id: UUID) -> int:
    """The device's shares that are not deleted, across its recordings."""
    count = await session.scalar(
        select(func.count())
        .select_from(Share)
        .join(Recording, Recording.id == Share.recording_id)
        .where(Recording.device_id == device_id, Share.deleted_at.is_(None))
    )
    return count or 0


async def share_type(session: AsyncSession, recording: Recording) -> Literal["video", "audio", "meeting"]:
    """What a share of this recording shows (D20): the meeting's minutes and transcript when it has a live meeting;
    else its audio or video. Derived here, never sent by the app."""
    meeting = await session.scalar(
        select(Meeting.id).where(Meeting.recording_id == recording.id, Meeting.deleted_at.is_(None))
    )
    if meeting is not None:
        return "meeting"
    if recording.recording_type == "audio":
        return "audio"
    return "video"


async def insert_share(session: AsyncSession, device_id: UUID, recording: Recording, body: ShareRequest) -> Share:
    """A new `uploading` share under the device's limit; a slug already taken is drawn again. An audio recording's
    share plays the recording's own audio upload (D12, D16), so it has nothing to upload and is live at once, as
    version 1; its page waits on the audio when that isn't complete yet."""
    count = await live_count(session, device_id)
    if count >= SHARES_PER_DEVICE:
        raise CloudFailure(429, "share_limit")
    values = {
        "id": uuid.uuid4(),
        "recording_id": recording.id,
        "title": body.title,
        "expires_at": expiry(body.expiresIn),
        "type": await share_type(session, recording),
    }
    if recording.recording_type == "audio":
        values.update(state="live", version=1, duration_ms=recording.duration_ms)
    for _ in range(SLUG_TRIES):
        made = insert(Share).values(slug=new_slug(), **values).on_conflict_do_nothing().returning(Share)
        share = await session.scalar(made)
        if share is not None:
            return share
    raise RuntimeError(f"{SLUG_TRIES} slugs in a row were taken")


async def owned(session: AsyncSession, device_id: UUID, slug: str, deleted_too: bool = False) -> Share:
    """The device's share by slug; another device's, an unknown or (unless `deleted_too`) a deleted one is 404."""
    query = (
        select(Share)
        .join(Recording, Recording.id == Share.recording_id)
        .where(Share.slug == slug, Recording.device_id == device_id)
    )
    if not deleted_too:
        query = query.where(Share.deleted_at.is_(None))
    found = await session.scalar(query)
    if found is None:
        raise CloudFailure(404, "not_found")
    return found


async def for_upload(session: AsyncSession, recording_id: UUID, share_id: UUID) -> Share:
    """The recording's share that a video upload names, or 404. The caller holds the recording's lock, so two
    starts never pick the same key."""
    found = await session.scalar(
        select(Share).where(Share.id == share_id, Share.recording_id == recording_id, Share.deleted_at.is_(None))
    )
    if found is None:
        raise CloudFailure(404, "not_found")
    return found


async def abort_open_uploads(session: AsyncSession, storage: Storage, share: Share) -> None:
    """Aborts the share's uploads still open, so of two re-exports the later one wins (12 LLD). The recording and
    kind let the lookup use the `uploads_recording` index."""
    still_open = await session.scalars(
        select(Upload).where(
            Upload.recording_id == share.recording_id,
            Upload.kind == "share",
            Upload.state == "uploading",
            Upload.share_id == share.id,
        )
    )
    for upload in still_open:
        upload.state = "aborted"
        await storage.abort(upload.s3_key, upload.upload_id)


async def of_upload(session: AsyncSession, upload: Upload) -> Share:
    """The share a video upload belongs to, read under the recording's lock; a deleted share is 404, so a late
    complete can't revive it."""
    share = await session.scalar(
        select(Share)
        .where(Share.id == upload.share_id, Share.deleted_at.is_(None))
        .execution_options(populate_existing=True)
    )
    if share is None:
        raise CloudFailure(404, "not_found")
    return share


def adopt(share: Share, upload: Upload, has_poster: bool) -> None:
    """Points the share at its completed upload's version, under the recording's lock. The poster moves only if it
    was PUT, and a disabled share stays disabled. Since writers of one recording take turns, no complete reaches
    here with a version the share already has or passed; the version check stays as a guard, so such an upload
    would change nothing."""
    version = version_of(upload)
    if version <= share.version:
        return
    if has_poster:
        share.poster_s3_key = key(share.id, version, "poster.jpg")
    share.s3_key = upload.s3_key
    share.version = version
    share.video_bytes = upload.bytes
    share.duration_ms = upload.duration_ms
    share.width = upload.width
    share.height = upload.height
    if share.state == "uploading":
        share.state = "live"


async def retire(storage: Storage, share: Share) -> None:
    """Tags `superseded=true` every object of a version older than the share's but the poster it still shows, for
    the bucket rule to delete. Idempotent, so a retried complete finishes what a failed one left. A newer version's
    poster, PUT before its video completes, is never touched."""
    keys = await storage.keys(f"shares/{share.id}/")
    older = [name for name in keys if version_in(name) < share.version and name != share.poster_s3_key]
    await storage.supersede(older)
