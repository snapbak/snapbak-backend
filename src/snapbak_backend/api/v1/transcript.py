"""The transcript: GET, and the edits the app's outbox sends (14-transcript-editing LLD; docs/cloud.md). The
backend keeps the current line only, last write wins with a `rev` counter (D11, Q19)."""

from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select

from snapbak_backend.ai.chain import ProviderFailed
from snapbak_backend.api import deps
from snapbak_backend.api.cloud_deps import Caller, Session
from snapbak_backend.api.logged import LoggedRoute
from snapbak_backend.db.models import Speaker, TranscriptionJob, TranscriptLine
from snapbak_backend.schemas.cloud import (
    LineOut,
    LineRequest,
    LineResponse,
    RomanLine,
    SpeakerNameRequest,
    SpeakerOut,
    SpeakerRequest,
    TranscriptResponse,
    TransliterateRequest,
    TransliterateResponse,
    WordOut,
)
from snapbak_backend.services import meetings, recordings, transliteration
from snapbak_backend.services.events import publish
from snapbak_backend.services.failures import CloudFailure

router = APIRouter(route_class=LoggedRoute)


def speaker_out(speaker: Speaker) -> SpeakerOut:
    return SpeakerOut(
        id=speaker.id,
        label=speaker.label,
        name=speaker.name,
        nameSource=speaker.name_source,
        colorIdx=speaker.color_idx,
    )


def roman_words(line: TranscriptLine) -> list[WordOut] | None:
    if line.roman_words is None:
        return None
    words = [WordOut.model_validate(word) for word in line.roman_words]
    return words


def label_number(speaker: Speaker) -> tuple[int, str]:
    """'Speaker 10' after 'Speaker 9'."""
    _, _, number = speaker.label.rpartition(" ")
    order = int(number) if number.isdigit() else 1_000_000
    return (order, speaker.label)


async def ready_job(db: Session, recording_id: UUID) -> TranscriptionJob:
    job = await db.scalar(
        select(TranscriptionJob).where(
            TranscriptionJob.recording_id == recording_id,
            TranscriptionJob.state == "ready",
            TranscriptionJob.deleted_at.is_(None),
        )
    )
    if job is None:
        raise CloudFailure(404, "not_found")
    return job


@router.get("/v1/recordings/{recording_id}/transcript")
async def transcript(recording_id: UUID, device: Caller, db: Session) -> TranscriptResponse:
    await recordings.owned(db, device.id, recording_id)
    job = await ready_job(db, recording_id)
    speakers = (await recordings.speakers_of(db, recording_id)).values()
    lines = await recordings.live_lines(db, recording_id)
    lines_out = [
        LineOut(
            id=line.id,
            idx=line.idx,
            speakerId=line.speaker_id,
            start=line.start_ms,
            end=line.end_ms,
            text=line.text,
            words=[WordOut.model_validate(word) for word in line.words],
            romanText=line.roman_text,
            romanWords=roman_words(line),
            rev=line.rev,
        )
        for line in lines
    ]
    speakers_out = [speaker_out(speaker) for speaker in sorted(speakers, key=label_number)]
    answer = TranscriptResponse(
        recordingId=recording_id,
        language=job.language_detected,
        script=job.script,
        speakers=speakers_out,
        lines=lines_out,
    )
    return answer


@router.patch("/v1/lines/{line_id}")
async def patch_line(line_id: UUID, body: LineRequest, device: Caller, db: Session, request: Request) -> LineResponse:
    """The whole line replaces the stored one; its Roman words are cleared, since they no longer match, and the
    next transliteration fills them again. An edit marks a meeting's minutes stale (D29)."""
    line = await recordings.owned_row(db, device.id, TranscriptLine, line_id)
    await recordings.locked(db, device.id, line.recording_id)
    line = await recordings.owned_row(db, device.id, TranscriptLine, line_id)
    speaker = await db.scalar(
        select(Speaker.id).where(
            Speaker.id == body.speakerId, Speaker.recording_id == line.recording_id, Speaker.deleted_at.is_(None)
        )
    )
    if speaker is None:
        raise CloudFailure(422, "invalid")
    words = [word.model_dump() for word in body.words]
    line.text = body.text
    line.words = words
    line.speaker_id = body.speakerId
    line.start_ms = min(word["s"] for word in words)
    line.end_ms = max(word["e"] for word in words)
    line.roman_text = None
    line.roman_words = None
    line.rev = TranscriptLine.rev + 1
    line.updated_at = func.now()
    bumped = await meetings.bump(db, line.recording_id)
    await db.flush()
    await db.refresh(line, ["rev"])
    rev = line.rev
    await db.commit()
    await publish_bumped(request, bumped)
    return LineResponse(rev=rev)


async def publish_bumped(request: Request, bumped: tuple[UUID, int] | None) -> None:
    """The meeting's new version, when the edit reached one (D29); the minutes are stale from here on."""
    if bumped is None:
        return
    meeting_id, version = bumped
    cloud = request.app.state.cloud
    await publish(cloud.redis, meeting_id, version)


@router.patch("/v1/speakers/{speaker_id}")
async def rename_speaker(
    speaker_id: UUID, body: SpeakerNameRequest, device: Caller, db: Session, request: Request
) -> SpeakerOut:
    speaker = await recordings.owned_row(db, device.id, Speaker, speaker_id)
    await recordings.locked(db, device.id, speaker.recording_id)
    speaker = await recordings.owned_row(db, device.id, Speaker, speaker_id)
    speaker.name = body.name
    speaker.name_source = "user"
    bumped = await meetings.bump(db, speaker.recording_id)
    await db.commit()
    await publish_bumped(request, bumped)
    return speaker_out(speaker)


@router.put("/v1/recordings/{recording_id}/speakers/{speaker_id}")
async def put_speaker(
    recording_id: UUID,
    speaker_id: UUID,
    body: SpeakerRequest,
    device: Caller,
    db: Session,
    request: Request,
    response: Response,
) -> SpeakerOut:
    """A speaker made on the Mac, idempotent by its ID. A label another speaker of the recording has is invalid."""
    await recordings.locked(db, device.id, recording_id)
    await ready_job(db, recording_id)
    taken = await db.scalar(
        select(Speaker.id).where(
            Speaker.recording_id == recording_id, Speaker.label == body.label, Speaker.id != speaker_id
        )
    )
    if taken is not None:
        raise CloudFailure(422, "invalid")
    speaker = await db.get(Speaker, speaker_id)
    if speaker is not None and (speaker.recording_id != recording_id or speaker.deleted_at is not None):
        raise CloudFailure(404, "not_found")
    response.status_code = 200 if speaker is not None else 201
    if speaker is None:
        speaker = Speaker(id=speaker_id, recording_id=recording_id, vendor_label="user")
        db.add(speaker)
    # A resent PUT that changes nothing (the outbox's retries) is no transcript edit: no stale mark (D29).
    changed = (speaker.label, speaker.name, speaker.color_idx) != (body.label, body.name, body.colorIdx)
    speaker.label = body.label
    speaker.name = body.name
    speaker.name_source = "user" if body.name else None
    speaker.color_idx = body.colorIdx
    bumped = await meetings.bump(db, recording_id) if changed else None
    await db.commit()
    await publish_bumped(request, bumped)
    return speaker_out(speaker)


@router.post("/v1/recordings/{recording_id}/transliterate")
async def transliterate(
    recording_id: UUID, body: TransliterateRequest, device: Caller, db: Session, request: Request
) -> TransliterateResponse:
    """Roman words for every line (TRX-09), made now for the lines that have none."""
    await recordings.owned(db, device.id, recording_id)
    await ready_job(db, recording_id)
    try:
        lines = await transliteration.transliterate(db, deps.tiers(request), recording_id)
    except ProviderFailed as error:
        raise CloudFailure(502, "provider") from error
    romans = [RomanLine(id=line.id, romanText=line.roman_text, romanWords=roman_words(line)) for line in lines]
    answer = TransliterateResponse(lines=romans)
    return answer
