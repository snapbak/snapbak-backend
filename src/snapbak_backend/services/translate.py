"""Caption translation (TRX-07, 16-caption-translation LLD, algorithms C and D): one page of transcript lines into a
language's caption track, keeping each line's idx, so the app lays the translation over the source line's time.

A page runs in three steps, so no transaction stays open while the model thinks: under the recording's lock, check
the page, make the track, pick the lines whose stored translation doesn't match and reserve them against the day's
limit, marking each of them in flight in the stored track; then ask the model in batches; then, under the lock again,
write the answers, which clears the marks. A line changed meanwhile (an edit, another translation) keeps its change.
A request that finds a line in flight for the same text leaves it out of its work, so it is asked and charged once,
and waits for it after its own batches. Never logs a line's text (CLAUDE.md rule 3)."""

import asyncio
import json
import math
import string
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from snapbak_backend.ai import languages
from snapbak_backend.ai.chain import Call, ProviderFailed, Tiers, ask
from snapbak_backend.ai.prompts import translate as prompts
from snapbak_backend.ai.providers.base import Provider
from snapbak_backend.db.models import CaptionTrack, Recording, Speaker, TranscriptionJob
from snapbak_backend.schemas.cloud import UTC_FORMAT
from snapbak_backend.schemas.translations import PAGE_LINES, LineEdit, SourceLine, TranslateRequest
from snapbak_backend.services import recordings, usage
from snapbak_backend.services.failures import CloudFailure
from snapbak_backend.transcription.languages import base

BATCH_LINES = 40
CONTEXT_LINES = 3
PARALLEL = 4
BATCH_DEADLINE_S = 30
HEDGE_S = 20
KEEP_TERMS = 50
# A line's in-flight mark lasts a page's worst case, two waves of batches, and a little more; past it, the request
# that made it is taken to have crashed and the mark is ignored.
IN_FLIGHT_S = 2 * BATCH_DEADLINE_S + 5
AWAIT_POLL_S = 0.2

type Stored = dict[str, Any]


@dataclass(frozen=True)
class Plan:
    """What the first step hands the model and the last: the track, the languages, the terms to keep, the lines to
    translate, the whole page in idx order (for context), each work line as step one wrote it (marked in flight), the
    day their lines were reserved on, and the lines another request is translating, by idx, with their marks' end
    (unix seconds)."""

    track_id: UUID
    target: languages.Language
    source: str
    source_name: str
    keep: list[str]
    work: list[SourceLine]
    page: list[SourceLine]
    seen: dict[int, Stored]
    day: date | None
    awaited: dict[int, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Batch:
    before: list[SourceLine]
    lines: list[SourceLine]
    after: list[SourceLine]


def now_text() -> str:
    stamp = datetime.now(UTC).strftime(UTC_FORMAT)
    return stamp


def matches(stored: Stored | None, line: SourceLine) -> bool:
    """A stored translation of the same kept text that didn't fail needs no model call (C.6)."""
    if stored is None:
        return False
    same = stored["sourceHash"] == line.sourceHash and stored["text"] is not None and not stored["failed"]
    return same


def live_mark(stored: Stored | None) -> Stored | None:
    """The stored line's in-flight mark, `{"hash", "until"}`, while it lasts."""
    mark = stored.get("inflight") if stored is not None else None
    live = mark if mark is not None and mark["until"] > time.time() else None
    return live


def unmarked(stored: Stored | None) -> Stored | None:
    """The stored line without its in-flight mark; None for a placeholder, a line in flight with no translation yet,
    which no response shows."""
    if stored is None or "lineId" not in stored:
        return None
    bare = {key: value for key, value in stored.items() if key != "inflight"}
    return bare


def batches(work: list[SourceLine], page: list[SourceLine]) -> list[Batch]:
    """`work` in batches of 40 in idx order, each with up to 3 of the page's lines on each side as context (C.7)."""
    position = {line.idx: at for at, line in enumerate(page)}
    made = []
    for first in range(0, len(work), BATCH_LINES):
        lines = work[first : first + BATCH_LINES]
        start = position[lines[0].idx]
        end = position[lines[-1].idx] + 1
        before = page[max(start - CONTEXT_LINES, 0) : start]
        after = page[end : end + CONTEXT_LINES]
        made.append(Batch(before, lines, after))
    return made


def checked(output: str, lines: list[SourceLine]) -> tuple[dict[int, str], list[str]]:
    """The answer's translations that pass every rule, by idx, and the rules it broke (D.4)."""
    limits = {line.idx: 4 * len(line.text) + 40 for line in lines}
    try:
        entries: Any = json.loads(output)["lines"]
    except ValueError, KeyError, TypeError:
        entries = None
    if not isinstance(entries, list):
        return {}, ['the answer must be JSON with a "lines" list']
    good: dict[int, str] = {}
    problems: list[str] = []
    seen: list[Any] = []
    for entry in entries:
        idx = entry.get("idx") if isinstance(entry, dict) else None
        text = entry.get("text") if isinstance(entry, dict) else None
        seen.append(idx)
        if not isinstance(idx, int) or idx not in limits:
            continue
        if not isinstance(text, str):
            problems.append(f"idx {idx} has no text string")
            continue
        flat = text.replace("\r\n", " ").replace("\n", " ").strip()
        if not flat:
            problems.append(f"idx {idx} is empty")
        elif len(flat) > limits[idx]:
            problems.append(f"idx {idx} is far longer than its source")
        else:
            good[idx] = flat
    if seen != list(limits):
        problems.append("the idx values must be exactly the lines in translate, in order, with no repeats")
    return good, problems


async def translate_batch(chain: list[Provider], plan: Plan, batch: Batch) -> tuple[dict[int, str], list[Call]]:
    """The batch's translations by idx; a line missing from it failed. A broken answer is asked again once with the
    rules it broke (D.5), and a model that doesn't answer fails the lines still missing (D.7)."""

    def entries(lines: list[SourceLine]) -> list[prompts.Entry]:
        return [{"idx": line.idx, "text": line.text} for line in lines]

    parts = (entries(batch.before), entries(batch.lines), entries(batch.after))
    script_note = languages.SCRIPT_NOTES.get(plan.target.code, "")
    turns = prompts.turns(
        plan.source, plan.source_name, plan.target.code, plan.target.name, script_note, plan.keep, parts
    )
    chars = sum(len(line.text) for line in batch.lines)
    max_tokens = 4 * math.ceil(chars / 3) + 200
    deadline = time.monotonic() + BATCH_DEADLINE_S
    calls: list[Call] = []
    good: dict[int, str] = {}
    try:
        output = await ask(chain, turns, max_tokens, deadline, calls, hedge_s=HEDGE_S)
        good, problems = checked(output, batch.lines)
        if problems:
            correction = prompts.correction(problems, [line.idx for line in batch.lines])
            retry = [*turns, ("assistant", output), ("user", correction)]
            output = await ask(chain, retry, max_tokens, deadline, calls, hedge_s=HEDGE_S)
            again, _ = checked(output, batch.lines)
            good = {**good, **again}
    except ProviderFailed:
        pass
    return good, calls


def keep_terms(speakers: list[str], title: str) -> list[str]:
    """Speaker names, then the title's words that start with a capital letter, each once, at most 50."""
    words = [word.strip(string.punctuation) for word in title.split()]
    capitals = [word for word in words if word[:1].isupper()]
    unique = list(dict.fromkeys([*speakers, *capitals]))
    return unique[:KEEP_TERMS]


async def locked_recording(session: AsyncSession, device_id: UUID, recording_id: UUID) -> Recording:
    try:
        found = await recordings.locked(session, device_id, recording_id)
    except CloudFailure as error:
        raise CloudFailure(404, "recording_not_found") from error
    return found


async def live_track(session: AsyncSession, recording_id: UUID, language: str) -> CaptionTrack | None:
    found = await session.scalar(
        select(CaptionTrack)
        .where(
            CaptionTrack.recording_id == recording_id,
            CaptionTrack.language == language,
            CaptionTrack.deleted_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    return found


async def plan_page(session: AsyncSession, device_id: UUID, recording_id: UUID, body: TranslateRequest) -> Plan:
    """Step one (C.1 to C.6), committed before any model call."""
    page = sorted(body.lines, key=lambda line: line.idx)
    if len(page) > PAGE_LINES:
        raise CloudFailure(413, "too_many_lines")
    if len({line.idx for line in page}) != len(page):
        raise CloudFailure(422, "invalid")
    recording = await locked_recording(session, device_id, recording_id)
    job = await session.scalar(
        select(TranscriptionJob).where(
            TranscriptionJob.recording_id == recording_id,
            TranscriptionJob.state == "ready",
            TranscriptionJob.deleted_at.is_(None),
        )
    )
    if job is None:
        raise CloudFailure(409, "transcript_not_ready")
    source = base(job.language_detected or "")
    target = languages.target(body.language, source)
    if target is None:
        raise CloudFailure(422, "language_unsupported")
    track = await live_track(session, recording_id, target.code)
    if track is None:
        track = CaptionTrack(
            id=uuid.uuid4(), recording_id=recording_id, language=target.code, source_language=source, lines=[]
        )
        session.add(track)
        await session.flush()
    stored = {line["idx"]: line for line in track.lines}
    until = int(time.time()) + IN_FLIGHT_S
    work: list[SourceLine] = []
    seen: dict[int, Stored] = {}
    awaited: dict[int, float] = {}
    for line in page:
        found = unmarked(stored.get(line.idx))
        if not body.force and matches(found, line):
            continue
        mark = live_mark(stored.get(line.idx))
        if mark is not None and mark["hash"] == line.sourceHash:
            awaited[line.idx] = mark["until"]
            continue
        work.append(line)
        flight = {"hash": line.sourceHash, "until": until}
        seen[line.idx] = {**found, "inflight": flight} if found is not None else {"idx": line.idx, "inflight": flight}
    day = None
    if work:
        day = await usage.reserve_lines(session, device_id, len(work))
        if day is None:
            await session.rollback()
            raise CloudFailure(429, "translation_limit")
    names = await session.scalars(
        select(Speaker.name).where(
            Speaker.recording_id == recording_id, Speaker.deleted_at.is_(None), Speaker.name.is_not(None)
        )
    )
    source_name = languages.source_name(source, job.language_requested == "hinglish")
    keep = keep_terms([name for name in names if name], recording.title)
    if seen:
        marked = {**stored, **seen}
        track.lines = [marked[idx] for idx in sorted(marked)]
    plan = Plan(track.id, target, source, source_name, keep, work, page, seen, day, awaited)
    await session.commit()
    return plan


def entry(line: SourceLine, text: str | None, previous: Stored | None, stamp: str) -> Stored:
    """The stored line for a translation, or for a failure (`text` None), which keeps a previous translation's text so
    it still shows (C.9)."""
    failed = text is None
    kept = previous if failed and previous is not None else {"text": None, "edited": False}
    made = {
        "idx": line.idx,
        "lineId": line.lineId,
        "sourceRev": line.rev,
        "sourceHash": line.sourceHash,
        "text": kept["text"] if failed else text,
        "failed": failed,
        "edited": kept["edited"],
        "updatedAt": stamp,
    }
    if previous is not None and "editRev" in previous:
        made["editRev"] = previous["editRev"]
    return made


def restored(lines: list[Stored], plan: Plan) -> list[Stored]:
    """The stored lines with the page's marks taken back: each line still as step one marked it is again what step one
    found, and a placeholder goes."""
    kept = []
    for line in lines:
        found = unmarked(line) if plan.seen.get(line["idx"]) == line else line
        if found is not None:
            kept.append(found)
    return kept


async def keep_answers(
    session: AsyncSession, device_id: UUID, recording_id: UUID, plan: Plan, answers: dict[int, str]
) -> tuple[CaptionTrack, list[int]]:
    """Step three (C.9, C.10): every answer written under the recording's lock, and the failed idx list. When no line
    got an answer, the page's marks are taken back, nothing else is written, the day's lines are refunded and the page
    is 502, for the app to send again."""
    await locked_recording(session, device_id, recording_id)
    track = await session.get(CaptionTrack, plan.track_id, populate_existing=True)
    if plan.work and not answers:
        if track is not None:
            track.lines = restored(track.lines, plan)
        if plan.day is not None:
            await usage.refund_lines(session, device_id, plan.day, len(plan.work))
        await session.commit()
        raise CloudFailure(502, "provider")
    if track is None or track.deleted_at is not None:
        raise CloudFailure(404, "track_not_found")
    stored = {line["idx"]: line for line in track.lines}
    stamp = now_text()
    failed = []
    for line in plan.work:
        previous = stored.get(line.idx)
        if previous != plan.seen.get(line.idx):
            continue
        text = answers.get(line.idx)
        if text is None:
            failed.append(line.idx)
        stored[line.idx] = entry(line, text, unmarked(previous), stamp)
    track.lines = [stored[idx] for idx in sorted(stored)]
    await session.commit()
    return track, failed


async def settled(
    session: AsyncSession, plan: Plan, track: CaptionTrack, failed: list[int]
) -> tuple[CaptionTrack, list[int]]:
    """The track once every line another request was translating is written or its mark ran out, read with no lock
    held; an awaited line that ended failed or never settled joins the failed list."""
    deadline = max(plan.awaited.values())
    pending = list(plan.awaited)
    while pending and time.time() < deadline:
        await asyncio.sleep(AWAIT_POLL_S)
        fresh = await session.get(CaptionTrack, plan.track_id, populate_existing=True)
        await session.commit()
        if fresh is None or fresh.deleted_at is not None:
            raise CloudFailure(404, "track_not_found")
        stored = {line["idx"]: line for line in fresh.lines}
        pending = [idx for idx in pending if live_mark(stored.get(idx)) is not None]
    stored = {line["idx"]: line for line in track.lines}
    unsettled = []
    for idx in plan.awaited:
        line = unmarked(stored.get(idx))
        if idx in pending or line is None or line["failed"]:
            unsettled.append(idx)
    every = sorted([*failed, *unsettled])
    return track, every


async def translate(
    session: AsyncSession, tiers: Tiers, device_id: UUID, recording_id: UUID, body: TranslateRequest
) -> tuple[CaptionTrack, list[int], list[Call]]:
    """One page into the track (algorithm C): the track, the idx of this page's lines that failed, and every model
    call, for the log."""
    plan = await plan_page(session, device_id, recording_id, body)
    chain = tiers.chain(tiers.translate or tiers.command)
    gate = asyncio.Semaphore(PARALLEL)

    async def run(batch: Batch) -> tuple[dict[int, str], list[Call]]:
        async with gate:
            result = await translate_batch(chain, plan, batch)
        return result

    results = await asyncio.gather(*(run(batch) for batch in batches(plan.work, plan.page)))
    answers = {idx: text for good, _ in results for idx, text in good.items()}
    calls = [call for _, made in results for call in made]
    track, failed = await keep_answers(session, device_id, recording_id, plan, answers)
    if plan.awaited:
        track, failed = await settled(session, plan, track, failed)
    return track, failed, calls


async def locked_track(session: AsyncSession, device_id: UUID, track_id: UUID) -> CaptionTrack:
    """The track, read under its recording's lock (docs/cloud.md, Locks); deleted ones too, for an idempotent DELETE.
    Another device's track, a trashed recording's and an unknown one are 404: the lock checks the recording."""
    located = await session.get(CaptionTrack, track_id)
    if located is None:
        raise CloudFailure(404, "track_not_found")
    try:
        await recordings.locked(session, device_id, located.recording_id)
    except CloudFailure as error:
        raise CloudFailure(404, "track_not_found") from error
    fresh = await session.get(CaptionTrack, track_id, populate_existing=True)
    if fresh is None:
        raise CloudFailure(404, "track_not_found")
    return fresh


async def edit_line(session: AsyncSession, device_id: UUID, track_id: UUID, idx: int, body: LineEdit) -> Stored:
    """A user's fix of one translated line (TRX-10): `edited`, no longer failed. An edit whose `rev` is older than
    the stored one changes nothing, so a late resend can't undo a newer fix; the stored line comes back either way."""
    track = await locked_track(session, device_id, track_id)
    if track.deleted_at is not None:
        raise CloudFailure(404, "track_not_found")
    lines = list(track.lines)
    at = next((position for position, line in enumerate(lines) if line["idx"] == idx), None)
    line = unmarked(lines[at]) if at is not None else None
    if at is None or line is None:
        raise CloudFailure(404, "line_not_found")
    if body.rev < line.get("editRev", -1):
        await session.commit()
        return line
    edited = {
        **line,
        "text": body.text,
        "edited": body.edited,
        "failed": body.failed,
        "editRev": body.rev,
        "updatedAt": now_text(),
    }
    lines[at] = edited
    track.lines = lines
    await session.commit()
    return edited


async def delete_track(session: AsyncSession, device_id: UUID, track_id: UUID) -> None:
    """Soft delete; again is a no-op."""
    track = await locked_track(session, device_id, track_id)
    if track.deleted_at is None:
        track.deleted_at = func.now()
    await session.commit()
