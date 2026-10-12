import pytest

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
