"""Lists written the way the app the user types in renders them (founder, 12 Oct: native per app)."""

import pytest

from snapbak_backend.ai.guardrail.shape import in_style, plain_bullets

LIST = "Things for today:\n- Call Rahul\n- Send the deck"
COUNTED = "Do three things:\n1. Call Ishan\n2. Send the deck\n3. Book the room"


def test_a_chat_app_gets_round_bullets() -> None:
    assert in_style(LIST, "chat") == "Things for today:\n• Call Rahul\n• Send the deck"


def test_a_chat_app_keeps_a_numbered_list_as_it_is() -> None:
    assert in_style(COUNTED, "chat") == COUNTED


@pytest.mark.parametrize("style", ["email", "docs", "other"])
def test_email_docs_and_other_apps_keep_dash_bullets_and_numbers(style: str) -> None:
    assert in_style(LIST, style) == LIST
    assert in_style(COUNTED, style) == COUNTED


def test_a_code_app_gets_each_item_as_a_sentence_after_its_lead() -> None:
    assert in_style(LIST, "code") == "Things for today: Call Rahul. Send the deck."
    assert in_style(COUNTED, "code") == "Do three things: Call Ishan. Send the deck. Book the room."


def test_a_code_app_keeps_an_items_own_end_mark_and_hindi_stops() -> None:
    hindi = "आज के काम:\n- राहुल को कॉल करो\n- डेक भेजो?"
    assert in_style(hindi, "code") == "आज के काम: राहुल को कॉल करो। डेक भेजो?"


def test_a_code_app_keeps_text_before_and_after_a_list() -> None:
    text = "Hi team.\n\nNotes:\n- Ship it\n- Tag the release\n\nThanks."
    assert in_style(text, "code") == "Hi team.\n\nNotes: Ship it. Tag the release.\n\nThanks."


def test_a_list_with_no_lead_in_a_code_app_is_its_sentences() -> None:
    assert in_style("- Ship it\n- Tag the release", "code") == "Ship it. Tag the release."


def test_text_without_a_list_is_left_alone_in_every_style() -> None:
    text = "Ship it today - the tag can wait. Then call me."
    for style in ["chat", "email", "code", "docs", "other"]:
        assert in_style(text, style) == text


def test_round_bullets_the_app_typed_before_are_read_as_dash_bullets() -> None:
    assert plain_bullets("Things for today:\n• Call Rahul\n• Send the deck") == LIST
    assert plain_bullets("Ship it • now") == "Ship it • now"
