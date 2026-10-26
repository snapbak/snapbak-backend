"""Ask's retrieval (MOM-06, the founder's order of 26 Oct 2026): the minutes' sections by meaning first, each with
the lines it cites, then the transcript by its words, and once more over the whole transcript before an answer is
"Not in this meeting.". The founder's "What's still undecided?" got the meeting's first 20 lines while the open
question cited lines 78 and 79, so no citation could stand."""

import asyncio
import json
from typing import Any
from uuid import uuid4

from snapbak_backend.ai.chain import Tiers
from snapbak_backend.ai.providers.base import Usage
from snapbak_backend.db.models import Speaker, TranscriptLine
from snapbak_backend.schemas.cloud import AgendaOut, ClaimOut, DerivationOut, MinutesOut
from snapbak_backend.services import qa

WHO = Speaker(id=uuid4(), recording_id=uuid4(), label="Speaker 1", vendor_label="A", name="Saksham")


def line(idx: int, text: str) -> TranscriptLine:
    return TranscriptLine(
        id=uuid4(),
        recording_id=WHO.recording_id,
        speaker_id=WHO.id,
        idx=idx,
        start_ms=idx * 3_000,
        end_ms=idx * 3_000 + 2_500,
        text=text,
        words=[],
    )


# A 90-line meeting: chatter, then the one unsettled question at lines 78 and 79, as in the founder's.
LINES = [line(n, f"We talked about item number {n} at some length.") for n in range(90)]
LINES[78] = line(78, "Which venue do we book for the launch?")
LINES[79] = line(79, "Nobody could say yet, we will see.")
LINES[40] = line(40, "The budget is fifty thousand rupees.")


def cite(idx: int) -> DerivationOut:
    return DerivationOut(lineId=LINES[idx].id, speaker="Saksham", atMs=LINES[idx].start_ms, quote=LINES[idx].text)


DOC = MinutesOut.model_validate(
    {
        "summary": "The team planned the launch.",
        "mom": "### Launch\n\nThey planned it.",
        "actionItems": [],
        "decisions": [],
        "openQuestions": [ClaimOut(id="q_1", text="Which venue to book", derivedFrom=[cite(78), cite(79)])],
        "agenda": [AgendaOut(id="ag_1", topic="Budget", status="partial", derivedFrom=[cite(40)])],
    }
)


def test_mom06_every_item_of_the_minutes_is_a_section_with_its_label_and_lines() -> None:
    sections = qa.sections_of(DOC)
    texts = [section.text for section in sections]
    assert "Open question: Which venue to book" in texts
    assert "Agenda (partly covered): Budget" in texts
    venue = next(section for section in sections if section.text.startswith("Open question"))
    assert venue.lines == [LINES[78].id, LINES[79].id]


def test_mom06_candidates_lead_with_the_sections_nearest_the_question_and_their_lines() -> None:
    """Chunk first, in cosine order, each with the lines it cites; keyword lines after them."""
    sections = qa.sections_of(DOC)
    # The question points at the open question, then the agenda; the summary least.
    nearest = {"Open question": [1.0, 0.0], "Agenda": [0.6, 0.8], "Summary": [0.0, 1.0]}
    vectors = [next(v for k, v in nearest.items() if section.text.startswith(k)) for section in sections]
    picked = qa.by_meaning([1.0, 0.0], vectors, sections)
    assert [section.text.split(":")[0] for section in picked][:2] == ["Open question", "Agenda (partly covered)"]
    chosen = qa.candidates("what about the budget", picked, LINES)
    assert [item.idx for item in chosen][:3] == [78, 79, 40]


def test_mom06_keyword_lines_come_after_the_sections_lines() -> None:
    sections = qa.sections_of(DOC)
    venue = [section for section in sections if section.text.startswith("Open question")]
    chosen = qa.candidates("how much is the budget in rupees", venue, LINES)
    assert [item.idx for item in chosen][:3] == [78, 79, 40]


def test_mom06_a_roman_question_with_no_word_in_the_lines_still_reaches_the_sections() -> None:
    """Meaning, not words: "abhi kya decide nahi hua" names no word of the lines, yet gets the open question's."""
    sections = qa.sections_of(DOC)
    vectors = [[1.0, 0.0] if section.text.startswith("Open question") else [0.0, 1.0] for section in sections]
    picked = qa.by_meaning([0.9, 0.1], vectors, sections)
    chosen = qa.candidates("abhi kya decide nahi hua", picked, LINES)
    assert {78, 79} <= {item.idx for item in chosen[:2]}


class Scripted:
    """Answers each Ask call from a script and keeps how many lines each call carried."""

    def __init__(self, *answers: dict[str, Any]) -> None:
        self.engine = "fake:scripted"
        self.answers = [json.dumps(answer) for answer in answers]
        self.sent: list[int] = []

    async def complete(self, turns: list[tuple[str, str]], max_tokens: int, timeout: float, reason: str = "") -> Any:
        self.sent.append(len(json.loads(turns[-1][1])["lines"]))
        return self.answers.pop(0), Usage(0, 0)

    async def aclose(self) -> None:
        pass


NOTHING = {"answer": "Not in this meeting.", "citations": []}
VENUE = {"answer": "The venue is still open.", "citations": [{"i": 78, "quote": "Which venue do we book"}]}


def asked_of(lines: list[TranscriptLine]) -> qa.Asked:
    return qa.Asked(lines=lines, speakers={WHO.id: WHO}, notes=[], everything=LINES)


def test_mom06_an_answer_not_found_in_the_candidates_is_asked_once_more_over_the_whole_transcript() -> None:
    provider = Scripted(NOTHING, VENUE)
    tiers = Tiers(polish=provider, command=provider, fallback=None)  # type: ignore[arg-type]
    made = asyncio.run(qa.answer(tiers, "what's still undecided?", asked_of(LINES[:20])))
    assert made.supported and made.citations[0].lineId == LINES[78].id
    assert provider.sent == [20, 90]


def test_mom06_a_supported_first_answer_is_never_asked_again() -> None:
    provider = Scripted(VENUE)
    tiers = Tiers(polish=provider, command=provider, fallback=None)  # type: ignore[arg-type]
    made = asyncio.run(qa.answer(tiers, "what's still undecided?", asked_of(LINES[70:])))
    assert made.supported and provider.sent == [20]


def test_mom06_the_whole_transcript_is_asked_at_most_once_and_no_citation_is_still_not_here() -> None:
    made_up = {"answer": "The venue is Goa.", "citations": [{"i": 78, "quote": "We book Goa"}]}
    provider = Scripted(NOTHING, made_up)
    tiers = Tiers(polish=provider, command=provider, fallback=None)  # type: ignore[arg-type]
    made = asyncio.run(qa.answer(tiers, "what's still undecided?", asked_of(LINES[:20])))
    assert made == qa.NOT_HERE and provider.sent == [20, 90]


def test_mom06_the_prompt_answers_questions_about_the_meeting_as_a_whole_from_the_minutes_with_their_lines() -> None:
    """ "What's still undecided?", "who owns what?", "summarize the meeting": answered from the minutes' items, cited
    by the lines behind them; "Not in this meeting." only when neither touches the question."""
    from snapbak_backend.ai.prompts import ask as prompt

    system = prompt.ANSWER_SYSTEM
    assert "about the meeting as a whole" in system and "the lines behind the items you use" in system
    assert "in any language" in system and "1 to 6 citations" in system


class Counting:
    """An embedder that keeps how many texts each call carried; every text points the same way."""

    engine = "fake:counting"

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        return [[1.0, 0.0] for _ in texts]

    async def aclose(self) -> None:
        pass


def test_mom06_with_the_sections_vectors_stored_a_question_embeds_only_itself() -> None:
    """Embedding the minutes' ~60 sections for every question took 2.5 s of the 5 s (26 Oct 2026): the embed task
    stores them once, and a question embeds only itself and ranks the stored ones."""
    sections = qa.sections_of(DOC)
    venue = next(n for n, section in enumerate(sections) if section.text.startswith("Open question"))
    embeddings = Counting()

    async def stored(vector: list[float]) -> list[int]:
        return [venue]

    base = qa.Asked(LINES[:20], {WHO.id: WHO}, [], LINES, sections)
    made = asyncio.run(qa.focused(embeddings, "what's still undecided?", base, stored))  # type: ignore[arg-type]
    assert embeddings.calls == [1]
    assert [line.idx for line in made.lines][:2] == [78, 79]


def test_mom06_without_stored_vectors_the_sections_are_embedded_with_the_question() -> None:
    """Minutes edited since their vectors were written (or never embedded): the sections are embedded on the spot."""
    sections = qa.sections_of(DOC)
    embeddings = Counting()

    async def stored(vector: list[float]) -> list[int]:
        return []

    base = qa.Asked(LINES[:20], {WHO.id: WHO}, [], LINES, sections)
    asyncio.run(qa.focused(embeddings, "what's still undecided?", base, stored))  # type: ignore[arg-type]
    assert embeddings.calls == [1, len(sections)]


def test_mom06_the_minutes_vectors_include_one_ask_section_per_section_of_the_minutes() -> None:
    chunks = qa.ask_chunks(DOC)
    assert [(kind, idx) for kind, idx, _ in chunks] == [("ask", n) for n in range(len(qa.sections_of(DOC)))]
    assert chunks[0][2] == qa.sections_of(DOC)[0].text
