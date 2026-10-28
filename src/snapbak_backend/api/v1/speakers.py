"""The speaker identification loop's routes (MEETINGS.md D42, MOM-08): the user's answer to one change, undo all,
and running the loop again. Each answer moves the pass's `rev`, so the app reads the transcript again; on a meeting
it counts as the user's own speaker edit (the minutes go stale, as after a rename), and an undone merge or move
writes again the sections it touched."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from sqlalchemy import select

from snapbak_backend.api import cloud_deps
from snapbak_backend.api.cloud_deps import Caller, Session
from snapbak_backend.api.logged import LoggedRoute
from snapbak_backend.api.v1.transcript import publish_renamed
from snapbak_backend.db.models import Meeting, Speaker
from snapbak_backend.schemas.cloud import Change, SpeakerChangeRequest, SpeakerPass
from snapbak_backend.services import jobs, meetings, people, recordings, speaker_loop, speakers
from snapbak_backend.services.failures import CloudFailure

router = APIRouter(route_class=LoggedRoute)


async def rewritten(db: Session, request: Request, recording_id: UUID, changed: list[Change]) -> None:
    """A meeting's minutes after the user changed its speakers, as each change stands now: stale like after a rename;
    an undone merge writes again the sections naming the speaker it kept, an undone or redone move those citing its
    line, and a redone merge retells its speaker's tokens as the loop did (no model call). The caller has committed
    the change."""
    meeting = await meetings.live_meeting(db, recording_id)
    doc = None if meeting is None else meetings.minutes_of(meeting)
    labels = await db.execute(select(Speaker.id, Speaker.label).where(Speaker.recording_id == recording_id))
    numbered = {speaker: n for speaker, label in labels if (n := people.number_of_label(label)) is not None}
    again: set[str] = set()
    merges: dict[int, int] = {}
    for change in changed:
        gone = numbered.get(change.speakerId)
        into = numbered.get(change.intoId) if change.intoId else None
        if change.kind == "merge" and change.state == "applied" and gone is not None and into is not None:
            merges[gone] = into
        elif doc is not None:
            again |= set(speakers.undone_sections(doc, change, into))
    if meeting is not None and doc is not None and merges:
        await meetings.changed(db, Meeting.id == meeting.id, minutes=people.retold(doc, merges).model_dump(mode="json"))
    bumped = await meetings.bump(db, recording_id)
    await db.commit()
    await publish_renamed(request, bumped)
    redis = cloud_deps.cloud(request).redis
    if meeting is not None and again:
        await meetings.begin(db, redis, meeting.id, clicked=True, sections=sorted(again))


async def not_while_writing(db: Session, recording_id: UUID, changes: list[Change]) -> None:
    """409 `minutes_busy` when a merge or move is undone while the meeting's minutes are being written: the sections
    it touched could not be written again until that run ends."""
    meeting = await meetings.live_meeting(db, recording_id)
    rewrites = any(change.kind in ("merge", "move") for change in changes)
    if rewrites and meeting is not None and meeting.state == "generating":
        raise CloudFailure(409, "minutes_busy")


@router.post("/v1/recordings/{recording_id}/speakers/changes/{change_id}")
async def change_speakers(
    recording_id: UUID, change_id: str, body: SpeakerChangeRequest, device: Caller, db: Session, request: Request
) -> SpeakerPass:
    """Undo an applied change or redo an undone one, or accept or reject a suggested name; 404 for an unknown change,
    409 `change_state` for one not in the state the action needs, `change_later` for an undo under a later merge,
    `change_gone` for a redo the transcript no longer allows, `minutes_busy` for a merge or move undone or redone while
    the minutes are being written (the sections it touched could not be written again)."""
    recording = await recordings.locked(db, device.id, recording_id)
    asked = next((each for each in speaker_loop.pass_of(recording).changes if each.id == change_id), None)
    rewrites = body.action in ("undo", "redo")
    if rewrites and asked is not None:
        await not_while_writing(db, recording_id, [asked])
    state, settled = await speaker_loop.settle_change(db, recording_id, change_id, body.action)
    await db.commit()
    if settled.state == "gone":
        # Stored so the app drops its Redo; nothing changed in the transcript.
        raise CloudFailure(409, "change_gone")
    # A suggestion turned down changes nothing in the transcript: the minutes stay as they are.
    if body.action != "reject":
        await rewritten(db, request, recording_id, [settled] if rewrites else [])
    return state


@router.post("/v1/recordings/{recording_id}/speakers/undo")
async def undo_speakers(recording_id: UUID, device: Caller, db: Session, request: Request) -> SpeakerPass:
    """Every applied change taken back, the latest first."""
    recording = await recordings.locked(db, device.id, recording_id)
    applied = [change for change in speaker_loop.pass_of(recording).changes if change.state == "applied"]
    await not_while_writing(db, recording_id, applied)
    state = await speaker_loop.undo_all(db, recording_id)
    await db.commit()
    if applied:
        ids = {change.id for change in applied}
        await rewritten(db, request, recording_id, [change for change in state.changes if change.id in ids])
    return state


@router.post("/v1/recordings/{recording_id}/speakers/identify")
async def identify_speakers(recording_id: UUID, device: Caller, db: Session, request: Request) -> SpeakerPass:
    """The loop again, owed from here on (stage `reading`): on a meeting, a minutes run with no section to write, so
    only what the loop changes is written again (D42); on any other recording, the loop alone. 409 `not_ready`
    before its transcript is, or while the minutes are being written."""
    recording = await recordings.locked(db, device.id, recording_id)
    meeting = await meetings.live_meeting(db, recording_id)
    job = await jobs.live(db, recording_id)
    stored = speaker_loop.pass_or_none(recording)
    # Ready minutes to work on, and one loop at a time.
    unready = meeting is not None and (meeting.state != "ready" or meeting.minutes is None)
    looping = stored is not None and stored.stage != "done"
    if job is None or job.state != "ready" or unready or looping:
        raise CloudFailure(409, "not_ready")
    state = await speaker_loop.restaged(db, recording_id, stage="reading")
    redis = cloud_deps.cloud(request).redis
    if meeting is not None:
        if await meetings.begin(db, redis, meeting.id, clicked=True, sections=[]) is None:
            raise CloudFailure(409, "not_ready")
    else:
        await db.commit()
        # Its own run token, which a retry of the job keeps (arq keeps a finished job's ID for an hour, so each ask
        # is its own job).
        run = uuid4()
        await redis.enqueue_job("identify_speakers", recording_id, run, _job_id=f"speakers:{recording_id}:{run.hex}")
    return state
