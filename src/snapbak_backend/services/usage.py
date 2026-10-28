"""The per-device daily minutes (CLD-03, Q98): reserved atomically when a job is made, refunded when it fails; and
the daily translated caption lines (16-caption-translation LLD), reserved and refunded the same way."""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import Date, Update, cast, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import ReturningInsert

from snapbak_backend.config.settings import (
    DAILY_QA_QUESTIONS,
    DAILY_SEARCH_QUERIES,
    DAILY_SECONDS,
    DAILY_TRANSLATED_LINES,
)
from snapbak_backend.db.models import Recording, TranscriptionJob, UsageDaily

# The daily counters of the model features and their limits: translated caption lines (TRX-07), and the meetings'
# questions and searches (MEETINGS.md, D33).
COUNTERS = {
    "translated_lines": DAILY_TRANSLATED_LINES,
    "qa_questions": DAILY_QA_QUESTIONS,
    "search_queries": DAILY_SEARCH_QUERIES,
}


def reservation(device_id: UUID, seconds: int) -> ReturningInsert[int]:
    """Adds `seconds` to today's (UTC) row unless the day would pass the limit, in one statement, so concurrent
    creates can't both slip under it. No row back means 429."""
    today = cast(func.timezone("UTC", func.now()), Date)
    statement = insert(UsageDaily).values(device_id=device_id, day=today, audio_seconds=seconds)
    added = UsageDaily.audio_seconds + statement.excluded.audio_seconds
    statement = statement.on_conflict_do_update(
        index_elements=[UsageDaily.device_id, UsageDaily.day],
        set_={"audio_seconds": added},
        where=added <= DAILY_SECONDS,
    )
    returning = statement.returning(UsageDaily.audio_seconds)
    return returning


async def reserve(session: AsyncSession, device_id: UUID, seconds: int) -> bool:
    row = (await session.execute(reservation(device_id, seconds))).first()
    return row is not None


async def release(session: AsyncSession, device_id: UUID, seconds: int) -> None:
    """Seconds `reserve` took today given back, floored at 0: the work they were for never ran."""
    today = cast(func.timezone("UTC", func.now()), Date)
    await session.execute(
        update(UsageDaily)
        .where(UsageDaily.device_id == device_id, UsageDaily.day == today)
        .values(audio_seconds=func.greatest(UsageDaily.audio_seconds - seconds, 0))
    )


def refund_of(job: TranscriptionJob) -> Update:
    """The job's seconds back to the UTC day it was made, floored at 0."""
    owner = select(Recording.device_id).where(Recording.id == job.recording_id).scalar_subquery()
    day = job.created_at.astimezone(UTC).date()
    statement = (
        update(UsageDaily)
        .where(UsageDaily.device_id == owner, UsageDaily.day == day)
        .values(audio_seconds=func.greatest(UsageDaily.audio_seconds - job.billed_seconds, 0))
    )
    return statement


async def refund(session: AsyncSession, job: TranscriptionJob) -> None:
    await session.execute(refund_of(job))


def resets_at(now: datetime) -> datetime:
    """The next 00:00 UTC, when the daily limit resets."""
    tomorrow: date = now.astimezone(UTC).date() + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=UTC)


async def reserve_counted(session: AsyncSession, device_id: UUID, name: str, amount: int = 1) -> date | None:
    """`amount` more on today's (UTC) `name` counter (one of `COUNTERS`) unless the day would pass its limit, in one
    statement as for `reservation` (a translation page is at most 200 lines, so a day's first row never passes it
    alone); the day reserved, or None for 429."""
    column = getattr(UsageDaily, name)
    today = cast(func.timezone("UTC", func.now()), Date)
    statement = insert(UsageDaily).values(device_id=device_id, day=today, **{name: amount})
    added = column + amount
    statement = statement.on_conflict_do_update(
        index_elements=[UsageDaily.device_id, UsageDaily.day],
        set_={name: added},
        where=added <= COUNTERS[name],
    )
    row = (await session.execute(statement.returning(UsageDaily.day))).first()
    if row is None:
        return None
    reserved: date = row[0]
    return reserved


async def refund_counted(session: AsyncSession, device_id: UUID, name: str, day: date, amount: int = 1) -> None:
    """`amount` back on the counter's UTC day, floored at 0 (a provider failure never burns a question, a search or
    a translated line)."""
    column = getattr(UsageDaily, name)
    await session.execute(
        update(UsageDaily)
        .where(UsageDaily.device_id == device_id, UsageDaily.day == day)
        .values(**{name: func.greatest(column - amount, 0)})
    )
