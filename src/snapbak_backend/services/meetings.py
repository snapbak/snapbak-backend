"""The meeting envelope over a recording (MEETINGS.md, D11-D13): promote, pins, the state machine, and the
transcript-version counter the stale mark reads. The caller holds the recording's lock for every write.

A minutes run carries a token (`minutes_run`) that only `begin` and the sweep's `rerun` hand out. The run claims the
meeting with it before it calls the model and writes its end only while it still holds it, so a stalled run the
sweep replaced can never write over the new one."""

import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID

from arq import ArqRedis
from sqlalchemy import ARRAY, ColumnElement, Row, Text, case, cast, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from snapbak_backend.db.models import Meeting, MeetingPin, Recording
from snapbak_backend.schemas.cloud import MinutesOut
from snapbak_backend.services import minutes as written
from snapbak_backend.services.events import publish
from snapbak_backend.services.failures import CloudFailure

# A minutes run may start from these states; `generating` means one is already running.
STARTERS = ("awaiting_transcript", "failed", "ready")
# The runs the minutes get without a click: the first, and the sweep's restarts of a stalled run or a provider
# failure. Past this a stalled run fails `provider` and a failure waits for Try again, which counts afresh.
MAX_ATTEMPTS = 3
# A click's Try again takes over a run that has not marked itself alive for this long (it beats the sweep to it).
CLICK_TAKES_OVER = timedelta(minutes=10)


async def promote(session: AsyncSession, recording: Recording, title: str, by_user: bool) -> Meeting:
    """A live meeting for the recording, or 409 when one exists (D13). Its times are the recording's; `by_user` is the
    app's word that the user chose the title, which the minutes' title then never replaces.
    Commits, so the unique index is the guard under the caller's recording lock."""
    ended = recording.created_at + timedelta(milliseconds=recording.duration_ms)
    meeting = Meeting(
        id=uuid.uuid4(),
        recording_id=recording.id,
        title=title,
        title_set_by_user=by_user,
        version=1,
        started_at=recording.created_at,
        ended_at=ended,
    )
    session.add(meeting)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise CloudFailure(409, "meeting_exists") from None
    return meeting


async def owned(session: AsyncSession, device_id: UUID, which: ColumnElement[bool]) -> Meeting:
    """The device's live meeting that `which` picks, on a live recording; anything else is 404, as for a
    recording."""
    found = await session.scalar(
        select(Meeting)
        .join(Recording, Recording.id == Meeting.recording_id)
        .where(which, Meeting.deleted_at.is_(None), Recording.device_id == device_id, Recording.deleted_at.is_(None))
    )
    if found is None:
        raise CloudFailure(404, "not_found")
    return found


async def live_meeting(session: AsyncSession, recording_id: UUID) -> Meeting | None:
    found = await session.scalar(
        select(Meeting).where(Meeting.recording_id == recording_id, Meeting.deleted_at.is_(None))
    )
    return found


def minutes_of(meeting: Meeting) -> MinutesOut | None:
    """The stored minutes as the wire document, partial while a run writes them, or None before one starts."""
    return None if meeting.minutes is None else MinutesOut.model_validate(meeting.minutes)


async def pins(session: AsyncSession, meeting_id: UUID) -> list[MeetingPin]:
    rows = await session.scalars(
        select(MeetingPin)
        .where(MeetingPin.meeting_id == meeting_id, MeetingPin.deleted_at.is_(None))
        .order_by(MeetingPin.created_at)
    )
    return list(rows)


async def put_pin(
    session: AsyncSession, meeting: Meeting, pin_id: UUID, at_ms: int, note: str | None
) -> tuple[MeetingPin, bool]:
    """The pin, idempotent by its client-made ID; True when it was made. The caller commits."""
    pin = await session.get(MeetingPin, pin_id)
    if pin is not None and (pin.meeting_id != meeting.id or pin.deleted_at is not None):
        raise CloudFailure(404, "not_found")
    made = pin is None
    if pin is None:
        pin = MeetingPin(id=pin_id, meeting_id=meeting.id)
        session.add(pin)
    pin.at_ms = at_ms
    pin.note = note
    return pin, made


async def changed(session: AsyncSession, which: ColumnElement[bool], **values: Any) -> Row[UUID, int, bool] | None:
    """One guarded statement on the meeting `which` picks: `values`, the version bumped and the time touched; its
    row (`id`, `version`, `minutes_clicked`), or None when the guard held nothing. The caller commits."""
    row = (
        await session.execute(
            update(Meeting)
            .where(which, Meeting.deleted_at.is_(None))
            .values(**values, version=Meeting.version + 1, updated_at=func.now())
            .returning(Meeting.id, Meeting.version, Meeting.minutes_clicked)
        )
    ).first()
    return row


async def bump(session: AsyncSession, recording_id: UUID) -> tuple[UUID, int] | None:
    """Counts a transcript edit (D29); the meeting's ID and version, or None when the recording has no live
    meeting. The caller commits and publishes."""
    row = await changed(
        session, Meeting.recording_id == recording_id, transcript_version=Meeting.transcript_version + 1
    )
    return None if row is None else (row.id, row.version)


async def rename(session: AsyncSession, meeting_id: UUID, title: str) -> int:
    """The meeting's title, the user's from now on; the version it reached. The caller commits and publishes."""
    row = await changed(session, Meeting.id == meeting_id, title=title, title_set_by_user=True)
    if row is None:
        raise CloudFailure(404, "not_found")
    return row.version


async def launch(session: AsyncSession, redis: ArqRedis, meeting_id: UUID, version: int, run: UUID) -> None:
    """Commits a run just handed out, publishes the meeting's version, and enqueues the run's own task."""
    await session.commit()
    await publish(redis, meeting_id, version)
    await redis.enqueue_job("generate_minutes", meeting_id, run, _job_id=f"minutes:{meeting_id}:{run}")


def started(stored: dict[str, Any] | None, sections: Sequence[str] | None) -> dict[str, Any]:
    """The document a run starts from: what is stored (the user's work and the sections not asked for stay), with
    `sections` pending in CALLS order (all of them when None, none when empty) and their old failures cleared."""
    doc = written.empty([]) if stored is None else MinutesOut.model_validate(stored)
    # An empty list asks for no section: Identify speakers runs the loop alone, writing only what it changes.
    asked = set(written.CALLS if sections is None else sections)
    pending = [name for name in written.CALLS if name in asked]
    left = [item for item in doc.incomplete if item.section not in asked]
    # New action items are judged again: an old "no commitment" would hide a line the guard should ask about.
    judged = [] if "actionItems" in asked else doc.notCommitments
    changes = {"pending": pending, "incomplete": left, "notCommitments": judged}
    return doc.model_copy(update=changes).model_dump(mode="json")


async def begin(
    session: AsyncSession, redis: ArqRedis, meeting_id: UUID, clicked: bool, sections: Sequence[str] | None = None
) -> int | None:
    """Starts a minutes run under the starter states (a click also over a run stalled past CLICK_TAKES_OVER) with a
    new token, then commits, publishes and enqueues it; the version reached, or None when a run is already going.
    `clicked` when a regenerate started it (D39); `sections` the ones to write again, all when None."""
    run = uuid.uuid4()
    stored = await session.scalar(select(Meeting.minutes).where(Meeting.id == meeting_id))
    startable = Meeting.state.in_(STARTERS) | stalled(CLICK_TAKES_OVER) if clicked else Meeting.state.in_(STARTERS)
    # A whole run is current as of its own start (its snapshot, set when it finishes); a run of some sections keeps
    # the version the minutes were written at, so what made them stale still shows.
    whole = {"minutes_transcript_version": None} if sections is None else {}
    row = await changed(
        session,
        (Meeting.id == meeting_id) & startable,
        state="generating",
        minutes=started(stored, sections),
        minutes_failure=None,
        minutes_run=run,
        minutes_clicked=clicked,
        minutes_attempts=1 if clicked else Meeting.minutes_attempts + 1,
        **whole,
    )
    if row is None:
        return None
    if sections is None:
        # A whole run (the first, or a regenerate) works out the speakers again (MOM-08); one section's retry doesn't.
        restarted = func.jsonb_set(Recording.speaker_pass, cast("{stage}", ARRAY(Text)), cast('"reading"', JSONB))
        recording = select(Meeting.recording_id).where(Meeting.id == meeting_id).scalar_subquery()
        await session.execute(update(Recording).where(Recording.id == recording).values(speaker_pass=restarted))
    await launch(session, redis, meeting_id, row.version, run)
    return row.version


async def wait_again(session: AsyncSession, redis: ArqRedis, meeting_id: UUID) -> None:
    """A failed meeting waits on its transcript again (a retried job), its failure cleared; committed and
    published. Any other state is left as it is."""
    row = await changed(
        session,
        (Meeting.id == meeting_id) & (Meeting.state == "failed"),
        state="awaiting_transcript",
        minutes_failure=None,
    )
    await session.commit()
    if row is not None:
        await publish(redis, meeting_id, row.version)


def stalled(since: timedelta) -> ColumnElement[bool]:
    """A minutes run that has not marked itself alive for `since`: the sweep's pick, and its guard."""
    return (Meeting.state == "generating") & Meeting.deleted_at.is_(None) & (Meeting.updated_at < func.now() - since)


async def rerun(session: AsyncSession, redis: ArqRedis, meeting_id: UUID, since: timedelta) -> None:
    """A run that stopped moving gets a new token and its own task again, so the old token can no longer write;
    after MAX_ATTEMPTS runs it fails `provider` instead (a run that always dies would be restarted for ever).
    Committed and published."""
    stuck = (Meeting.id == meeting_id) & stalled(since)
    spent = await changed(
        session, stuck & (Meeting.minutes_attempts >= MAX_ATTEMPTS), state="failed", minutes_failure="provider"
    )
    if spent is not None:
        await session.commit()
        await publish(redis, meeting_id, spent.version)
        return
    run = uuid.uuid4()
    row = await changed(session, stuck, minutes_run=run, minutes_attempts=Meeting.minutes_attempts + 1)
    if row is not None:
        await launch(session, redis, meeting_id, row.version, run)


def retryable(since: timedelta) -> ColumnElement[bool]:
    """Minutes a provider outage left short a while ago, with runs left: failed `provider`, or ready with a section
    the provider failed. The sweep writes those sections again with no click (D39)."""
    short = Meeting.minutes["incomplete"].contains([{"code": "provider"}])
    return (
        (((Meeting.state == "failed") & (Meeting.minutes_failure == "provider")) | ((Meeting.state == "ready") & short))
        & (Meeting.minutes_attempts < MAX_ATTEMPTS)
        & Meeting.deleted_at.is_(None)
        & (Meeting.updated_at < func.now() - since)
    )


async def claim(session: AsyncSession, meeting_id: UUID, run: UUID) -> bool:
    """The run marks the meeting alive while it still holds its token; False when it lost it (or the meeting
    moved on). The caller commits."""
    held = await session.execute(
        update(Meeting).where(holding(meeting_id, run)).values(updated_at=func.now()).returning(Meeting.id)
    )
    return held.first() is not None


def holding(meeting_id: UUID, run: UUID) -> ColumnElement[bool]:
    return (Meeting.id == meeting_id) & (Meeting.state == "generating") & (Meeting.minutes_run == run)


async def save_minutes(session: AsyncSession, meeting_id: UUID, run: UUID, body: dict[str, Any]) -> int | None:
    """A section's progress, written only while the run holds its token; the meeting takes the minutes' title
    unless the user named it. The version reached, or None when the run lost its token. The caller commits and
    publishes."""
    title = body.get("title")
    named = {} if not title else {"title": case((Meeting.title_set_by_user, Meeting.title), else_=title[:300])}
    row = await changed(session, holding(meeting_id, run), minutes=body, **named)
    return None if row is None else row.version


async def edit_minutes(
    session: AsyncSession, meeting: Meeting, base: int, minutes: dict[str, Any], title: str | None
) -> int:
    """The user's edited minutes, against the version they edited; a new minutes title is their name for the
    meeting. The version reached; 409 `version_conflict` when the meeting moved on. The caller commits."""
    named = {} if title is None else {"title": title[:300], "title_set_by_user": True}
    which = (Meeting.id == meeting.id) & (Meeting.version == base) & (Meeting.state == "ready")
    row = await changed(session, which, minutes=minutes, minutes_generated_at=func.now(), **named)
    if row is None:
        raise CloudFailure(409, "version_conflict")
    return row.version


async def finish(
    session: AsyncSession, meeting_id: UUID, run: UUID, minutes: dict[str, Any], model: str, snapshot: int
) -> tuple[int, bool] | None:
    """The run's minutes, written only while it holds its token; the version and whether a click started it. The
    caller commits and publishes."""
    row = await changed(
        session,
        holding(meeting_id, run),
        state="ready",
        minutes=minutes,
        minutes_model=model,
        minutes_generated_at=func.now(),
        minutes_transcript_version=snapshot,
        minutes_failure=None,
    )
    return None if row is None else (row.version, row.minutes_clicked)


async def fail_minutes(session: AsyncSession, meeting_id: UUID, run: UUID, reason: str) -> int | None:
    """`failed` with the reason while the run holds its token; the version it reached, or None. The caller
    commits and publishes."""
    row = await changed(session, holding(meeting_id, run), state="failed", minutes_failure=reason)
    return None if row is None else row.version


async def fail_waiting(session: AsyncSession, recording_id: UUID, reason: str) -> tuple[UUID, int] | None:
    """The recording's meeting still waiting on a transcript fails with the job's code, so it never waits forever;
    its ID and version. The caller commits and publishes."""
    waiting = (Meeting.recording_id == recording_id) & (Meeting.state == "awaiting_transcript")
    row = await changed(session, waiting, state="failed", minutes_failure=reason)
    return None if row is None else (row.id, row.version)
