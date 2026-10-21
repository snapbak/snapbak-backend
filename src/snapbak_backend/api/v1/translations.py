"""Caption translation (TRX-07, 16-caption-translation LLD, Backend endpoints): a page of lines into a language's
track, a user's fix of one line, and removing a language. Behind the device token."""

import time
from uuid import UUID

from fastapi import APIRouter, Request

from snapbak_backend.ai.chain import elapsed_ms, retries, total
from snapbak_backend.api import deps
from snapbak_backend.api.cloud_deps import Caller, Session
from snapbak_backend.api.logged import LoggedRoute
from snapbak_backend.db.models import CaptionTrack
from snapbak_backend.schemas.translations import (
    CaptionTrackOut,
    LineEdit,
    TrackLine,
    TranslateRequest,
    TranslateResponse,
)
from snapbak_backend.services import translate
from snapbak_backend.services.metrics import log, verdicts

router = APIRouter(route_class=LoggedRoute)
ROUTE = "/v1/recordings/{recording_id}/captions/translations"


def track_out(track: CaptionTrack) -> CaptionTrackOut:
    shown = [line for line in track.lines if translate.unmarked(line) is not None]
    lines = [TrackLine.model_validate(line) for line in shown]
    out = CaptionTrackOut(
        id=track.id,
        recordingId=track.recording_id,
        language=track.language,
        sourceLanguage=track.source_language,
        lines=lines,
    )
    return out


@router.post(ROUTE)
async def translate_page(
    recording_id: UUID, body: TranslateRequest, device: Caller, db: Session, request: Request
) -> TranslateResponse:
    """Idempotent: lines whose stored translation matches their `sourceHash` come back without a model call."""
    started = time.monotonic()
    track, failed, calls = await translate.translate(db, deps.tiers(request), device.id, recording_id, body)
    engine = calls[0].engine if calls else "-"
    log(ROUTE, engine, "translate", elapsed_ms(started), total(calls), verdicts(calls) or "kept", retries(calls))
    answer = TranslateResponse(track=track_out(track), failed=failed)
    return answer


@router.patch("/v1/caption-tracks/{track_id}/lines/{idx}")
async def edit_line(track_id: UUID, idx: int, body: LineEdit, device: Caller, db: Session) -> TrackLine:
    line = await translate.edit_line(db, device.id, track_id, idx, body)
    out = TrackLine.model_validate(line)
    return out


@router.delete("/v1/caption-tracks/{track_id}", status_code=204)
async def delete_track(track_id: UUID, device: Caller, db: Session) -> None:
    await translate.delete_track(db, device.id, track_id)
