import json

import pytest

from snapbak_backend.ai.providers import base
from snapbak_backend.ai.providers.base import ProviderError, Usage
from snapbak_backend.ai.providers.factory import make_provider
from snapbak_backend.ai.providers.fake import Fake

KEYS = {"groq": "gsk", "openai": "sk", "anthropic": "sk-ant", "gemini": "gm"}


@pytest.mark.parametrize(
    "spec",
    [
        "groq:llama-3.3-70b-versatile",
        "openai:gpt-4.1-nano",
        "anthropic:claude-haiku-4-5",
        "gemini:gemini-2.5-flash-lite",
        "fake:echo",
        "fake:edit",
        "fake:fail",
        "fake:slow",
        "fake:reply",
        "groq:openai/gpt-oss-20b",
    ],
)
def test_each_registered_provider_builds_with_its_engine_name(spec: str) -> None:
    assert make_provider(spec, KEYS).engine == spec


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("mistral:large", "Unknown provider 'mistral'"),
        ("openai", "provider:model"),
        ("openai:", "provider:model"),
        ("fake:chaos", "Unknown fake mode 'chaos'"),
    ],
)
def test_a_bad_spec_is_refused(spec: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        make_provider(spec, KEYS)


def test_a_provider_without_its_key_is_refused() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        make_provider("openai:gpt-4.1-nano", {**KEYS, "openai": ""})


def polish_turns(text: str, prior: str = "") -> list[tuple[str, str]]:
    request = {"text": text, "prior": prior, "terms": [], "app": "", "style": "chat", "language": "en"}
    return [("system", "rules"), ("user", json.dumps(request))]


def command_turns(selection: str) -> list[tuple[str, str]]:
    request = {"selection": selection, "instruction": "shout", "app": "", "language": "en"}
    return [("system", "rules"), ("user", json.dumps(request))]


async def answer(mode: str, turns: list[tuple[str, str]]) -> dict[str, object]:
    output, usage = await Fake(mode).complete(turns, 100, 1.0)
    assert usage == Usage(0, 0)
    return json.loads(output)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ship it.", "ship it!"),
        ("ship it", "ship it!"),
        ("really?", "really!"),
        ("done!", "done!"),
        ("kal milte hain", "kal milte hain!"),
        ("कल मिलते हैं।", "कल मिलते हैं।!"),
        ("", ""),
    ],
)
async def test_fake_edit_ends_the_text_with_a_bang(text: str, expected: str) -> None:
    assert await answer("edit", polish_turns(text)) == {"revision": None, "text": expected}


@pytest.mark.anyio
async def test_fake_echo_returns_the_text_unchanged() -> None:
    assert await answer("echo", polish_turns("ship it")) == {"revision": None, "text": "ship it"}


@pytest.mark.anyio
async def test_fake_command_shouts_the_selection_in_edit_mode_and_echoes_it_otherwise() -> None:
    assert await answer("edit", command_turns("send the deck")) == {"text": "SEND THE DECK"}
    assert await answer("echo", command_turns("send the deck")) == {"text": "send the deck"}


@pytest.mark.anyio
async def test_fake_fail_raises_a_provider_error() -> None:
    with pytest.raises(ProviderError):
        await Fake("fail").complete(polish_turns("ship it"), 100, 1.0)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Ship it today! no wait friday", "Ship it friday!"),
        ("We ship it today. No, wait, Friday.", "We ship it Friday."),
        ("We ship it today! No, wait! Friday", "We ship it Friday!"),
        ("मीटिंग सोमवार। no wait मंगलवार", "मीटिंग मंगलवार।"),
        ("no wait friday", "no wait friday!"),
        ("Ship it today! now wait for it", "Ship it today! now wait for it!"),
        ("Ship it today! no wait make it friday", "Ship it today! no wait make it friday!"),
    ],
)
async def test_fake_edit_swaps_the_last_word_of_the_sentence_before_no_wait(text: str, expected: str) -> None:
    assert await answer("edit", polish_turns(text)) == {"revision": None, "text": expected}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Kal subah meeting rakhte hain. nahi nahi parso", "Parso subah meeting rakhte hain."),
        ("Kal subah meeting rakhte hain. Nahi nahi, parso.", "Parso subah meeting rakhte hain."),
        ("KAL SUBAH MEETING RAKHTE HAIN! Nahin nahin, parso!", "Parso SUBAH MEETING RAKHTE HAIN!"),
        ("कल सुबह मीटिंग रखते हैं। नहीं नहीं, परसों।", "परसों सुबह मीटिंग रखते हैं।"),
        ("Subah meeting rakhte hain. nahi nahi parso", "Subah meeting rakhte hain. nahi nahi parso!"),
        ("nahi nahi parso", "nahi nahi parso!"),
    ],
)
async def test_fake_edit_puts_the_word_after_a_doubled_nahi_in_place_of_the_first_day_word(
    text: str, expected: str
) -> None:
    assert await answer("edit", polish_turns(text)) == {"revision": None, "text": expected}


@pytest.mark.anyio
async def test_fake_reply_answers_the_user_so_the_guardrail_rejects_it() -> None:
    assert await answer("reply", polish_turns("ship it")) == {"revision": None, "text": "Sure, ship it"}


def test_providers_keep_idle_connections_for_five_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[dict] = []
    monkeypatch.setattr(base.httpx, "AsyncClient", lambda **options: built.append(options))
    make_provider("openai:gpt-4.1-nano", KEYS)
    assert built[0]["limits"].keepalive_expiry == 300
    assert built[0]["http2"] is True
