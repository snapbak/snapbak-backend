"""Meeting minutes (MEETINGS.md 7, minutes v2 of 26 Oct 2026): one model call per section, in CALLS order, each
checked by deterministic rules that every claim's derivation is a real line; the rules judge the model, never the
reverse. A section stands or fails alone, and the run stores each as it lands, so it resumes at the first pending
section. At the end the detail guard (services/details.py) looks for transcript details the minutes left out and
asks for them once."""

import asyncio
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from snapbak_backend.ai.chain import Call, ProviderFailed, Tiers, ask
from snapbak_backend.ai.prompts import minutes as prompt
from snapbak_backend.db.models import Speaker, TranscriptLine
from snapbak_backend.schemas.cloud import (
    ActionItemOut,
    AgendaOut,
    ClaimOut,
    DecisionOut,
    DerivationOut,
    Incomplete,
    MinutesOut,
    NotCommitmentOut,
    RiskOut,
    Section,
    TopicOut,
)
from snapbak_backend.services import details
from snapbak_backend.services.folding import DEVANAGARI, quoted
from snapbak_backend.services.recordings import speaker_of

# The calls, in the order they run: title and summary first, so the first value comes fast.
CALLS: tuple[Section, ...] = (
    "summary",
    "mom",
    "actionItems",
    "decisions",
    "openQuestions",
    "risks",
    "followUps",
    "nextAgenda",
    "agenda",
    "contradictions",
)
# What each item section's items are on the wire, and the fewest checked derivations each must keep.
ITEMS: dict[str, tuple[type[BaseModel], int]] = {
    "actionItems": (ActionItemOut, 1),
    "decisions": (DecisionOut, 1),
    "openQuestions": (ClaimOut, 1),
    "risks": (RiskOut, 1),
    "followUps": (ActionItemOut, 1),
    "nextAgenda": (TopicOut, 1),
    "agenda": (AgendaOut, 1),
    "contradictions": (ClaimOut, 2),
}
# Each call's room to answer: the founder's limit, 64,000 output tokens a call.
MAX_TOKENS = 64_000
SECTION_DEADLINE_S = 180
# A section not answered in this long is stuck: the fallback joins it and the first answer wins. Well past the
# slowest section measured on a 30 min meeting (24 s, 26 Oct 2026), so one answering in its usual time is never
# doubled; a stuck call once held a run for 180 s, past MOM-04's 3 min.
SECTION_HEDGE_S = 40
# A section's model is asked once more, 2 s after it fails; then the section is marked, and a longer outage is the
# sweep's (meetings.MAX_ATTEMPTS runs, 15 minutes apart): one owner, so calls never multiply.
TRIES = 2
RETRY_S = (2,)
SHAPE = "the answer was not the JSON shape asked for"
NOT_ENGLISH = "the text was not all English: translate what was said in another language"
# Each item section's id prefix: the server numbers the items itself (`checked_items`).
PREFIXES = {
    "actionItems": "act",
    "decisions": "dec",
    "openQuestions": "q",
    "risks": "risk",
    "followUps": "fu",
    "nextAgenda": "next",
    "agenda": "ag",
    "contradictions": "con",
}
# The detail guard asks for the details left out when the minutes keep fewer than this share of them.
DETAIL_TARGET = 0.95


class SectionFailed(Exception):
    """One section's run failed for good: `code` is `bad_answer` or `provider`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RawDerivation(BaseModel):
    """What the model sends for one derivation (ai/prompts/minutes.py): the line's `i`, not yet its UUID."""

    model_config = ConfigDict(extra="ignore")
    i: int
    speaker: str
    atMs: int = Field(ge=0)
    quote: str


def derivation_ok(entry: RawDerivation, line: TranscriptLine, speaker: Speaker | None) -> bool:
    """The entry's time is inside the line's, its speaker is the line's, and its quote is the line's words."""
    if not line.start_ms <= entry.atMs <= line.end_ms:
        return False
    if entry.speaker.strip() != speaker_of(speaker).strip():
        return False
    return quoted(entry.quote, line.text)


def keep_derivations(
    entries: list[RawDerivation],
    by_index: dict[int, TranscriptLine],
    speakers: dict[UUID, Speaker],
) -> list[DerivationOut]:
    """The entries that check out, as wire bodies with the line's UUID; an entry naming an unknown line `i` is
    refused."""
    kept: list[DerivationOut] = []
    for entry in entries:
        line = by_index.get(entry.i)
        if line is None:
            continue
        if not derivation_ok(entry, line, speakers.get(line.speaker_id)):
            continue
        made = DerivationOut(lineId=line.id, speaker=entry.speaker.strip(), atMs=entry.atMs, quote=entry.quote)
        kept.append(made)
    return kept


def body_of(lines: list[TranscriptLine], speakers: dict[UUID, Speaker]) -> list[dict[str, Any]]:
    """The lines as every call sends them."""
    return [
        {
            "i": line.idx,
            "s": line.start_ms,
            "e": line.end_ms,
            "sname": speaker_of(speakers.get(line.speaker_id)),
            "text": line.text,
        }
        for line in lines
    ]


def checked_items(
    section: str, raw: Any, by_index: dict[int, TranscriptLine], speakers: dict[UUID, Speaker], problems: list[str]
) -> list[Any]:
    """The section's items whose derivations check out (a contradiction's on two different lines) and whose shape the
    wire takes; each one refused adds its problem."""
    model, fewest = ITEMS[section]
    kept_items: list[Any] = []
    for item in raw if isinstance(raw, list) else []:
        item_id = str(item.get("id", "?"))[:40] if isinstance(item, dict) else "?"
        try:
            entries = [RawDerivation.model_validate(entry) for entry in item.get("derivedFrom") or []]
            kept = keep_derivations(entries, by_index, speakers)
            if len({entry.lineId for entry in kept}) < fewest:
                problems.append(f"{section} {item_id} had too few derivations that are real lines")
                continue
            fields = {name: item[name] for name in model.model_fields if name in item and name != "byUser"}
            written = [fields.get(name) for name in ("text", "topic", "rationale", "due")] + [
                *(fields.get("alternatives") or [])
            ]
            if not all(english(str(said or "")) for said in written):
                problems.append(f"{section} {item_id} was not all English")
                continue
            if "due" in fields:
                fields["due"] = due_of(fields["due"])
            if not real_date(fields.get("due")):
                problems.append(f"{section} {item_id} had a due that looks like a date but is none")
                continue
            kept_items.append(model.model_validate({**fields, "derivedFrom": kept, "byUser": False}))
        except ValueError, AttributeError:
            problems.append(f"{section} {item_id} was not the shape asked for")
    # The ids are the server's, never the model's (whose `act_1` repeats across runs and within a section): unique in
    # the call, so an edit resent over a newer run can never take another item's citations (grading 4).
    tag = uuid.uuid4().hex[:6]
    return [item.model_copy(update={"id": f"{PREFIXES[section]}_{tag}_{n}"}) for n, item in enumerate(kept_items, 1)]


def parse_section(
    section: str, answer: str, by_index: dict[int, TranscriptLine], speakers: dict[UUID, Speaker]
) -> tuple[dict[str, Any] | None, list[str]]:
    """The section's answer as the document's fields with every refused item left out, or None with its problem
    when the JSON is not the shape. The problems feed the section's one correction turn."""
    problems: list[str] = []
    try:
        said = json.loads(answer)
        if section == "summary":
            value = {"title": text_of(said.get("title")) or None, "summary": text_of(said["summary"])}
            ok = bool(value["summary"])
        elif section == "mom":
            value = {"mom": text_of(said["mom"])}
            ok = bool(value["mom"])
        else:
            value = {section: checked_items(section, said["items"], by_index, speakers, problems)}
            ok = isinstance(said["items"], list)
    except ValueError, KeyError, TypeError:
        return None, [SHAPE]
    if not ok:
        return None, [SHAPE]
    if section in ("summary", "mom") and not all(english(str(text or "")) for text in value.values()):
        return None, [NOT_ENGLISH]
    return value, problems


ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def due_of(said: Any) -> str | None:
    """A due as stored: the text stripped, an empty one none."""
    return said.strip() or None if isinstance(said, str) else None


def real_date(due: Any) -> bool:
    """A due that looks like YYYY-MM-DD is a real calendar day (the app formats it as one); any other text is the
    deadline as said."""
    if not isinstance(due, str) or not ISO_DAY.fullmatch(due):
        return True
    try:
        date.fromisoformat(due)
    except ValueError:
        return False
    return True


def text_of(said: Any) -> str:
    """A text field as the model sent it: a string, stripped; anything else (a JSON null) is no text."""
    return said.strip() if isinstance(said, str) else ""


def english(text: str) -> bool:
    """No Devanagari in what the model wrote: the minutes are always English (MOM-05, D26); only quotes keep the
    script they were spoken in."""
    return not any(DEVANAGARI[0] <= char <= DEVANAGARI[1] for char in text)


async def one_try(chain: list[Any], turns: list[Any], calls: list[Call]) -> str:
    deadline = time.monotonic() + SECTION_DEADLINE_S
    return await ask(chain, turns, MAX_TOKENS, deadline, calls, hedge_s=SECTION_HEDGE_S)


async def asked(chain: list[Any], turns: list[Any], calls: list[Call]) -> str:
    """One model call for a section, asked again after RETRY_S when the provider fails, TRIES in all; the fallback
    joins only a call stuck past SECTION_HEDGE_S (unattended batch work: one answering in its usual time is never
    doubled)."""
    for wait_s in RETRY_S[: TRIES - 1]:
        try:
            return await one_try(chain, turns, calls)
        except ProviderFailed:
            await asyncio.sleep(wait_s)
    # The last try's failure is the section's.
    return await one_try(chain, turns, calls)


async def write_section(
    tiers: Tiers,
    section: str,
    title: str,
    pins: list[dict[str, Any]],
    lines: list[TranscriptLine],
    speakers: dict[UUID, Speaker],
    calls: list[Call],
    headings: list[str] | None = None,
    day: str | None = None,
) -> dict[str, Any]:
    """One section, corrected at most once (`headings`: the MoM's, for the agenda to name its topics by); the
    repair sees its own first answer and what was refused in it, and a repair that fails or doesn't parse leaves
    the first answer's checked items standing. SectionFailed when no acceptable answer arrives."""
    by_index = {line.idx: line for line in lines}
    chain = tiers.chain(tiers.polish)
    turns = prompt.turns(section, title, pins, body_of(lines, speakers), headings, day)
    try:
        first = await asked(chain, turns, calls)
    except ProviderFailed:
        raise SectionFailed("provider") from None
    value, problems = parse_section(section, first, by_index, speakers)
    if value is not None and not problems:
        return value
    retry = [*turns, ("assistant", first), ("user", prompt.correction(problems))]
    try:
        second = await asked(chain, retry, calls)
    except ProviderFailed:
        second = ""
    fixed, _ = parse_section(section, second, by_index, speakers)
    best = fixed or value
    if best is None:
        raise SectionFailed("bad_answer")
    return best


def empty(pending: list[str]) -> MinutesOut:
    """A document with nothing written yet and `pending` to come."""
    return MinutesOut.model_validate(
        {"summary": "", "mom": "", "actionItems": [], "openQuestions": [], "decisions": [], "pending": pending}
    )


def unique_ids(items: list[Any]) -> list[Any]:
    """A section's items with every id its own: an id seen before in the section becomes `<id>_<position>`, its
    index there from 0, the rule Studio applies to what it reads, so a save never sends an id the server doesn't
    know (grading 4)."""
    seen: set[str] = set()
    made: list[Any] = []
    for position, item in enumerate(items):
        named = item.id
        if named in seen:
            item = item.model_copy(update={"id": f"{named}_{position}"})
        seen.add(item.id)
        made.append(item)
    return made


def merged(doc: MinutesOut, value: dict[str, Any]) -> MinutesOut:
    """The section's new AI content in the document: a text the user edited stays theirs, and the user's own or
    edited items stay beside the new AI ones (a regenerate replaces only AI items)."""
    update: dict[str, Any] = {}
    for name, made in value.items():
        if name in ITEMS:
            # A user's item kept beside the new AI ones gets an id of its own if one of them took it.
            mine = [item for item in getattr(doc, name) if item.byUser]
            update[name] = unique_ids([*made, *mine])
        elif name not in doc.userEdited:
            update[name] = made
    return doc.model_copy(update=update)


async def complete(
    tiers: Tiers, doc: MinutesOut, lines: list[TranscriptLine], speakers: dict[UUID, Speaker], calls: list[Call]
) -> MinutesOut:
    """The detail guard: when the minutes keep fewer than DETAIL_TARGET of the transcript's details, a commitment is
    uncovered, or an agenda topic has no section, one completion call works the missing ones in (its new action
    items checked as any), and one more answers for any commitment the first left."""
    units = details.inventory(lines, speakers)
    before = details.recall(doc, units, lines)
    left = details.missing(doc, units, lines)
    topics = details.topics_missing(doc)
    # Every commitment must end an action item or a judgment that it is none, whatever the overall recall.
    promised = {unit.idx for unit in left if unit.kind == "commitment"}
    if before >= DETAIL_TARGET and not promised and not topics:
        return doc
    asked_for = [{"i": unit.idx, "kind": unit.kind, "text": unit.text} for unit in left]
    asked_for += [{"i": None, "kind": "topic", "text": topic} for topic in topics]
    better = await completed(tiers, doc, asked_for, promised, lines, speakers, calls) or doc
    # A long meeting's first answer may leave commitments unanswered: one more pass asks for those alone.
    still = [unit for unit in details.missing(better, units, lines) if unit.kind == "commitment"]
    if still and better is not doc:
        again = [{"i": unit.idx, "kind": unit.kind, "text": unit.text} for unit in still]
        better = await completed(tiers, better, again, {unit.idx for unit in still}, lines, speakers, calls) or better
    after, left_topics = details.recall(better, units, lines), len(details.topics_missing(better))
    # Only ever forward: neither measure worse, and one of them better.
    worse = after < before or left_topics > len(topics)
    if worse or (after, -left_topics) == (before, -len(topics)):
        return doc
    return better


async def completed(
    tiers: Tiers,
    doc: MinutesOut,
    asked_for: list[dict[str, Any]],
    promised: set[int],
    lines: list[TranscriptLine],
    speakers: dict[UUID, Speaker],
    calls: list[Call],
) -> MinutesOut | None:
    """One completion call for `asked_for`, its answer worked into the document (new action items checked as any,
    judgments only on the `promised` lines); None when it gave nothing usable."""
    turns = prompt.completion_turns(doc.summary, doc.mom, asked_for, body_of(lines, speakers))
    try:
        said = json.loads(await asked(tiers.chain(tiers.polish), turns, calls))
        by_index = {line.idx: line for line in lines}
        added = checked_items("actionItems", said.get("actionItems") or [], by_index, speakers, [])
        # A text that isn't English (MOM-05) or is empty leaves the one it would replace standing.
        texts = {name: text_of(said.get(name)) for name in ("summary", "mom")}
        texts = {name: text for name, text in texts.items() if text and english(text)}
        judged = judgments(said.get("notCommitments"), promised, by_index)
    except ProviderFailed, ValueError, AttributeError, TypeError:
        return None
    fresh = added
    known_lines = {item.lineId for item in doc.notCommitments}
    kept = [*doc.notCommitments, *(item for item in judged if item.lineId not in known_lines)]
    update = {"actionItems": unique_ids([*doc.actionItems, *fresh]), "notCommitments": kept}
    better = merged(doc, texts).model_copy(update=update)
    # New text that loses a topic's `### ` heading is left out; the answer's items still count.
    if len(details.topics_missing(better)) > len(details.topics_missing(doc)):
        return doc.model_copy(update=update)
    return better


def judgments(raw: Any, promised: set[int], by_index: dict[int, TranscriptLine]) -> list[NotCommitmentOut]:
    """The completion's "no commitment" judgments, each on a line the guard asked about; any other is dropped."""
    found: list[NotCommitmentOut] = []
    for item in raw if isinstance(raw, list) else []:
        idx = item.get("i") if isinstance(item, dict) else None
        reason = str(item.get("reason") or "").strip() if isinstance(item, dict) else ""
        if idx in promised and reason:
            found.append(NotCommitmentOut(lineId=by_index[idx].id, reason=reason))
    return found


type Saved = Callable[[MinutesOut], Awaitable[None]]


TEXTS = ("title", "summary", "mom")


def edited(stored: MinutesOut, sent: MinutesOut) -> MinutesOut:
    """The user's edit of the stored minutes: an item that is new or changed is theirs (`byUser`) and keeps only
    the evidence the server checked for it (none for a new one); a text they changed joins `userEdited`. The run's
    own fields (pins, pending, incomplete) stay the server's."""
    update: dict[str, Any] = {}
    for name in ITEMS:
        before = {item.id: item for item in getattr(stored, name)}
        items = []
        # A repeated id is made unique first, as Studio makes it: the second of two is new to the server.
        for item in unique_ids(getattr(sent, name)):
            old = before.get(item.id)
            same = old is not None and item.model_dump(exclude={"byUser"}) == old.model_dump(exclude={"byUser"})
            mine = old.byUser if old is not None and same else True
            items.append(item.model_copy(update={"byUser": mine, "derivedFrom": old.derivedFrom if old else []}))
        update[name] = items
    changed = {name for name in TEXTS if getattr(sent, name) != getattr(stored, name)}
    update["userEdited"] = [name for name in TEXTS if name in changed or name in stored.userEdited]
    update |= {"pinned": stored.pinned, "pending": stored.pending, "incomplete": stored.incomplete}
    update["notCommitments"] = stored.notCommitments
    return sent.model_copy(update=update)


def failure(doc: MinutesOut) -> str | None:
    """The code a run fails with: only when neither the summary nor the minutes could be written (the summary's
    code); with either one the minutes stand, partial."""
    codes = {item.section: item.code for item in doc.incomplete}
    if "summary" in codes and "mom" in codes:
        return codes["summary"]
    return None


async def run(
    tiers: Tiers,
    title: str,
    pins: list[dict[str, Any]],
    lines: list[TranscriptLine],
    speakers: dict[UUID, Speaker],
    doc: MinutesOut,
    saved: Saved,
    day: str | None = None,
) -> tuple[MinutesOut, str]:
    """The document's pending sections, one call each in CALLS order, each handed to `saved` (stored and published)
    the moment it lands; a section that fails is marked `incomplete` with its code and the rest go on. Then the
    detail guard, saved too. The engine that answered last."""
    calls: list[Call] = []
    todo: list[Section] = [name for name in CALLS if name in doc.pending]
    for section in todo:
        try:
            # The agenda names its topics by the MoM's own headings, written before it.
            headings = [line[4:].strip() for line in doc.mom.splitlines() if line.startswith("### ")]
            named = headings if section == "agenda" else None
            value = await write_section(tiers, section, title, pins, lines, speakers, calls, named, day)
            doc = merged(doc, value)
        except SectionFailed as failed:
            incomplete = [item for item in doc.incomplete if item.section != section]
            doc = doc.model_copy(update={"incomplete": [*incomplete, Incomplete(section=section, code=failed.code)]})
        doc = doc.model_copy(update={"pending": [name for name in doc.pending if name != section]})
        await saved(doc)
    failed = {item.section for item in doc.incomplete}
    if not failed & {"summary", "mom"}:
        doc = await complete(tiers, doc, lines, speakers, calls)
        await saved(doc)
    answered = [call.engine for call in calls if call.verdict == "ok"]
    return doc, answered[-1] if answered else tiers.polish.engine


def chunks_of(doc: MinutesOut) -> list[tuple[str, int, str]]:
    """The minutes' embedded sections (D38): the summary whole, each action item, decision (with its why) and open
    question on its own."""
    rows: list[tuple[str, int, str]] = [("summary", 0, doc.summary)]
    for idx, item in enumerate(doc.actionItems):
        owner = f" owner {item.owner}" if item.owner else ""
        due = f" due {item.due}" if item.due else ""
        rows.append(("action", idx, f"{item.text}{owner}{due}"))
    for idx, decision in enumerate(doc.decisions):
        why = f" because {decision.rationale}" if decision.rationale else ""
        rows.append(("decision", idx, f"{decision.text}{why}"))
    for idx, claim in enumerate(doc.openQuestions):
        rows.append(("question", idx, claim.text))
    # An empty body (a summary that failed, or one the user cleared) is a 400 from the embeddings API.
    return [row for row in rows if row[2].strip()]
