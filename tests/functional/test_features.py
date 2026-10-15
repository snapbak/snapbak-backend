"""Each graded feature against its spec through /v1/polish, with the models' answers scripted per model."""

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from snapbak_backend import main as app_module

SMALL = "gpt-4.1-nano"
BIG = "gpt-4.1"
AUTH = {"Authorization": "Bearer dev"}


def polish(
    monkeypatch: pytest.MonkeyPatch,
    replies: dict[str, dict],
    text: str,
    prior: str = "",
    terms: tuple[str, ...] = (),
    continues: bool = False,
) -> tuple[dict, list[dict]]:
    """The response, and every request body the models got."""
    env = {
        "SNAPBAK_TOKEN": "dev",
        "SNAPBAK_POLISH": f"openai:{SMALL}",
        "SNAPBAK_BIG": f"openai:{BIG}",
        "SNAPBAK_FALLBACK": "",
        "OPENAI_API_KEY": "sk-test",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    asked: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        asked.append(body)
        content = json.dumps(replies[body["model"]], ensure_ascii=False)
        usage = {"prompt_tokens": 1000, "completion_tokens": 20}
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": usage})

    request = {
        "text": text,
        "prior": prior,
        "terms": list(terms),
        "app": "Slack",
        "style": "chat",
        "continues": continues,
        "deadline_ms": 2000,
    }
    with respx.mock() as mock, TestClient(app_module.app) as client:
        mock.post("https://api.openai.com/v1/chat/completions").mock(side_effect=answer)
        response = client.post("/v1/polish", json=request, headers=AUTH)
    assert response.status_code == 200
    return response.json(), asked


def said(text: str, revision: str | None = None) -> dict:
    return {"revision": revision, "text": text}


def models(asked: list[dict]) -> list[str]:
    return [body["model"] for body in asked]


BACKTRACKING = [
    ("no wait make it thursday", "Let's meet on Friday at noon.", "Let's meet on Thursday at noon.", ""),
    ("deploy on tuesday actually wednesday", "", "Deploy on Wednesday.", None),
    ("Send this to Jam Co. No, send this to GMMCO.", "", "Send this to GMMCO.", None),
    (
        "We should send this to the frontend team. No, don't send it yet.",
        "",
        "Don't send it yet.",
        None,
    ),
    ("No, don't send it yet.", "We should send this to the frontend team.", "Don't send it yet.", ""),
    ("book the flight to delhi scratch that call priya", "", "Call Priya.", None),
    ("send it to sam i mean ravi", "", "Send it to Ravi.", None),
    ("book five seats make that six", "", "Book 6 seats.", None),
    ("scratch that", "Ship it today team. Also loop in Priya on the budget doc.", "", "Ship it today team."),
    ("don't send it, no wait, send it", "", "Send it.", None),
    ("no wait send it", "Don't send it.", "", "Send it."),
]


@pytest.mark.parametrize(("text", "prior", "output", "revision"), BACKTRACKING)
def test_backtracking_drops_the_retracted_words_on_the_one_polish_model(
    monkeypatch: pytest.MonkeyPatch, text: str, prior: str, output: str, revision: str | None
) -> None:
    # 15 Oct 2026: one model polishes every chunk; a cue no longer sends it to the big model.
    body, asked = polish(monkeypatch, {SMALL: said(output, revision)}, text, prior=prior)
    assert (body["text"], body["revision"], body["rejected"], body["tier"]) == (output, revision, False, "small")
    assert models(asked) == [SMALL]


@pytest.mark.parametrize(
    ("text", "prior", "output", "revision"),
    [
        (
            "no wait make it thursday",
            "We need a list. Let's meet on Friday at noon.",
            "Let's meet on Thursday at noon.",
            "We need a list.",
        ),
        ("no wait make it thursday", "Let's meet on Friday at noon.", "Let's meet on Thursday at noon.", ""),
    ],
)
def test_a_correction_is_asked_with_the_last_sentence_and_answered_as_text_and_revision(
    monkeypatch: pytest.MonkeyPatch, text: str, prior: str, output: str, revision: str
) -> None:
    body, asked = polish(monkeypatch, {SMALL: said(output)}, text, prior=prior)
    last_turn = json.loads(asked[0]["messages"][-1]["content"])
    assert (last_turn.get("prior", ""), last_turn["text"].removesuffix(f" {text}")) == (
        revision,
        prior.removeprefix(revision).strip(),
    )
    assert (body["text"], body["revision"], body["rejected"]) == (output, revision, False)


@pytest.mark.parametrize(
    ("text", "output"),
    [
        ("no problem i'll send it tonight", "No problem, I'll send it tonight."),
        ("there is no data in the sheet yet", "There is no data in the sheet yet."),
    ],
)
def test_an_ordinary_no_stays_and_stays_on_the_small_model(
    monkeypatch: pytest.MonkeyPatch, text: str, output: str
) -> None:
    body, asked = polish(monkeypatch, {SMALL: said(output)}, text)
    assert (body["text"], body["rejected"], body["tier"]) == (output, False, "small")
    assert models(asked) == [SMALL]


@pytest.mark.parametrize(
    ("text", "output"),
    [
        ("it actually works now", "It actually works now."),
    ],
)
def test_an_ordinary_cue_word_stays(monkeypatch: pytest.MonkeyPatch, text: str, output: str) -> None:
    body, asked = polish(monkeypatch, {SMALL: said(output)}, text)
    assert (body["text"], body["rejected"], body["tier"]) == (output, False, "small")
    assert models(asked) == [SMALL]


@pytest.mark.parametrize(
    ("text", "prior", "output", "revision"),
    [
        ("deploy on tuesday then wednesday", "", "Deploy on Wednesday.", None),
        ("no wait thursday", "Let's meet on Friday at noon.", "", "Let's meet on Thursday for lunch."),
        ("No, don't send it yet.", "We should send this to the frontend team.", "", ""),
        ("no, don't send it yet", "", "No.", None),
        ("actually", "Hi team, the launch moved to Monday and legal signed off.", "", ""),
        ("ravi said sorry about the delay", "", "About the delay.", None),
        ("tell the vendor no pickup after six pm", "", "Tell the pickup after 6 pm.", None),
        ("send it no wait send it", "", "Don't send it.", None),
        ("i have 2 no kids", "", "I have 2 kids.", None),
    ],
)
def test_a_correction_that_drops_or_invents_words_is_rejected(
    monkeypatch: pytest.MonkeyPatch, text: str, prior: str, output: str, revision: str | None
) -> None:
    replies = {SMALL: said(output, revision), BIG: said(output, revision)}
    body, _ = polish(monkeypatch, replies, text, prior=prior)
    assert (body["text"], body["revision"], body["rejected"]) == (text, None, True)


FORMATTING = [
    (
        "we need three things first the api second the ui third the docs",
        "We need three things:\n1. The API\n2. The UI\n3. The docs",
    ),
    ("for the trip bullet towels bullet a charger", "For the trip:\n- Towels\n- A charger"),
    ("it costs twenty five dollars mail john at procol dot in", "It costs $25. Mail john@procol.in."),
    ("we ship on march third at nine am", "We ship on March 3 at 9 am."),
    ("hey team comma ship it full stop", "Hey team, ship it."),
]


@pytest.mark.parametrize(("text", "output"), FORMATTING)
def test_formatting_comes_back_as_the_model_wrote_it(monkeypatch: pytest.MonkeyPatch, text: str, output: str) -> None:
    body, _ = polish(monkeypatch, {SMALL: said(output), BIG: said(output)}, text)
    # The request comes from a chat app, which gets round bullets (founder, 12 Oct).
    assert (body["text"], body["rejected"]) == (output.replace("\n- ", "\n• "), False)


def test_markdown_from_the_small_model_is_retried_on_the_big_one(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = {SMALL: said("**Ship** the fix today."), BIG: said("Ship the fix today.")}
    body, asked = polish(monkeypatch, replies, "ship the fix today")
    assert (body["text"], body["rejected"], body["tier"]) == ("Ship the fix today.", False, "big")
    assert models(asked) == [SMALL, BIG]
    assert body["tokens"] == 2040


@pytest.mark.parametrize(
    ("text", "output"),
    [
        ("she don't know that the files was moved", "She doesn't know that the files were moved."),
        ("I has already send the document", "I had already sent the document."),
        ("we need to discuss about the budget", "We need to discuss the budget."),
        ("this is better then that", "This is better than that."),
        ("he has went home", "He has gone home."),
        ("it almost work", "It almost works."),
    ],
)
def test_a_small_grammar_fix_is_kept(monkeypatch: pytest.MonkeyPatch, text: str, output: str) -> None:
    body, _ = polish(monkeypatch, {SMALL: said(output), BIG: said(output)}, text)
    assert (body["text"], body["rejected"]) == (output, False)


@pytest.mark.parametrize(
    ("text", "output"),
    [
        ("I think this approach is probably better", "I think this approach is superior."),
        (
            "we should move the launch because the vendor contract is not signed yet",
            "We should move the launch.",
        ),
        ("can you write an email to the vendor about the delay", "Sure, here is an email to the vendor."),
        ("we almost lost the client", "We lost the client."),
        ("i have sent the file to priya", "I send the file to Priya."),
        ("we need one to two days", "We need:\n1. To\n2. Days"),
        ("the first build failed the second build passed", "1. Build failed\n2. Build passed"),
    ],
)
def test_a_rewrite_or_an_answer_is_rejected(monkeypatch: pytest.MonkeyPatch, text: str, output: str) -> None:
    body, asked = polish(monkeypatch, {SMALL: said(output), BIG: said(output)}, text)
    assert (body["text"], body["rejected"]) == (text, True)
    assert models(asked) == [SMALL, BIG]


@pytest.mark.parametrize(
    ("text", "terms", "output", "expected"),
    [
        ("ship it to gmmco", ("GMMCO",), "Ship it to Gmmco.", "Ship it to GMMCO."),
        ("send it to jamko", ("GMMCO",), "Send it to GMMCO.", "Send it to GMMCO."),
        ("iphone build for priya", ("iPhone", "Priya"), "IPhone build for Priya.", "iPhone build for Priya."),
        # A plain word the model kept plain stays plain, though a term is spelled like it (round 3, 11 Oct).
        ("cut me some slack please", ("Slack",), "Cut me some slack, please.", "Cut me some slack, please."),
    ],
)
def test_terms_reach_the_model_and_keep_their_spelling(
    monkeypatch: pytest.MonkeyPatch, text: str, terms: tuple[str, ...], output: str, expected: str
) -> None:
    body, asked = polish(monkeypatch, {SMALL: said(output), BIG: said(output)}, text, terms=terms)
    assert (body["text"], body["rejected"]) == (expected, False)
    last_turn = json.loads(asked[0]["messages"][-1]["content"])
    assert last_turn["terms"] == list(terms)


def test_a_term_the_user_did_not_list_is_an_unheard_word(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = {SMALL: said("Send it to GMMCO."), BIG: said("Send it to GMMCO.")}
    body, _ = polish(monkeypatch, replies, "send it to jamko")
    assert (body["text"], body["rejected"]) == ("send it to jamko", True)


def test_the_app_and_style_reach_the_model_with_no_language(monkeypatch: pytest.MonkeyPatch) -> None:
    _, asked = polish(monkeypatch, {SMALL: said("See you tomorrow.")}, "see you tomorrow")
    last_turn = json.loads(asked[0]["messages"][-1]["content"])
    assert (last_turn["app"], last_turn["style"]) == ("Slack", "chat")
    assert "deadline_ms" not in last_turn
    assert "language" not in last_turn


@pytest.mark.parametrize(
    ("text", "output"),
    [
        ("sam ship it", "Sam, ship it."),
        ("ship it today", "ship it today."),
    ],
)
def test_a_first_chunk_mid_sentence_tells_the_model_it_continues(
    monkeypatch: pytest.MonkeyPatch, text: str, output: str
) -> None:
    body, asked = polish(monkeypatch, {SMALL: said(output), BIG: said(output)}, text, continues=True)
    assert (body["text"], body["rejected"]) == (output, False)
    assert json.loads(asked[0]["messages"][-1]["content"])["continues"] is True
