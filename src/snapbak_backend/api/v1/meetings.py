"""The meeting routes (MEETINGS.md, section 5): promote, read (long-poll), rename, pins, regenerate, edit the
minutes, ask, search."""

import asyncio
import contextlib
import time
from datetime import UTC, date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from snapbak_backend.ai.chain import ProviderFailed
from snapbak_backend.api import cloud_deps, deps
from snapbak_backend.api.cloud_deps import Caller, Session
from snapbak_backend.api.logged import LoggedRoute
from snapbak_backend.api.v1.transcription import FINAL
from snapbak_backend.config.settings import MAX_MINUTES_BYTES
from snapbak_backend.db.models import Meeting, MeetingPin, Recording, Script, TranscriptionJob
from snapbak_backend.schemas.cloud import (
    UTC_FORMAT,
    AskRequest,
    AskResponse,
    MeetingAccepted,
    MeetingPatchRequest,
    MeetingRequest,
    MeetingResponse,
    MinutesEditRequest,
    PinOut,
    PinRequest,
    RegenerateRequest,
    SearchRequest,
    SearchResponse,
)
from snapbak_backend.services import jobs, meeting_index, meetings, minutes, people, qa, recordings, speaker_loop, usage
from snapbak_backend.services.cloud import Cloud
from snapbak_backend.services.embeddings import EmbedError
from snapbak_backend.services.events import publish
from snapbak_backend.services.failures import CloudFailure

router = APIRouter(route_class=LoggedRoute)


def pin_out(pin: MeetingPin) -> PinOut:
    made = PinOut(id=pin.id, atMs=pin.at_ms, note=pin.note)
    return made


async def response_of(db: Session, meeting: Meeting) -> MeetingResponse:
    # Served whenever stored: while a run writes them, the sections still to come are in `pending`.
    minutes = meetings.minutes_of(meeting)
    pins = await meetings.pins(db, meeting.id)
    stale = (
        meeting.minutes_transcript_version is not None
        and meeting.minutes_transcript_version != meeting.transcript_version
    )
    made = MeetingResponse(
        meetingId=meeting.id,
        recordingId=meeting.recording_id,
        title=meeting.title,
        state=meeting.state,
        minutes=minutes,
        minutesFailure=meeting.minutes_failure,
        stale=stale,
        version=meeting.version,
        pins=[pin_out(pin) for pin in pins],
        startedAt=meeting.started_at,
        endedAt=meeting.ended_at,
        speakerPass=speaker_loop.pass_or_none(await db.get_one(Recording, meeting.recording_id)),
    )
    return made


async def transcript_job(
    db: Session, cloud: Cloud, recording: Recording, language: str, script: Script
) -> TranscriptionJob | None:
    """The recording's live job, or a new one when it has none, as the transcription route makes it; started at once
    when its audio is already up. A job a concurrent request made first is that one's."""
    job = await jobs.live(db, recording.id)
    if job is None:
        try:
            job = await jobs.create(db, recording, language, script, cloud.config.transcription)
        except CloudFailure as error:
            if error.code != "job_exists":
                raise
    if job is not None and job.state == "queued" and job.upload_id is not None:
        await jobs.enqueue_start(cloud.redis, job.id)
    return job


@router.post("/v1/recordings/{recording_id}/meeting", status_code=201)
async def promote(
    recording_id: UUID, body: MeetingRequest, device: Caller, db: Session, request: Request
) -> MeetingAccepted:
    """The recording becomes a meeting (D13); one that is one already is a 409 before anything is spent. A
    transcript that isn't ready gets its job started here; a live job's merge starts the minutes when it lands."""
    cloud = cloud_deps.cloud(request)
    recording = await recordings.locked(db, device.id, recording_id)
    if await meetings.live_meeting(db, recording_id) is not None:
        raise CloudFailure(409, "meeting_exists")
    job = await transcript_job(db, cloud, recording, body.language, body.script)
    meeting = await meetings.promote(db, recording, body.title, body.titleByUser)
    # Minutes start at once when the transcript is already there.
    started = await meetings.begin(db, cloud.redis, meeting.id, clicked=False) if job and job.state == "ready" else None
    if started is None:
        return MeetingAccepted(meetingId=meeting.id, state="awaiting_transcript", version=1)
    return MeetingAccepted(meetingId=meeting.id, state="generating", version=started)


async def read(cloud: Cloud, device_id: UUID, recording_id: UUID) -> MeetingResponse:
    """The meeting as the app sees it, in a session of its own, so a long-poll holds no connection while it waits."""
    async with cloud.sessions() as db:
        meeting = await meetings.owned(db, device_id, Meeting.recording_id == recording_id)
        made = await response_of(db, meeting)
    return made


@router.get("/v1/recordings/{recording_id}/meeting")
async def get_meeting(
    recording_id: UUID,
    device: Caller,
    request: Request,
    wait: Annotated[int, Query(ge=0, le=25)] = 0,
    since: Annotated[int, Query(ge=0)] = 0,
) -> MeetingResponse:
    """Answers at once when the meeting moved past `since` or is final; else waits as the job route does."""
    cloud = cloud_deps.cloud(request)
    meeting_id = await meeting_id_of(cloud, device.id, recording_id)
    deadline = time.monotonic() + wait
    with request.app.state.events.watch(meeting_id) as changed:
        current = await read(cloud, device.id, recording_id)
        while current.version <= since and current.state not in FINAL and time.monotonic() < deadline:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), deadline - time.monotonic())
            # Cleared before the read, so a change after it wakes the next wait at once.
            changed.clear()
            current = await read(cloud, device.id, recording_id)
    return current


async def meeting_id_of(cloud: Cloud, device_id: UUID, recording_id: UUID) -> UUID:
    """The meeting's ID, read before the watch starts so the first read after it cannot miss a change."""
    async with cloud.sessions() as db:
        meeting = await meetings.owned(db, device_id, Meeting.recording_id == recording_id)
        return meeting.id


@router.patch("/v1/meetings/{meeting_id}")
async def patch_meeting(
    meeting_id: UUID, body: MeetingPatchRequest, device: Caller, db: Session, request: Request
) -> MeetingResponse:
    meeting = await meetings.owned(db, device.id, Meeting.id == meeting_id)
    await recordings.locked(db, device.id, meeting.recording_id)
    version = await meetings.rename(db, meeting.id, body.title)
    await db.commit()
    await publish(cloud_deps.cloud(request).redis, meeting.id, version)
    fresh = await meetings.owned(db, device.id, Meeting.id == meeting_id)
    made = await response_of(db, fresh)
    return made


@router.put("/v1/meetings/{meeting_id}/pins/{pin_id}")
async def put_pin(
    meeting_id: UUID, pin_id: UUID, body: PinRequest, device: Caller, db: Session, response: Response
) -> PinOut:
    """A pin, idempotent by its client-made ID (the app's outbox may resend); 201 when it was made."""
    meeting = await meetings.owned(db, device.id, Meeting.id == meeting_id)
    await recordings.locked(db, device.id, meeting.recording_id)
    pin, made = await meetings.put_pin(db, meeting, pin_id, body.atMs, body.note)
    await db.commit()
    response.status_code = 201 if made else 200
    return pin_out(pin)


@router.post("/v1/meetings/{meeting_id}/minutes/regenerate")
async def regenerate(
    meeting_id: UUID, device: Caller, db: Session, request: Request, body: RegenerateRequest | None = None
) -> MeetingAccepted:
    """Minutes again from the transcript as it stands now (D29), the app's Try again: the `sections` asked for, or
    all of them; the user's own items and edited texts stay. A meeting whose transcript failed gets a new job,
    with the language and script its last one asked for, and waits on it again."""
    cloud = cloud_deps.cloud(request)
    meeting = await meetings.owned(db, device.id, Meeting.id == meeting_id)
    recording = await recordings.locked(db, device.id, meeting.recording_id)
    last = await jobs.latest(db, recording.id)
    job = await transcript_job(db, cloud, recording, last.language_requested, last.script) if last else None
    if job is not None and job.state == "ready":
        sections = list(body.sections) if body and body.sections else None
        # A whole regenerate counts against the meeting's day (MOM-09); a failed section's Try again never does.
        whole = sections is None
        if whole and not await meetings.counted_regenerate(db, meeting.id):
            resets = usage.resets_at(datetime.now(UTC)).strftime(UTC_FORMAT)
            await db.rollback()
            raise CloudFailure(429, "regenerate_limit", resetsAt=resets)
        started = await meetings.begin(db, cloud.redis, meeting.id, clicked=True, sections=sections)
        # A run is writing (and still alive): starting nothing behind a 200 left the section asked for failed with
        # no word why. A whole one refused costs nothing.
        if started is None:
            if whole:
                await meetings.uncounted_regenerate(db, meeting.id)
                await db.commit()
            raise CloudFailure(409, "minutes_running")
    elif meeting.state == "generating":
        raise CloudFailure(409, "minutes_running")
    else:
        await meetings.wait_again(db, cloud.redis, meeting.id)
    fresh = await meetings.owned(db, device.id, Meeting.id == meeting_id)
    made = MeetingAccepted(meetingId=meeting.id, state=fresh.state, version=fresh.version)
    return made


@router.put("/v1/meetings/{meeting_id}/minutes")
async def edit_minutes(
    meeting_id: UUID, body: MinutesEditRequest, device: Caller, db: Session, request: Request
) -> MeetingResponse:
    """The user's whole edited minutes against the version they edited (409 `version_conflict` when it moved, 409
    `minutes_not_ready` while a run writes them). What they added or changed is marked theirs; the minutes are
    published and embedded again."""
    # Bytes, not characters: Devanagari is three bytes a character.
    if len(body.minutes.model_dump_json().encode()) > MAX_MINUTES_BYTES:
        raise CloudFailure(413, "minutes_too_large")
    cloud = cloud_deps.cloud(request)
    meeting = await meetings.owned(db, device.id, Meeting.id == meeting_id)
    await recordings.locked(db, device.id, meeting.recording_id)
    stored = meetings.minutes_of(meeting)
    if meeting.state != "ready" or stored is None:
        raise CloudFailure(409, "minutes_not_ready")
    doc = minutes.edited(stored, body.minutes)
    title = doc.title if doc.title and doc.title != stored.title else None
    version = await meetings.edit_minutes(db, meeting, body.baseVersion, doc.model_dump(mode="json"), title)
    await db.commit()
    await publish(cloud.redis, meeting.id, version)
    await cloud.redis.enqueue_job("embed_minutes_task", meeting.id, _job_id=f"embed:{meeting.id}:{version}")
    fresh = await meetings.owned(db, device.id, Meeting.id == meeting_id)
    made = await response_of(db, fresh)
    return made


async def reserve(db: Session, device: Caller, name: str) -> date:
    """One use of a daily LLM counter (D33), committed at once, or a 429 with when the day resets. The day comes
    back for the refund a provider failure owes."""
    day = await usage.reserve_counted(db, device.id, name)
    if day is None:
        resets = usage.resets_at(datetime.now(UTC)).strftime(UTC_FORMAT)
        await db.rollback()
        raise CloudFailure(429, "daily_limit", resetsAt=resets)
    await db.commit()
    return day


async def give_back(db: Session, device: Caller, name: str, day: date) -> None:
    """The day's use back, on a session a failed statement may have left needing its rollback (else the refund
    fails too and hides the first error)."""
    # Read before the rollback, which expires the device row (a lazy load can't run here).
    device_id = device.id
    await db.rollback()
    await usage.refund_counted(db, device_id, name, day)
    await db.commit()


@router.post("/v1/meetings/{meeting_id}/ask")
async def ask(meeting_id: UUID, body: AskRequest, device: Caller, db: Session, request: Request) -> AskResponse:
    """One question about one meeting (D18), answered from its transcript lines with checked citations. Everything
    is read and the transaction ended before the model runs, so no database connection waits on it."""
    meeting = await meetings.owned(db, device.id, Meeting.id == meeting_id)
    # Ask reads the transcript (the minutes only add notes): it answers while a run writes them (grading 4).
    job = await jobs.live(db, meeting.recording_id)
    if job is None or job.state != "ready":
        raise CloudFailure(409, "transcript_not_ready")
    day = await reserve(db, device, "qa_questions")
    try:
        asked = await qa.gather(db, meeting, body.question)
        await db.commit()

        # The minutes' sections by meaning first: the question embedded with no transaction open, then one short
        # read of the meeting's stored Ask sections nearest it.
        async def stored(vector: list[float]) -> list[int]:
            order = await meeting_index.nearest_ask(db, meeting.id, vector, qa.NEAREST)
            await db.commit()
            # Vectors from other minutes than these (an edit not yet embedded again) are no guide.
            return order if order and max(order) < len(asked.sections) else []

        asked = await qa.focused(cloud_deps.cloud(request).embeddings, body.question, asked, stored)
        answer = await qa.answer(deps.tiers(request), body.question, asked)
    except Exception as error:
        # Whatever broke, the question was never answered: it goes back to the day's count.
        await give_back(db, device, "qa_questions", day)
        if isinstance(error, ProviderFailed):
            raise CloudFailure(502, "provider") from error
        raise
    return answer


@router.post("/v1/meetings/search")
async def search(body: SearchRequest, device: Caller, db: Session, request: Request) -> SearchResponse:
    """The device's meetings by meaning over their minutes sections, re-ranked by the polish model (D38). The
    embedding and the re-rank run with no transaction open."""
    cloud = cloud_deps.cloud(request)
    day = await reserve(db, device, "search_queries")
    try:
        vector = (await cloud.embeddings.embed([body.query]))[0]
        found = await meeting_index.candidates(db, device.id, vector, body.limit)
        # An action's owner names its people by the name they have now (D41).
        recordings_of = {row["recording_id"] for row in found if row["owner"]}
        speakers = {rid: await recordings.speakers_of(db, rid) for rid in recordings_of}
        rows = people.named_owners(found, speakers)
        await db.commit()
        hits = await meeting_index.reranked(deps.tiers(request), body.query, body.limit, rows)
    except Exception as error:
        await give_back(db, device, "search_queries", day)
        if isinstance(error, (ProviderFailed, EmbedError)):
            raise CloudFailure(502, "provider") from error
        raise
    made = SearchResponse(hits=hits)
    return made
