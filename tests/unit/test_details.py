"""The no-dropped-details guard (services/details.py, minutes v2): the transcript's salient details, and whether
the minutes kept them."""

from uuid import uuid4

from snapbak_backend.db.models import Speaker, TranscriptLine
from snapbak_backend.schemas.cloud import ActionItemOut, DerivationOut, MinutesOut
from snapbak_backend.services import details


def made(text: str, idx: int, who: Speaker) -> TranscriptLine:
    line = TranscriptLine(
        id=uuid4(),
        recording_id=who.recording_id,
        speaker_id=who.id,
        idx=idx,
        start_ms=idx * 1000,
        end_ms=idx * 1000 + 900,
        text=text,
        words=[],
    )
    return line


PRIYA = Speaker(id=uuid4(), recording_id=uuid4(), label="Speaker 1", vendor_label="A", name="Priya")
LINES = [
    made("We fly out Friday at 11 p.m., and the budget is 5,000 rupees.", 0, PRIYA),
    made("Let's meet Harini at Lajpat Nagar first.", 1, PRIYA),
    made("Main kal tak list bhej dunga, 2 baje se pehle.", 2, PRIYA),
]


def kinds(units: list[details.Unit]) -> set[tuple[str, str]]:
    return {(unit.kind, unit.text) for unit in units}


def test_mom05_the_inventory_finds_numbers_times_names_and_commitments_in_english_and_hinglish() -> None:
    found = kinds(details.inventory(LINES, {PRIYA.id: PRIYA}))
    assert {("number", "11"), ("number", "5 000"), ("number", "2")} <= found
    assert {("time", "friday"), ("time", "kal")} <= found
    assert {("name", "harini"), ("name", "lajpat"), ("name", "nagar"), ("name", "priya")} <= found
    assert {("commitment", "let s"), ("commitment", "dunga")} <= found
    # The first word of a sentence is no name.
    assert ("name", "we") not in found and ("name", "main") not in found


def minutes(summary: str, cited: list[int]) -> MinutesOut:
    action = ActionItemOut(
        id="act_1",
        text="Send the list",
        derivedFrom=[
            DerivationOut(lineId=LINES[n].id, speaker="Priya", atMs=LINES[n].start_ms, quote="x") for n in cited
        ],
    )
    return MinutesOut(
        summary=summary, mom="### Trip\n\nThe plan.", actionItems=[action], openQuestions=[], decisions=[]
    )


def test_mom05_minutes_that_drop_a_number_or_a_name_miss_it() -> None:
    units = details.inventory(LINES, {PRIYA.id: PRIYA})
    full = minutes(
        "Priya: fly Friday 11 p.m., budget 5,000 rupees; meet Harini at Lajpat Nagar; list kal by 2.", [1, 2]
    )
    assert details.missing(full, units, LINES) == []
    dropped = minutes("Priya: fly Friday 11 p.m.; meet Harini at Lajpat Nagar; list kal by 2.", [1, 2])
    assert [(unit.kind, unit.text) for unit in details.missing(dropped, units, LINES)] == [("number", "5 000")]
    assert details.recall(dropped, units, LINES) < 1.0


def test_mom05_a_commitment_counts_when_an_action_item_or_follow_up_cites_its_line() -> None:
    units = details.inventory(LINES, {PRIYA.id: PRIYA})
    summary = "Priya: fly Friday 11 p.m., budget 5,000 rupees; meet Harini at Lajpat Nagar; list kal by 2."
    uncited = details.missing(minutes(summary, [0]), units, LINES)
    assert sorted((unit.kind, unit.text) for unit in uncited) == [("commitment", "dunga"), ("commitment", "let s")]


def test_mom05_every_agenda_topic_needs_its_own_section_in_the_minutes() -> None:
    from snapbak_backend.schemas.cloud import AgendaOut

    doc = minutes("x", []).model_copy(
        update={
            "agenda": [
                AgendaOut(id="ag_1", topic="Trip", status="covered", derivedFrom=[]),
                AgendaOut(id="ag_2", topic="Budget", status="partial", derivedFrom=[]),
            ]
        }
    )
    assert details.topics_missing(doc) == ["Budget"]


def test_mom05_a_commitment_the_completion_judged_no_commitment_counts_as_covered() -> None:
    """Every commitment ends as an action item or an explicit, stored judgment that it is none (26 Oct 2026)."""
    from snapbak_backend.schemas.cloud import NotCommitmentOut

    doc = MinutesOut(summary="s", mom="m", actionItems=[], openQuestions=[], decisions=[])
    units = [unit for unit in details.inventory(LINES, {PRIYA.id: PRIYA}) if unit.kind == "commitment"]
    assert [unit.idx for unit in details.missing(doc, units, LINES)] == [1, 2]
    judged = doc.model_copy(update={"notCommitments": [NotCommitmentOut(lineId=LINES[1].id, reason="a proposal")]})
    assert [unit.idx for unit in details.missing(judged, units, LINES)] == [2]


def test_mom05_each_line_s_commitment_is_its_own_unit() -> None:
    """Every "I'll" is a commitment of its own: deduplicating by the phrase counted only a meeting's first."""
    lines = [made("I'll send the deck.", 0, PRIYA), made("I'll book the room.", 1, PRIYA)]
    found = [unit.idx for unit in details.inventory(lines, {PRIYA.id: PRIYA}) if unit.kind == "commitment"]
    assert found == [0, 1]


def test_mom05_negated_rhetorical_and_conditional_phrases_make_no_commitment() -> None:
    """From a sample of AMI's uncovered units (26 Oct 2026): 14 of 15 were no commitment."""
    said = [
        "You don't need to send very much information.",
        "You don't have to pay a lot of attention on those.",
        "So let's see.",
        "I'll make, let's say, fifteen buttons.",
        "Our budget would have to reflect it.",
        "We do not need to decide that.",
        "We'd have to have some room for future channels.",
    ]
    lines = [made(text, n, PRIYA) for n, text in enumerate(said)]
    found = [unit for unit in details.inventory(lines, {PRIYA.id: PRIYA}) if unit.kind == "commitment"]
    assert [(unit.idx, unit.text) for unit in found] == [(3, "i ll")]


def test_mom05_a_real_commitment_beside_a_rhetorical_one_still_counts() -> None:
    lines = [made("Let's see, I'll send it by Friday.", 0, PRIYA), made("We need to book the hall.", 1, PRIYA)]
    found = [unit.idx for unit in details.inventory(lines, {PRIYA.id: PRIYA}) if unit.kind == "commitment"]
    assert found == [0, 1]
