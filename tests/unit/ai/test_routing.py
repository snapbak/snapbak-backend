import json
from importlib import resources

import pytest

from snapbak_backend.ai import routing
from snapbak_backend.ai.routing import has_cue


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("let's meet on friday no wait thursday", True),
        ("Send this to Jam Co. No, send this to GMMCO.", True),
        ("deploy on tuesday actually wednesday", True),
        ("the copy should say built for teams scratch that designed for teams", True),
        ("call Priya I mean Riya tomorrow", True),
        ("book five seats make that six", True),
        ("no no send it to Ravi", True),
        ("no problem, I'll send it today", False),
        ("there is no data in the sheet yet", False),
        ("send this to jam co no send this to GMMCO", False),
        ("meet friday no thursday", False),
        ("ship the build to staging tonight", False),
        ("await the build tonight", False),
    ],
)
def test_a_correction_cue_is_found_and_a_plain_no_is_not(text: str, expected: bool) -> None:
    # The cue folds the prior's last sentence into the chunk (transformers/polish.py); since 15 Oct it no longer
    # picks a model.
    assert has_cue(text) is expected


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


def test_a_long_chunk_splits_where_a_paragraph_is_said_between_long_paragraphs() -> None:
    # Founder, 13 Oct: each paragraph is polished at once, in parallel, so a long dictation takes its slowest part.
    first = " ".join(["first"] * 30)
    second = " ".join(["second"] * 30)
    assert routing.paragraphs(f"{first} new paragraph {second}") == [first, second]
    # A short side, or a paragraph named rather than asked for at the edge, stays one chunk.
    short = f"{first} new paragraph two words"
    assert routing.paragraphs(short) == [short]
    assert routing.paragraphs("ship it") == ["ship it"]


# The cues people say to take words back, in natural English (14 Oct 2026).
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
def test_natural_correction_cues_are_found(text: str) -> None:
    assert has_cue(text)


def test_the_cues_are_read_from_the_shared_cue_list_shipped_with_the_package() -> None:
    # One list for the app and the server (15 Oct): the app embeds a byte-identical copy of this file.
    shipped = resources.files("snapbak_backend.ai").joinpath("cues.json")
    cues = json.loads(shipped.read_text(encoding="utf-8"))
    assert cues == routing.CUES
    assert len(cues) == 26
    assert {"no,", "scratch that", "i mean", "no don't do that"} <= set(cues)
