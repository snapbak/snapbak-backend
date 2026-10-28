"""A meeting's minutes and transcript as the share page shows them (D16, D19): the minutes card in the app's order,
its small markdown renderer, and the timed transcript. Every character the model or a speaker wrote is escaped
before any markup is made."""

import html
import re
from collections.abc import Sequence
from datetime import date

from snapbak_backend.schemas.cloud import (
    ActionItemOut,
    AgendaOut,
    ClaimOut,
    DecisionOut,
    MinutesOut,
    RiskOut,
    TopicOut,
)

BOLD = re.compile(r"\*\*(.+?)\*\*")

# A meeting line as the page shows it: when it starts (ms), who said it, what they said.
type Said = tuple[int, str, str]


def clock(ms: int) -> str:
    """`1:02`, or `1:02:03` past an hour."""
    minutes, seconds = divmod(ms // 1000, 60)
    hours, minutes = divmod(minutes, 60)
    shown = f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"
    return shown


def stamp(ms: int) -> str:
    """A moment in the meeting as the app writes one (library/format.ts `clock`): `00:21`, `13:32`, `1:02:03`."""
    return clock(ms).rjust(5, "0")


def due(said: str) -> str:
    """An action item's due date as the app writes it (minutes.ts `due`): "2026-10-31" is "Fri 31 Oct"; anything
    that is no date stays as the model wrote it."""
    try:
        when = date.fromisoformat(said)
    except ValueError:
        return said
    return f"{when:%a} {when.day} {when:%b}"


def inline(text: str) -> str:
    """One line of the minutes' markdown: escaped first, then only `**bold**` comes back as markup."""
    made = BOLD.sub(r"<strong>\1</strong>", html.escape(text))
    return made


def markdown(text: str) -> str:
    """The minutes' markdown (`mom`) as HTML, by a renderer this small on purpose: headings, `- ` lists (the only
    ones the app's renderer reads), `**bold**` and paragraphs. Every character is escaped before any markup is
    made, so nothing the model wrote becomes a tag."""
    blocks: list[str] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        rows = [row.strip() for row in block.split("\n") if row.strip()]
        if rows and rows[0].startswith("#"):
            blocks.append(f"<h3>{inline(rows.pop(0).lstrip('#').strip())}</h3>")
        if rows and all(row.startswith("- ") for row in rows):
            items = "".join(f"<li>{inline(row[2:])}</li>" for row in rows)
            blocks.append(f"<ul>{items}</ul>")
        elif rows:
            blocks.append(f"<p>{inline(' '.join(rows))}</p>")
    return "".join(blocks)


def at(ms: int) -> str:
    """A transcript line's time, which seeks the player (share.js)."""
    made = f'<a class="at" href="#t={ms // 1000}">{stamp(ms)}</a>'
    return made


class Colours:
    """Each speaker's colour (1 to 8, as the app's transcript chips), in order of first appearance."""

    def __init__(self) -> None:
        self.given: dict[str, int] = {}

    def of(self, name: str) -> int:
        if name not in self.given:
            self.given[name] = len(self.given) % 8 + 1
        return self.given[name]


def cite(ms: int, speaker: str | None, colours: Colours) -> str:
    """A chip that seeks the player to where a claim was said: the speaker's dot and name, then the time."""
    if speaker is None:
        return f'<a class="cite" href="#t={ms // 1000}"><i></i><b>{stamp(ms)}</b></a>'
    made = (
        f'<a class="cite s{colours.of(speaker)}" href="#t={ms // 1000}"><i></i>{html.escape(speaker)} '
        f"<b>{stamp(ms)}</b></a>"
    )
    return made


type Item = ClaimOut | ActionItemOut | TopicOut

# An agenda topic's status as the app words it.
STATUS = {"covered": "Covered", "partial": "Partly covered", "unresolved": "Unresolved"}


def sources(claim: Item, colours: Colours) -> str:
    """Who said it and when, each a cite; an action item, a follow-up or a risk leads with its owner (and due)."""
    said = [cite(entry.atMs, entry.speaker, colours) for entry in claim.derivedFrom]
    if isinstance(claim, ActionItemOut | RiskOut):
        owner = html.escape(claim.owner or "No owner")
        when = getattr(claim, "due", None)
        said.insert(0, owner if when is None else f"{owner} · Due {html.escape(due(when))}")
    made = f'<span class="src">{"".join(said)}</span>'
    return made


def said_of(item: Item) -> str:
    """An item's own words: a topic (with its status on the agenda), a risk's kind, a decision's why and what was
    not chosen."""
    if isinstance(item, TopicOut):
        status = f' <span class="muted">· {STATUS[item.status]}</span>' if isinstance(item, AgendaOut) else ""
        return html.escape(item.topic) + status
    text = html.escape(item.text)
    if isinstance(item, RiskOut) and item.kind != "risk":
        text = f"<b>{item.kind.capitalize()}</b> {text}"
    if isinstance(item, DecisionOut) and item.rationale:
        text += f'<span class="src">Why: {html.escape(item.rationale)}</span>'
    if isinstance(item, DecisionOut) and item.alternatives:
        text += f'<span class="src">Not chosen: {html.escape("; ".join(item.alternatives))}</span>'
    return text


def listed(title: str, claims: Sequence[Item], colours: Colours) -> str:
    if not claims:
        return ""
    items = "".join(f"<li>{said_of(claim)}{sources(claim, colours)}</li>" for claim in claims)
    made = f"<h2>{title}</h2><ul>{items}</ul>"
    return made


def minutes_card(minutes: MinutesOut, colours: Colours) -> str:
    """The minutes in the app's order and with its headings (web/studio sections.ts, MEETINGS.md 6.2): summary,
    minutes, action items, decisions, open questions, risks, follow-ups, the next meeting's topics, agenda
    coverage, contradictions, pins. A section with nothing in it (or not written yet) is left out."""
    pins = "".join(
        f"<li>{html.escape(pin.note) if pin.note else '<span class="muted">Pinned moment</span>'}"
        f'<span class="src">{cite(pin.atMs, None, colours)}</span></li>'
        for pin in minutes.pinned
    )
    parts = [
        f"<h2>Summary</h2><p>{html.escape(minutes.summary)}</p>" if minutes.summary else "",
        f"<h2>Minutes</h2>{markdown(minutes.mom)}" if minutes.mom else "",
        listed("Action items", minutes.actionItems, colours),
        listed("Decisions", minutes.decisions, colours),
        listed("Open questions", minutes.openQuestions, colours),
        listed("Risks and blockers", minutes.risks, colours),
        listed("Follow-ups", minutes.followUps, colours),
        listed("Next meeting", minutes.nextAgenda, colours),
        listed("Agenda coverage", minutes.agenda, colours),
        listed("Contradictions", minutes.contradictions, colours),
        f"<h2>Pins</h2><ul>{pins}</ul>" if pins else "",
    ]
    made = f'<article class="card plate">{"".join(parts)}</article>'
    return made


def meeting_sections(minutes: MinutesOut | None, lines: list[Said]) -> str:
    """A meeting's minutes beside its full transcript (D16), from the same JSON the app reads. Each speaker keeps
    one colour across both. The transcript grows with the meeting (a 2 h one is a few hundred KB); only the page
    around it is held to the 100 KB budget."""
    colours = Colours()
    said = "".join(
        f'<li><div class="who"><span class="speaker s{colours.of(who)}">{html.escape(who)}</span>{at(start_ms)}'
        f"</div>{html.escape(text)}</li>"
        for start_ms, who, text in lines
    )
    card = "" if minutes is None else minutes_card(minutes, colours)
    made = f'<div class="cols">{card}<aside class="card plate tx"><h2>Transcript</h2><ol>{said}</ol></aside></div>'
    return made
