"""The app's speaker edits as one call (TRX-11): a merge, its undo, a replace-all, a line given to a new speaker. The
app sends the speakers and lines as the edit leaves them, so an undo is the same call with the state before, and a
resend changes nothing. A meeting's minutes name a merged speaker's tokens by the one kept at once (no model call)
and go stale until Regenerate (D29, D41)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Request
from sqlalchemy import func, select, update

from snapbak_backend.api.cloud_deps import Caller, Session
from snapbak_backend.api.logged import LoggedRoute
from snapbak_backend.api.v1.transcript import publish_renamed, ready_job, speaker_out
from snapbak_backend.db.models import Meeting, Speaker, TranscriptLine
from snapbak_backend.schemas.cloud import EditedSpeaker, LineRev, SpeakerEditsRequest, SpeakerEditsResponse
from snapbak_backend.services import meetings, people, recordings
from snapbak_backend.services.failures import CloudFailure

router = APIRouter(route_class=LoggedRoute)


def checked(body: SpeakerEditsRequest, stored: dict[UUID, Speaker], owner: dict[UUID, UUID]) -> None:
    """422 unless every line ends on a live speaker of this recording and no label is taken twice."""
    gone = {each.id for each in body.speakers if each.deleted}
    live = {id for id, speaker in stored.items() if speaker.deleted_at is None and id not in gone}
    live |= {each.id for each in body.speakers if not each.deleted}
    after = dict(owner)
    for moved in body.lines:
        if moved.id not in after:
            raise CloudFailure(422, "invalid")
        after[moved.id] = moved.speakerId
    if any(speaker not in live for speaker in after.values()):
        raise CloudFailure(422, "invalid")
    labels = {stored[id].label: id for id in live if id in stored}
    labels |= {each.label: each.id for each in body.speakers if not each.deleted}
    if len(labels) != len(live):
        raise CloudFailure(422, "invalid")


def kept(stored: dict[UUID, Speaker], sent: EditedSpeaker, recording_id: UUID) -> tuple[Speaker, bool]:
    """The speaker as sent, made when new and back when it was deleted; whether that changed it."""
    speaker = stored.get(sent.id) or Speaker(id=sent.id, recording_id=recording_id, vendor_label="user")
    source = (sent.nameSource or "user") if sent.name else None
    before = (speaker.label, speaker.name, speaker.name_source, speaker.color_idx, speaker.deleted_at)
    speaker.label = sent.label
    speaker.name = sent.name
    speaker.name_source = source
    speaker.color_idx = sent.colorIdx
    speaker.deleted_at = None
    after = (speaker.label, speaker.name, speaker.name_source, speaker.color_idx, speaker.deleted_at)
    return speaker, before != after


def sent_ids(body: SpeakerEditsRequest) -> list[UUID]:
    return [moved.id for moved in body.lines]


async def moved_lines(db: Session, body: SpeakerEditsRequest, owner: dict[UUID, UUID]) -> dict[UUID, set[UUID]]:
    """Each line to its new speaker in one statement per speaker (a merge is one), as the user's edit now: the speaker
    loop moves only lines still at rev 1 (MOM-08). Where each speaker's lines went, by the speaker they left."""
    by_target: dict[UUID, list[UUID]] = {}
    went: dict[UUID, set[UUID]] = {}
    for moved in body.lines:
        if owner[moved.id] == moved.speakerId:
            continue
        by_target.setdefault(moved.speakerId, []).append(moved.id)
        went.setdefault(owner[moved.id], set()).add(moved.speakerId)
    for target, ids in by_target.items():
        await db.execute(
            update(TranscriptLine)
            .where(TranscriptLine.id.in_(ids))
            .values(speaker_id=target, rev=TranscriptLine.rev + 1, updated_at=func.now())
        )
    return went


@dataclass
class Gone:
    deleted: list[UUID]
    # Speaker N merged away into Speaker M, for the minutes' tokens.
    retold: dict[int, int]


def gone(body: SpeakerEditsRequest, stored: dict[UUID, Speaker], went: dict[UUID, set[UUID]]) -> Gone:
    """Each speaker the edits delete, voiceprint and all; one whose lines all went to one speaker is merged into it."""
    now = datetime.now(UTC)
    made = Gone(deleted=[], retold={})
    for sent in body.speakers:
        speaker = stored.get(sent.id)
        if not sent.deleted or speaker is None or speaker.deleted_at is not None:
            continue
        speaker.deleted_at = now
        speaker.voiceprint = None
        speaker.voiceprint_key_id = None
        made.deleted.append(sent.id)
        targets = went.get(sent.id, set())
        into = stored.get(next(iter(targets))) if len(targets) == 1 else None
        gone_n = people.number_of(speaker)
        into_n = None if into is None else people.number_of(into)
        if gone_n is not None and into_n is not None:
            made.retold[gone_n] = into_n
    return made


@router.post("/v1/recordings/{recording_id}/speakers/edits")
async def edit_speakers(
    recording_id: UUID, body: SpeakerEditsRequest, device: Caller, db: Session, request: Request
) -> SpeakerEditsResponse:
    """409 `minutes_busy` while the minutes are being written: no edit is taken then (TRX-11)."""
    await recordings.locked(db, device.id, recording_id)
    await ready_job(db, recording_id)
    meeting = await meetings.live_meeting(db, recording_id)
    if meeting is not None and meeting.state == "generating":
        raise CloudFailure(409, "minutes_busy")
    stored = {each.id: each for each in await db.scalars(select(Speaker).where(Speaker.recording_id == recording_id))}
    alien = await db.scalar(
        select(Speaker.id).where(
            Speaker.id.in_([each.id for each in body.speakers]), Speaker.recording_id != recording_id
        )
    )
    if alien is not None:
        raise CloudFailure(404, "not_found")
    owned = await db.execute(
        select(TranscriptLine.id, TranscriptLine.speaker_id).where(
            TranscriptLine.recording_id == recording_id, TranscriptLine.deleted_at.is_(None)
        )
    )
    owner: dict[UUID, UUID] = dict(owned.tuples().all())
    checked(body, stored, owner)

    changed = False
    for sent in body.speakers:
        if sent.deleted:
            continue
        speaker, moved = kept(stored, sent, recording_id)
        db.add(speaker)
        stored[sent.id] = speaker
        changed |= moved
    await db.flush()
    went = await moved_lines(db, body, owner)
    changed |= bool(went)
    merges = gone(body, stored, went)
    changed |= bool(merges.deleted)

    doc = None if meeting is None or not merges.retold else meetings.minutes_of(meeting)
    if meeting is not None and doc is not None:
        retold = people.retold(doc, merges.retold).model_dump(mode="json")
        await meetings.changed(db, Meeting.id == meeting.id, minutes=retold)
    bumped = await meetings.bump(db, recording_id) if changed else None
    revs = await db.execute(select(TranscriptLine.id, TranscriptLine.rev).where(TranscriptLine.id.in_(sent_ids(body))))
    rev_of = dict(revs.tuples().all())
    answer = SpeakerEditsResponse(
        speakers=[speaker_out(stored[each.id]) for each in body.speakers],
        lines=[LineRev(id=moved.id, rev=rev_of[moved.id]) for moved in body.lines],
    )
    await db.commit()
    await publish_renamed(request, bumped)
    return answer
