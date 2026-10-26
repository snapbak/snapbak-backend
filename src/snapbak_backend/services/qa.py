"""One meeting, one question (MEETINGS.md 5.3, D18), retrieved in the founder's order (26 Oct 2026): the minutes'
sections nearest the question by meaning first, each with the transcript lines it cites; then the lines that name
the question's words; answered in one call from them and the sections, with citations checked by the same
deterministic rule as the minutes. An answer not found there is asked once more over the whole transcript before
the server says "Not in this meeting."."""

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from snapbak_backend.ai.chain import ProviderFailed, Tiers, ask
from snapbak_backend.ai.prompts import ask as prompt
from snapbak_backend.db.models import Meeting, Speaker, TranscriptLine
from snapbak_backend.schemas.cloud import AgendaOut, AskResponse, CitationOut, MinutesOut, TopicOut
from snapbak_backend.services import folding, meetings, recordings
from snapbak_backend.services.embeddings import Embeddings, EmbedError

SHORTEST_WORD = 3
CANDIDATES = 30
# The minutes' sections nearest the question whose lines lead the candidates.
NEAREST = 8
# Each section of the minutes as Ask labels it, so its meaning carries what kind of thing it is ("Open question:
# ..." sits near "what's still undecided?").
LABELS = {
    "actionItems": "Action item",
    "decisions": "Decision",
    "openQuestions": "Open question",
    "risks": "Risk",
    "followUps": "Follow-up",
    "nextAgenda": "Next meeting",
    "agenda": "Agenda",
    "contradictions": "Contradiction",
}
STATUS = {"covered": "covered", "partial": "partly covered", "unresolved": "unresolved"}
# What a question gets when no line supports an answer (MEETINGS.md 5.3), in the server's words, never the model's.
NOT_HERE = AskResponse(answer="Not in this meeting.", citations=[], supported=False)
MAX_TOKENS = 2000
# MOM-06 answers inside 5 s at P95 (2.2 s measured): a slow model is joined by the fallback before then, and the
# question gives up soon after.
# Past the 5 s P95 on purpose: at 5 s a slow call under load was an error, not a slow answer (26 Oct 2026).
DEADLINE_S = 8.0
HEDGE_S = 2.5
# Words that name no topic: "the" would find every "then".
STOPWORDS = frozenset(
    "the and are but can could did does doing done for from had has have how into its just not now our out she "
    "should that their them then there these they this those was were what when where which who whom why will "
    "with would you your about after again also any been being both each few more most other over same some such "
    "than too very all yes okay said say says tell told".split()
)


def tokens(text: str) -> list[str]:
    """The text's words folded as the quote check folds them (`folding.skeleton`), so a line Ask finds for a word
    is one whose quote of it the check accepts."""
    return folding.skeleton(text).split()


def words(question: str) -> list[str]:
    """The question's content words, de-duplicated in order: no stopword, nothing shorter than 3 letters."""
    seen: list[str] = []
    for word in tokens(question):
        if len(word) >= SHORTEST_WORD and word not in STOPWORDS and word not in seen:
            seen.append(word)
    return seen


@dataclass(frozen=True)
class Section:
    """One part of the minutes as Ask reads it: its labelled text and the lines it cites."""

    text: str
    lines: list[UUID]


def sections_of(doc: MinutesOut | None) -> list[Section]:
    """The summary and every item of every section, labelled ("Open question: ...", "Agenda (partly covered):
    ..."), with an owner, a due date or a reason where it has one."""
    if doc is None:
        return []
    made = [Section(f"Summary: {doc.summary}", [])] if doc.summary else []
    for name, label in LABELS.items():
        for item in getattr(doc, name):
            said = item.topic if isinstance(item, TopicOut) else item.text
            tag = f"{label} ({STATUS[item.status]})" if isinstance(item, AgendaOut) else label
            extra = [f"{key} {value}" for key in ("owner", "due", "rationale") if (value := getattr(item, key, None))]
            text = f"{tag}: {said}" + (f" ({'; '.join(extra)})" if extra else "")
            made.append(Section(text, [entry.lineId for entry in item.derivedFrom]))
    return made


def by_meaning(vector: list[float], vectors: list[list[float]], sections: list[Section]) -> list[Section]:
    """The sections nearest the question's vector (cosine: the vectors are unit length), nearest first, NEAREST of
    them."""

    def near(pair: tuple[list[float], Section]) -> float:
        return -sum(a * b for a, b in zip(pair[0], vector, strict=True))

    scored = sorted(zip(vectors, sections, strict=True), key=near)
    return [section for _, section in scored[:NEAREST]]


def by_words(lines: list[TranscriptLine], question: str) -> list[TranscriptLine]:
    """The lines holding the most distinct words of the question as whole words, earlier first on a tie."""
    keys = set(words(question))
    scored = [(len(keys & set(tokens(line.text))), line) for line in lines]
    return [line for hits, line in sorted(scored, key=lambda pair: (-pair[0], pair[1].idx)) if hits]


def candidates(question: str, nearest: list[Section], lines: list[TranscriptLine]) -> list[TranscriptLine]:
    """Up to CANDIDATES lines, in the founder's order: each nearest section's lines in its turn, then the lines that
    name the question's words; with neither, the meeting's first lines."""
    by_id = {line.id: line for line in lines}
    cited_lines = [by_id[line_id] for section in nearest for line_id in section.lines if line_id in by_id]
    named = by_words(lines, question)
    chosen = list({line.id: line for line in [*cited_lines, *named]}.values()) or lines
    return chosen[:CANDIDATES]


@dataclass(frozen=True)
class Asked:
    """What a question needs, read before any model runs so no database connection waits on one: the candidate
    lines, the speakers' names, the minutes' sections as notes, the meeting's every line (for the one retry over
    the whole transcript) and the sections to rank by meaning."""

    lines: list[TranscriptLine]
    speakers: dict[UUID, Speaker]
    notes: list[str]
    everything: list[TranscriptLine] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)


async def gather(session: AsyncSession, meeting: Meeting, question: str) -> Asked:
    """The meeting's live lines, its speakers and its minutes' sections, with candidates by words alone (`focused`
    adds the sections by meaning once the transaction is over)."""
    doc = meetings.minutes_of(meeting)
    lines = await recordings.live_lines(session, meeting.recording_id)
    sections = sections_of(doc)
    speakers = await recordings.speakers_of(session, meeting.recording_id)
    notes = [section.text for section in sections]
    made = Asked(candidates(question, [], lines), speakers, notes, lines, sections)
    return made


# The nearest of a meeting's stored Ask sections to a question's vector, as their places in `sections_of`, nearest
# first; empty when none are stored for the minutes as they stand.
type Stored = Callable[[list[float]], Awaitable[list[int]]]


def ask_chunks(doc: MinutesOut) -> list[tuple[str, int, str]]:
    """Each Ask section as a vector to store (kind `ask`, its place in `sections_of`), written beside the search's
    sections by the embed task, so a question embeds only itself."""
    return [("ask", n, section.text) for n, section in enumerate(sections_of(doc))]


async def focused(embeddings: Embeddings, question: str, asked: Asked, stored: Stored | None = None) -> Asked:
    """The candidates led by the sections nearest the question: the question embedded alone and ranked against the
    stored section vectors; with none stored for these minutes, the sections embedded on the spot. An embedding that
    fails leaves the candidates by words (the full-transcript retry still stands)."""
    if not asked.sections:
        return asked
    try:
        vector = (await embeddings.embed([question]))[0]
        order = await stored(vector) if stored is not None else []
        if order and all(0 <= n < len(asked.sections) for n in order):
            nearest = [asked.sections[n] for n in order[:NEAREST]]
        else:
            vectors = await embeddings.embed([section.text for section in asked.sections])
            nearest = by_meaning(vector, vectors, asked.sections)
    except EmbedError:
        return asked
    lines = candidates(question, nearest, asked.everything)
    notes = list(dict.fromkeys([*(section.text for section in nearest), *asked.notes]))
    made = Asked(lines, asked.speakers, notes, asked.everything, asked.sections)
    return made


async def answer(tiers: Tiers, question: str, asked: Asked) -> AskResponse:
    """One model call over the candidate lines and the minutes' sections (MOM-06's 5 s budget); when no citation of
    it stands, one more over the meeting's whole transcript ("do find out the answer"), never a third. Citations are
    checked against the lines sent by the minutes' quote rule; with none standing the answer is NOT_HERE, so an
    uncited answer is never shown."""
    made = await answered(tiers, question, asked.lines, asked)
    if made.supported or len(asked.everything) <= len(asked.lines):
        return made
    again = await answered(tiers, question, asked.everything, asked)
    return again


async def answered(tiers: Tiers, question: str, lines: list[TranscriptLine], asked: Asked) -> AskResponse:
    """One call over `lines`, its citations checked against them."""
    if not lines:
        return NOT_HERE
    body = [
        {
            "i": line.idx,
            "sname": recordings.speaker_of(asked.speakers.get(line.speaker_id)),
            "atMs": line.start_ms,
            "text": line.text,
        }
        for line in lines
    ]
    deadline = time.monotonic() + DEADLINE_S
    turns = prompt.answer_turns(question, body, asked.notes)
    said = await ask(tiers.chain(tiers.polish), turns, MAX_TOKENS, deadline, [], hedge_s=HEDGE_S)
    try:
        made = json.loads(said)
        text = str(made["answer"])
        citations = made["citations"]
    except (ValueError, KeyError, TypeError) as error:
        raise ProviderFailed(tiers.polish.engine, "bad_answer") from error
    by_index = {line.idx: line for line in lines}
    checked: list[CitationOut] = []
    for citation in citations if isinstance(citations, list) else []:
        index = citation.get("i") if isinstance(citation, dict) else None
        quote = citation.get("quote") if isinstance(citation, dict) else None
        line = by_index.get(index) if isinstance(index, int) else None
        if line is not None and isinstance(quote, str) and folding.quoted(quote, line.text):
            checked.append(CitationOut(lineId=line.id, quote=quote))
    if not checked:
        return NOT_HERE
    return AskResponse(answer=text, citations=checked, supported=True)
