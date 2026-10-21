"""The share routes: create or update, read, turn off and on, expiry, delete (SHR-01, CLD-03, CLD-04; 12 LLD;
docs/cloud.md)."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import func

from snapbak_backend.api import cloud_deps
from snapbak_backend.api.cloud_deps import Caller, Session
from snapbak_backend.api.logged import LoggedRoute
from snapbak_backend.config.settings import SHARE_MAX_BYTES
from snapbak_backend.db.models import Share, page_state
from snapbak_backend.schemas.cloud import ShareRef, ShareRequest, ShareResponse, ShareUpdateRequest
from snapbak_backend.services import recordings, shares
from snapbak_backend.services.failures import CloudFailure

router = APIRouter(route_class=LoggedRoute)


def ref(share: Share, base: str) -> ShareRef:
    url = shares.link(base, share.slug)
    made = ShareRef(shareId=share.id, slug=share.slug, url=url, state=share.state, version=share.version)
    return made


def answer(share: Share, base: str, poster_upload_url: str | None = None) -> ShareResponse:
    """The share as every share route answers it; `posterUploadUrl` only from create."""
    named = ref(share, base).model_dump()
    state = page_state(share, datetime.now(UTC))
    made = ShareResponse(**named, pageState=state, expiresAt=share.expires_at, posterUploadUrl=poster_upload_url)
    return made


@router.post("/v1/recordings/{recording_id}/shares", status_code=201)
async def create(
    recording_id: UUID, body: ShareRequest, device: Caller, db: Session, request: Request, response: Response
) -> ShareResponse:
    """201 with a new slug, or 200 with the recording's share, ready for the next version's poster and video."""
    if body.bytes > SHARE_MAX_BYTES:
        raise CloudFailure(413, "upload_too_large")
    share, created = await shares.open_share(db, device.id, recording_id, body)
    await db.commit()
    cloud = cloud_deps.cloud(request)
    poster = shares.key(share.id, share.version + 1, "poster.jpg")
    upload_url = cloud.storage.presign_put(poster, "image/jpeg", shares.SIGNED_S)
    response.status_code = 201 if created else 200
    made = answer(share, cloud.config.share_url, upload_url)
    return made


@router.get("/v1/recordings/{recording_id}/shares")
async def read(recording_id: UUID, device: Caller, db: Session, request: Request) -> ShareResponse:
    await recordings.owned(db, device.id, recording_id)
    share = await shares.live_of(db, recording_id)
    if share is None:
        raise CloudFailure(404, "not_found")
    found = answer(share, cloud_deps.cloud(request).config.share_url)
    return found


@router.patch("/v1/shares/{slug}")
async def update(slug: str, body: ShareUpdateRequest, device: Caller, db: Session, request: Request) -> ShareResponse:
    """Turns the link off or on, or sets its expiry from now; a share with no video yet can't be turned on."""
    share = await shares.owned(db, device.id, slug)
    await recordings.lock(db, share.recording_id)
    share = await shares.owned(db, device.id, slug)
    await db.refresh(share)
    if body.state == "live" and share.version == 0:
        raise CloudFailure(422, "invalid")
    if body.state is not None:
        share.state = body.state
    if body.expiresIn is not None:
        share.expires_at = shares.expiry(body.expiresIn)
    await db.commit()
    changed = answer(share, cloud_deps.cloud(request).config.share_url)
    return changed


@router.delete("/v1/shares/{slug}", status_code=204)
async def delete(slug: str, device: Caller, db: Session) -> None:
    """Soft delete; the S3 objects stay (Q101). A second call also answers 204."""
    share = await shares.owned(db, device.id, slug, deleted_too=True)
    await recordings.lock(db, share.recording_id)
    await db.refresh(share)
    if share.deleted_at is None:
        share.deleted_at = func.now()
        share.state = "disabled"
        await db.commit()
