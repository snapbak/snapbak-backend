"""PUT and DELETE /v1/recordings/{id} (Q88, CLD-04; docs/cloud.md)."""

from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import literal_column
from sqlalchemy.dialects.postgresql import insert

from snapbak_backend.api import cloud_deps
from snapbak_backend.api.cloud_deps import Caller, Session
from snapbak_backend.api.logged import LoggedRoute
from snapbak_backend.db.models import Recording
from snapbak_backend.schemas.cloud import RecordingRequest, RecordingResponse
from snapbak_backend.services import recordings
from snapbak_backend.services.events import publish
from snapbak_backend.services.failures import CloudFailure

router = APIRouter(route_class=LoggedRoute)


@router.put("/v1/recordings/{recording_id}")
async def put_recording(
    recording_id: UUID, body: RecordingRequest, device: Caller, db: Session, response: Response
) -> RecordingResponse:
    """Idempotent create or update in one statement: the update applies only to this device's live recording."""
    values = {"title": body.title, "duration_ms": body.durationMs}
    upsert = insert(Recording).values(id=recording_id, device_id=device.id, created_at=body.createdAt, **values)
    upsert = upsert.on_conflict_do_update(
        index_elements=[Recording.id],
        set_=values,
        where=(Recording.device_id == device.id) & Recording.deleted_at.is_(None),
    )
    # xmax is 0 on a row this statement inserted.
    created = literal_column("xmax = 0")
    row = (await db.execute(upsert.returning(created))).first()
    if row is None:
        raise CloudFailure(404, "not_found")
    await db.commit()
    response.status_code = 201 if row[0] else 200
    return RecordingResponse(recordingId=recording_id, title=body.title, durationMs=body.durationMs)


@router.delete("/v1/recordings/{recording_id}", status_code=204)
async def delete_recording(recording_id: UUID, device: Caller, db: Session, request: Request) -> None:
    """Soft delete; a second call also answers 204."""
    found = await recordings.lock(db, recording_id)
    if found.device_id != device.id:
        raise CloudFailure(404, "not_found")
    if found.deleted_at is None:
        failed = await recordings.soft_delete(db, recording_id)
        await db.commit()
        if failed is not None:
            await publish(cloud_deps.cloud(request).redis, failed.id, failed.version)
