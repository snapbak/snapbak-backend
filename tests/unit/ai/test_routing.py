import pytest

from snapbak_backend.ai import routing
from snapbak_backend.ai.routing import has_cue, tier

LONG = " ".join(["word"] * 151)
LIMIT = " ".join(["word"] * 150)


@pytest.mark.parametrize(
    ("text", "language", "expected"),
    [
        ("let's meet on friday no wait thursday", "en", "big"),
        ("Send this to Jam Co. No, send this to GMMCO.", "en", "big"),
        ("deploy on tuesday actually wednesday", "en", "big"),
        ("the copy should say built for teams scratch that designed for teams", "en", "big"),
        ("call Priya I mean Riya tomorrow", "en", "big"),
        ("book five seats make that six", "en", "big"),
        ("kal milte hain nahi nahi parso milte hain", "en", "big"),
        ("report bhejo matlab invoice bhejo", "en", "big"),
        ("कल मिलते हैं नहीं नहीं परसों मिलते हैं", "en", "big"),
        ("रिपोर्ट भेजो मतलब इनवॉइस भेजो", "en", "big"),
        ("रिपोर्ट भेजो बल्कि डेक भेजो", "en", "big"),
        ("report bhejo balki deck bhejo", "en", "big"),
        ("no no send it to Ravi", "en", "big"),
        ("kal milte hain nahin nahin parso", "en", "big"),
        ("report bhejo nahin, deck bhejo", "en", "big"),
        ("मेल राहुल को भेजो सॉरी रोहित को", "en", "big"),
        ("मेल राहुल को भेजो, आई मीन रोहित को", "en", "big"),
        ("no problem, I'll send it today", "en", "small"),
        ("there is no data in the sheet yet", "en", "small"),
        ("send this to jam co no send this to GMMCO", "en", "small"),
        ("meet friday no thursday", "en", "small"),
        ("ship the build to staging tonight", "en", "small"),
        ("await the build tonight", "en", "small"),
        ("yeh kaam ho gaya", "hinglish", "big"),
        ("यह काम हो गया", "hi", "big"),
        ("yeh kaam ho gaya", "en", "small"),
        ("ship it", "fr", "small"),
        (LONG, "en", "big"),
        (LIMIT, "en", "small"),
    ],
)
def test_tier_escalates_corrections_hindi_and_long_text(text: str, language: str, expected: str) -> None:
    assert tier(text, language) == expected


@pytest.mark.parametrize(
    "text",
    ["no, wait", "No, make it noon", "nahi, kal", "नहीं, कल", "Scratch that.", "NO WAIT"],
)
def test_correction_cues_are_found_in_any_case(text: str) -> None:
    assert has_cue(text)


@pytest.mark.parametrize(
    "text",
    [
        "no problem",
        "nobody waited",
        "I know the plan",
        "नहीं है",
        "nahi hai",
        "the actuality",
        "",
        "await the build",
        "bring the piano, and the drums",
        "casino, then dinner",
    ],
)
def test_ordinary_words_are_not_cues(text: str) -> None:
    assert not has_cue(text)


def test_hindi_rather_is_a_correction_cue() -> None:
    assert has_cue("रविवार को नहीं बल्कि शनिवार को")


@pytest.mark.parametrize(
    ("text", "language", "reasons"),
    [
        ("kal milte hain", "hinglish", True),
        ("कल मिलते हैं", "hi", True),
        ("ship it on monday no wait tuesday", "en", True),
        (" ".join(["word"] * 151), "en", False),
        ("ship it today", "en", False),
    ],
)
def test_reasoning_goes_where_it_measured_a_gain(text: str, language: str, reasons: bool) -> None:
    # Round b3 measured medium reasoning lifting Hindi and Hinglish rows (150 against 131 of 216); founder 13 Oct:
    # an English chunk that is big only for its length answers without it, which is faster.
    assert routing.reasons(text, language) is reasons


def test_a_long_chunk_splits_where_a_paragraph_is_said_between_long_paragraphs() -> None:
    # Founder, 13 Oct: each paragraph is polished at once, in parallel, so a long dictation takes its slowest part.
    first = " ".join(["first"] * 30)
    second = " ".join(["second"] * 30)
    assert routing.paragraphs(f"{first} new paragraph {second}") == [first, second]
    assert routing.paragraphs(f"{first} naya paragraph {second}") == [first, second]
    assert routing.paragraphs(f"{first} नया पैराग्राफ {second}") == [first, second]
    # A short side, or a paragraph named rather than asked for at the edge, stays one chunk.
    short = f"{first} new paragraph two words"
    assert routing.paragraphs(short) == [short]
    assert routing.paragraphs("ship it") == ["ship it"]
