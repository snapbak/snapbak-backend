import pytest

from snapbak_backend.ai import routing
from snapbak_backend.ai.routing import has_cue, tier

LONG = " ".join(["word"] * 151)
LIMIT = " ".join(["word"] * 150)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("let's meet on friday no wait thursday", "big"),
        ("Send this to Jam Co. No, send this to GMMCO.", "big"),
        ("deploy on tuesday actually wednesday", "big"),
        ("the copy should say built for teams scratch that designed for teams", "big"),
        ("call Priya I mean Riya tomorrow", "big"),
        ("book five seats make that six", "big"),
        ("no no send it to Ravi", "big"),
        ("no problem, I'll send it today", "small"),
        ("there is no data in the sheet yet", "small"),
        ("send this to jam co no send this to GMMCO", "small"),
        ("meet friday no thursday", "small"),
        ("ship the build to staging tonight", "small"),
        ("await the build tonight", "small"),
        (LONG, "big"),
        (LIMIT, "small"),
    ],
)
def test_tier_escalates_corrections_and_long_text(text: str, expected: str) -> None:
    assert tier(text) == expected


@pytest.mark.parametrize(
    "text",
    ["no, wait", "No, make it noon", "Scratch that.", "NO WAIT"],
)
def test_correction_cues_are_found_in_any_case(text: str) -> None:
    assert has_cue(text)


@pytest.mark.parametrize(
    "text",
    [
        "no problem",
        "nobody waited",
        "I know the plan",
        "the actuality",
        "",
        "await the build",
        "bring the piano, and the drums",
        "casino, then dinner",
    ],
)
def test_ordinary_words_are_not_cues(text: str) -> None:
    assert not has_cue(text)


@pytest.mark.parametrize(
    ("text", "effort"),
    [
        ("ship it on monday no wait tuesday", "low"),
        ("ship it today", "minimal"),
        (" ".join(["word"] * 151), "minimal"),
    ],
)
def test_a_chunk_reasons_at_its_route_effort(monkeypatch: pytest.MonkeyPatch, text: str, effort: str) -> None:
    # A chunk with a correction cue takes the cue route's effort, any other chunk the plain route's, on either tier.
    monkeypatch.setitem(routing.EFFORTS, "cue", "low")
    monkeypatch.setitem(routing.EFFORTS, "plain", "minimal")
    assert routing.effort(text) == effort


def test_every_route_reasons_at_medium() -> None:
    # The 14 Oct 2026 English sweep: none and low fell outside medium's noise band (docs/escalation.md).
    assert routing.EFFORTS == {"cue": "medium", "plain": "medium", "correction": "medium"}


def test_a_long_chunk_splits_where_a_paragraph_is_said_between_long_paragraphs() -> None:
    # Founder, 13 Oct: each paragraph is polished at once, in parallel, so a long dictation takes its slowest part.
    first = " ".join(["first"] * 30)
    second = " ".join(["second"] * 30)
    assert routing.paragraphs(f"{first} new paragraph {second}") == [first, second]
    # A short side, or a paragraph named rather than asked for at the edge, stays one chunk.
    short = f"{first} new paragraph two words"
    assert routing.paragraphs(short) == [short]
    assert routing.paragraphs("ship it") == ["ship it"]


# The cues people say to take words back, in natural English (14 Oct 2026): each sends the chunk to the big tier.
@pytest.mark.parametrize(
    "text",
    [
        "Send it to Sam. No, don't do this. Send it to Priya.",
        "send it to sam no don't do that send it to priya",
        "Book the room for Monday, scratch that, Tuesday.",
        "Book the room for Monday, actually Tuesday.",
        "Book the room for Monday, I mean Tuesday.",
        "Book the room for Monday. Wait, no, Tuesday.",
        "Send the report to Sam. Delete that. Send the deck to Priya.",
        "Book five seats, make that six.",
        "Book the room for Monday, sorry, Tuesday.",
    ],
)
def test_natural_correction_cues_go_to_the_big_tier(text: str) -> None:
    assert tier(text) == "big"
