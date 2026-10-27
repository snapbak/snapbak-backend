"""The people the minutes name (MEETINGS.md D41): the model writes a speaker as `{{p:N}}`, N the number of their label
"Speaker N" (unique in a recording and never renumbered), and someone who never speaks as `{{n:Name}}`. The stored
minutes keep the tokens, so the app shows each person by the name they have now, however often they are renamed;
every reader that needs words (search, Ask, the detail guard, the share page) takes `rendered_doc`."""

import re
from collections.abc import Callable, Iterable
from typing import Any
from uuid import UUID

from snapbak_backend.db.models import Speaker, TranscriptLine
from snapbak_backend.schemas.cloud import MinutesOut
from snapbak_backend.services.recordings import speaker_of

TOKEN = re.compile(r"\{\{p:(\d+)\}\}|\{\{n:([^{}]+)\}\}")
# A token, or anything the model wrote that starts like one and isn't ("{{p:3}", "{{ p:3 }}"): never shown.
WRITTEN = re.compile(TOKEN.pattern + r"|\{\{[^{}]*\}{0,2}")
LABEL = re.compile(r"Speaker (\d+)")
# The item sections, and the words of an item that can name people.
SECTIONS = ("actionItems", "decisions", "openQuestions", "risks", "followUps", "nextAgenda", "agenda", "contradictions")
WORDS = ("text", "topic", "owner", "rationale")
# A token for a speaker gone from the transcript, as words.
SOMEONE = "someone"


def number_of_label(label: str) -> int | None:
    """The N of a "Speaker N" label: its speaker's `p`."""
    found = LABEL.fullmatch(label)
    return int(found[1]) if found else None


def number_of(speaker: Speaker) -> int | None:
    return number_of_label(speaker.label)


def by_number(speakers: Iterable[Speaker]) -> dict[int, Speaker]:
    numbered = {n: speaker for speaker in speakers if (n := number_of(speaker)) is not None}
    return numbered


def checked(text: str, speakers: dict[UUID, Speaker], problems: list[str], where: str) -> str:
    """The model's words with every token grounded: a `{{p:N}}` no speaker has is refused (its problem feeds the
    correction turn) and left out; a `{{n:Name}}` that is a speaker's name now becomes their `{{p:N}}`."""
    numbered = by_number(speakers.values())
    named = {speaker_of(speaker).casefold(): n for n, speaker in numbered.items()}
    broken: list[str] = []

    def grounded(match: re.Match[str]) -> str:
        label, name = match.groups()
        if name is not None:
            n = named.get(name.strip().casefold())
            return f"{{{{n:{name.strip()}}}}}" if n is None else f"{{{{p:{n}}}}}"
        if label is None:
            broken.append(match[0])
            return ""
        if int(label) in numbered:
            return match[0]
        problems.append(f"{where} named {match[0]}, who is no speaker: use a line's p")
        return ""

    made = WRITTEN.sub(grounded, text)
    if broken:
        problems.append(f"{where} wrote {', '.join(broken)}: a token is {{{{p:N}}}} or {{{{n:Name}}}}")
    return made


def rendered(text: str, speakers: dict[UUID, Speaker]) -> str:
    """The words with each person by the name they have now."""
    numbered = by_number(speakers.values())

    def named(match: re.Match[str]) -> str:
        label, name = match.groups()
        if name is not None:
            return name
        speaker = numbered.get(int(label))
        return speaker_of(speaker) if speaker else SOMEONE

    made = TOKEN.sub(named, text)
    return made


def item_words(field: Callable[[str], Any], change: Callable[[str], str]) -> dict[str, Any]:
    """An item's words that can name people, each changed; `field` reads one of its fields by name (an item, or the
    model's raw fields)."""
    words: dict[str, Any] = {name: change(value) for name in WORDS if isinstance(value := field(name), str)}
    alternatives = field("alternatives")
    if isinstance(alternatives, list):
        words["alternatives"] = [change(each) if isinstance(each, str) else each for each in alternatives]
    return words


def texts(doc: MinutesOut, change: Callable[[str], str]) -> MinutesOut:
    """The document with every one of its words that can name people changed: title, summary, minutes, and each
    item's text, topic, owner, rationale and alternatives."""
    update: dict[str, Any] = {"summary": change(doc.summary), "mom": change(doc.mom)}
    if doc.title:
        update["title"] = change(doc.title)
    for section in SECTIONS:
        update[section] = [
            item.model_copy(update=item_words(lambda name, item=item: getattr(item, name, None), change))
            for item in getattr(doc, section)
        ]
    made = doc.model_copy(update=update)
    return made


def recited(item: Any, said_by: dict[UUID, str]) -> Any:
    """An item whose cites each name their line's speaker as they are now (a line gone keeps the name it had)."""
    entries = [
        entry.model_copy(update={"speaker": said_by.get(entry.lineId) or entry.speaker}) for entry in item.derivedFrom
    ]
    made = item.model_copy(update={"derivedFrom": entries})
    return made


def rendered_doc(doc: MinutesOut, speakers: dict[UUID, Speaker], lines: Iterable[TranscriptLine] = ()) -> MinutesOut:
    """The minutes with each person by the name they have now, for a reader that needs words, never to store; given
    the `lines`, each cite names its line's speaker as they are now too (a line gone keeps the name it had)."""
    plain = texts(doc, lambda text: rendered(text, speakers))
    said_by = {line.id: speaker_of(speakers.get(line.speaker_id)) for line in lines}
    if not said_by:
        return plain
    update = {section: [recited(item, said_by) for item in getattr(plain, section)] for section in SECTIONS}
    made = plain.model_copy(update=update)
    return made


def named_owners(rows: list[Any], speakers: dict[Any, dict[UUID, Speaker]]) -> list[dict[str, Any]]:
    """Search rows (`recording_id`, `owner`) with each owner's people by the name they have now, `speakers` by
    recording."""
    made = []
    for row in rows:
        owner, known = row["owner"], speakers.get(row["recording_id"], {})
        made.append({**row, "owner": rendered(owner, known) if owner else owner})
    return made


def retold(doc: MinutesOut, merges: dict[int, int]) -> MinutesOut:
    """The minutes after speakers merged (MOM-08): each merged speaker's `{{p:N}}` becomes the one kept last, along
    a chain of merges too (7 into 4, then 4 into 2: 7 is 2)."""
    last: dict[int, int] = {}
    for start in merges:
        into, seen = merges[start], {start}
        while into in merges and into not in seen:
            seen.add(into)
            into = merges[into]
        last[start] = into

    def moved(match: re.Match[str]) -> str:
        label = match[1]
        if label is None or int(label) not in last:
            return match[0]
        return f"{{{{p:{last[int(label)]}}}}}"

    made = texts(doc, lambda text: TOKEN.sub(moved, text))
    return made
