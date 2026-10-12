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
    language: str = "en",
    continues: bool = False,
) -> tuple[dict, list[dict]]:
    """The response, and every request body the models got."""
    env = {
        "SNAPBAK_TOKEN": "dev",
        "SNAPBAK_SMALL": f"openai:{SMALL}",
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
        "language": language,
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
    ("no wait make it thursday", "Let's meet on Friday at noon.", "en", "Let's meet on Thursday at noon.", ""),
    ("deploy on tuesday actually wednesday", "", "en", "Deploy on Wednesday.", None),
    ("Send this to Jam Co. No, send this to GMMCO.", "", "en", "Send this to GMMCO.", None),
    ("kal milte hain nahi nahi parso milte hain", "", "hinglish", "Parso milte hain.", None),
    ("nahi nahi parso rakhte hain", "Kal meeting rakhte hain.", "hinglish", "Parso meeting rakhte hain.", ""),
    ("कल मिलते हैं नहीं नहीं परसों मिलते हैं", "", "hi", "परसों मिलते हैं।", None),
    ("नहीं नहीं परसों", "कल मीटिंग रखते हैं।", "hi", "परसों मीटिंग रखते हैं।", ""),
    (
        "We should send this to the frontend team. No, don't send it yet.",
        "",
        "en",
        "Don't send it yet.",
        None,
    ),
    ("No, don't send it yet.", "We should send this to the frontend team.", "en", "Don't send it yet.", ""),
    ("client ko bhejo. nahi, abhi mat bhejo", "", "hinglish", "Abhi mat bhejo.", None),
    ("nahi, abhi mat bhejo", "Client ko bhejo.", "hinglish", "Abhi mat bhejo.", ""),
    ("क्लाइंट को भेजो। नहीं, अभी मत भेजो", "", "hi", "अभी मत भेजो।", None),
    ("नहीं, अभी मत भेजो", "क्लाइंट को भेजो।", "hi", "अभी मत भेजो।", ""),
    ("book the flight to delhi scratch that call priya", "", "en", "Call Priya.", None),
    ("send it to sam i mean ravi", "", "en", "Send it to Ravi.", None),
    ("book five seats make that six", "", "en", "Book 6 seats.", None),
    ("मीटिंग सोमवार को है बल्कि मंगलवार को है", "", "hi", "मीटिंग मंगलवार को है।", None),
    ("meeting monday ko hai balki tuesday ko hai", "", "hinglish", "Meeting Tuesday ko hai.", None),
    ("scratch that", "Ship it today team. Also loop in Priya on the budget doc.", "en", "", "Ship it today team."),
    ("मेल राहुल को भेजो सॉरी रोहित को", "", "hi", "मेल रोहित को भेजो।", None),
    ("nahi nahi tum next friday nahi sunday aao", "", "hinglish", "Tum next Sunday aao.", None),
    ("नहीं नहीं तुम अगले फ्राइडे नहीं संडे आओ", "", "hi", "तुम अगले संडे आओ।", None),
    ("pod delete karo matlab rehne do", "", "hinglish", "Rehne do.", None),
    ("पॉड डिलीट करो मतलब रहने दो", "", "hi", "रहने दो।", None),
    ("main kal nahi parso aaunga", "", "hinglish", "Main parso aaunga.", None),
    ("मैं कल नहीं परसों आऊंगा", "", "hi", "मैं परसों आऊंगा।", None),
    ("don't send it, no wait, send it", "", "en", "Send it.", None),
    ("no wait send it", "Don't send it.", "en", "", "Send it."),
    ("mat bhejo, nahi nahi, bhejo", "", "hinglish", "Bhejo.", None),
    ("मत भेजो नहीं नहीं भेजो", "", "hi", "भेजो।", None),
    ("mujhe 3 tickets nahi 4 chahiye", "", "hinglish", "Mujhe 4 tickets chahiye.", None),
    ("मुझे तीन टिकट नहीं चार चाहिए", "", "hi", "मुझे चार टिकट चाहिए।", None),
]


@pytest.mark.parametrize(("text", "prior", "language", "output", "revision"), BACKTRACKING)
def test_backtracking_drops_the_retracted_words_on_the_big_model(
    monkeypatch: pytest.MonkeyPatch, text: str, prior: str, language: str, output: str, revision: str | None
) -> None:
    body, asked = polish(monkeypatch, {BIG: said(output, revision)}, text, prior=prior, language=language)
    assert (body["text"], body["revision"], body["rejected"], body["tier"]) == (output, revision, False, "big")
    assert models(asked) == [BIG]


@pytest.mark.parametrize(
    ("text", "prior", "language", "output", "revision"),
    [
        (
            "no wait make it thursday",
            "We need a list. Let's meet on Friday at noon.",
            "en",
            "Let's meet on Thursday at noon.",
            "We need a list.",
        ),
        ("no wait make it thursday", "Let's meet on Friday at noon.", "en", "Let's meet on Thursday at noon.", ""),
        (
            "nahi nahi parso",
            "Report ready hai. Kal meeting hai.",
            "hinglish",
            "Parso meeting hai.",
            "Report ready hai.",
        ),
        ("नहीं नहीं परसों", "रिपोर्ट तैयार है। कल मीटिंग है।", "hi", "परसों मीटिंग है।", "रिपोर्ट तैयार है।"),
    ],
)
def test_a_correction_is_asked_with_the_last_sentence_and_answered_as_text_and_revision(
    monkeypatch: pytest.MonkeyPatch, text: str, prior: str, language: str, output: str, revision: str
) -> None:
    body, asked = polish(monkeypatch, {BIG: said(output)}, text, prior=prior, language=language)
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
    ("text", "language", "output"),
    [
        ("mujhe nahi pata kya hua", "hinglish", "Mujhe nahi pata kya hua."),
        ("मुझे नहीं पता क्या हुआ", "hi", "मुझे नहीं पता क्या हुआ।"),
        ("it actually works now", "en", "It actually works now."),
        ("iska matlab hai ki hum ready hain", "hinglish", "Iska matlab hai ki hum ready hain."),
    ],
)
def test_an_ordinary_cue_word_stays_in_each_language(
    monkeypatch: pytest.MonkeyPatch, text: str, language: str, output: str
) -> None:
    body, asked = polish(monkeypatch, {BIG: said(output)}, text, language=language)
    assert (body["text"], body["rejected"], body["tier"]) == (output, False, "big")
    assert models(asked) == [BIG]


@pytest.mark.parametrize(
    ("text", "prior", "language", "output", "revision"),
    [
        ("deploy on tuesday then wednesday", "", "en", "Deploy on Wednesday.", None),
        ("no wait thursday", "Let's meet on Friday at noon.", "en", "", "Let's meet on Thursday for lunch."),
        ("nahi nahi parso", "Kal meeting hai.", "hinglish", "", "The meeting is the day after tomorrow."),
        ("नहीं नहीं परसों", "कल मीटिंग है।", "hi", "", "parso meeting hai."),
        ("No, don't send it yet.", "We should send this to the frontend team.", "en", "", ""),
        ("no, don't send it yet", "", "en", "No.", None),
        ("nahi, abhi mat bhejo", "Client ko bhejo.", "hinglish", "", "Client ko bhejo."),
        ("नहीं, अभी मत भेजो", "क्लाइंट को भेजो।", "hi", "", ""),
        ("actually", "Hi team, the launch moved to Monday and legal signed off.", "en", "", ""),
        ("is shabd ka matlab kya hai", "", "hinglish", "Kya hai?", None),
        ("इस शब्द का मतलब क्या है", "", "hi", "क्या है?", None),
        ("ravi said sorry about the delay", "", "en", "About the delay.", None),
        ("main friday nahi aaunga", "", "hinglish", "Main friday aaunga.", None),
        ("file delete mat karo", "", "hinglish", "File delete karo.", None),
        ("मैं फ्राइडे नहीं आऊंगा", "", "hi", "मैं फ्राइडे आऊंगा।", None),
        ("main friday nahi aaunga", "", "hinglish", "Main aaunga.", None),
        ("मैं कल नहीं आऊंगा", "", "hi", "मैं आऊंगा।", None),
        ("tell the vendor no pickup after six pm", "", "en", "Tell the pickup after 6 pm.", None),
        ("aur report bhejo", "Main Friday nahi aaunga.", "hinglish", "Aur report bhejo.", "Main aaunga."),
        ("pod delete karo matlab report bhejo", "", "hinglish", "Report bhejo.", None),
        ("send it no wait send it", "", "en", "Don't send it.", None),
        ("i have 2 no kids", "", "en", "I have 2 kids.", None),
        ("main 2 din nahi aaunga", "", "hinglish", "Main aaunga.", None),
        ("मैं दो दिन नहीं आऊंगा", "", "hi", "मैं आऊंगा।", None),
    ],
)
def test_a_correction_that_drops_or_invents_words_is_rejected(
    monkeypatch: pytest.MonkeyPatch, text: str, prior: str, language: str, output: str, revision: str | None
) -> None:
    replies = {SMALL: said(output, revision), BIG: said(output, revision)}
    body, _ = polish(monkeypatch, replies, text, prior=prior, language=language)
    assert (body["text"], body["revision"], body["rejected"]) == (text, None, True)


FORMATTING = [
    (
        "we need three things first the api second the ui third the docs",
        "en",
        "We need three things:\n1. The API\n2. The UI\n3. The docs",
    ),
    ("for the trip bullet towels bullet a charger", "en", "For the trip:\n- Towels\n- A charger"),
    ("it costs twenty five dollars mail john at procol dot in", "en", "It costs $25. Mail john@procol.in."),
    ("we ship on march third at nine am", "en", "We ship on March 3 at 9 am."),
    ("hey team comma ship it full stop", "en", "Hey team, ship it."),
    ("kal teen baje meeting hai", "hinglish", "Kal 3 baje meeting hai."),
    ("कल तीन बजे मीटिंग है", "hi", "कल 3 बजे मीटिंग है।"),
]


@pytest.mark.parametrize(("text", "language", "output"), FORMATTING)
def test_formatting_comes_back_as_the_model_wrote_it(
    monkeypatch: pytest.MonkeyPatch, text: str, language: str, output: str
) -> None:
    body, _ = polish(monkeypatch, {SMALL: said(output), BIG: said(output)}, text, language=language)
    # The request comes from a chat app, which gets round bullets (founder, 12 Oct).
    assert (body["text"], body["rejected"]) == (output.replace("\n- ", "\n• "), False)


def test_markdown_from_the_small_model_is_retried_on_the_big_one(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = {SMALL: said("**Ship** the fix today."), BIG: said("Ship the fix today.")}
    body, asked = polish(monkeypatch, replies, "ship the fix today")
    assert (body["text"], body["rejected"], body["tier"]) == ("Ship the fix today.", False, "big")
    assert models(asked) == [SMALL, BIG]
    assert body["tokens"] == 2040


@pytest.mark.parametrize(
    ("text", "language", "output"),
    [
        ("kal client ko call karna hai", "hinglish", "Call the client tomorrow."),
        ("कल क्लाइंट को कॉल करना है", "hi", "Call the client tomorrow."),
        ("कल क्लाइंट को कॉल करना है", "hi", "kal client ko call karna hai."),
    ],
)
def test_a_translation_or_transliteration_is_rejected(
    monkeypatch: pytest.MonkeyPatch, text: str, language: str, output: str
) -> None:
    body, _ = polish(monkeypatch, {BIG: said(output)}, text, language=language)
    assert (body["text"], body["rejected"]) == (text, True)


@pytest.mark.parametrize(
    ("text", "language", "output"),
    [
        ("she don't know that the files was moved", "en", "She doesn't know that the files were moved."),
        ("I has already send the document", "en", "I had already sent the document."),
        ("we need to discuss about the budget", "en", "We need to discuss the budget."),
        ("usne report bheja tha", "hinglish", "Usne report bheji thi."),
        ("वह कल आएगी और रिपोर्ट भेजेगा", "hi", "वह कल आएगी और रिपोर्ट भेजेगी।"),
        ("this is better then that", "en", "This is better than that."),
        ("he has went home", "en", "He has gone home."),
        ("it almost work", "en", "It almost works."),
    ],
)
def test_a_small_grammar_fix_is_kept(monkeypatch: pytest.MonkeyPatch, text: str, language: str, output: str) -> None:
    body, _ = polish(monkeypatch, {SMALL: said(output), BIG: said(output)}, text, language=language)
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
    ("text", "language", "output"),
    [
        ("kal client ko call karna hai", "hinglish", "Kal client ko phone karna hai."),
        ("कल क्लाइंट को कॉल करना है", "hi", "कल क्लाइंट को फ़ोन करना है।"),
        ("kal meeting hai", "hinglish", "Kal ka meeting ki hai ke tha."),
        ("कल मीटिंग है", "hi", "कल का मीटिंग की है के था।"),
        ("kal meeting hai", "hinglish", "Kal meeting thi."),
        ("मैं कल रिपोर्ट भेजूंगा", "hi", "मैं कल रिपोर्ट भेजता हूं।"),
        ("build lagbhag ready hai", "hinglish", "Build ready hai."),
        ("बिल्ड अक्सर फेल होता है", "hi", "बिल्ड फेल होता है।"),
        ("humein ek se do din chahiye", "hinglish", "Humein:\n1. Se\n2. Din chahiye"),
        ("हमें एक से दो दिन चाहिए", "hi", "हमें:\n1. से\n2. दिन चाहिए"),
        (
            "mera pehla draft weak tha mera doosra draft strong tha",
            "hinglish",
            "1. Draft weak tha\n2. Draft strong tha",
        ),
        ("मेरा पहला ड्राफ्ट कमज़ोर था मेरा दूसरा ड्राफ्ट अच्छा था", "hi", "1. ड्राफ्ट कमज़ोर था\n2. ड्राफ्ट अच्छा था"),
    ],
)
def test_a_hinglish_or_hindi_rewrite_is_rejected_on_the_big_model(
    monkeypatch: pytest.MonkeyPatch, text: str, language: str, output: str
) -> None:
    body, asked = polish(monkeypatch, {BIG: said(output)}, text, language=language)
    assert (body["text"], body["rejected"]) == (text, True)
    assert models(asked) == [BIG, BIG], "a rejected big answer is asked again once"


@pytest.mark.parametrize(
    ("text", "terms", "language", "output", "expected"),
    [
        ("ship it to gmmco", ("GMMCO",), "en", "Ship it to Gmmco.", "Ship it to GMMCO."),
        ("send it to jamko", ("GMMCO",), "en", "Send it to GMMCO.", "Send it to GMMCO."),
        ("iphone build for priya", ("iPhone", "Priya"), "en", "IPhone build for Priya.", "iPhone build for Priya."),
        # A plain word the model kept plain stays plain, though a term is spelled like it (round 3, 11 Oct).
        ("cut me some slack please", ("Slack",), "en", "Cut me some slack, please.", "Cut me some slack, please."),
        ("gmmco ko invoice bhejo", ("GMMCO",), "hinglish", "Gmmco ko invoice bhejo.", "GMMCO ko invoice bhejo."),
        ("प्रिया को रिपोर्ट भेजो", ("प्रिया",), "hi", "प्रिया को रिपोर्ट भेजो।", "प्रिया को रिपोर्ट भेजो।"),
    ],
)
def test_terms_reach_the_model_and_keep_their_spelling(
    monkeypatch: pytest.MonkeyPatch, text: str, terms: tuple[str, ...], language: str, output: str, expected: str
) -> None:
    body, asked = polish(monkeypatch, {SMALL: said(output), BIG: said(output)}, text, terms=terms, language=language)
    assert (body["text"], body["rejected"]) == (expected, False)
    last_turn = json.loads(asked[0]["messages"][-1]["content"])
    assert last_turn["terms"] == list(terms)


@pytest.mark.parametrize(
    ("text", "language", "terms", "output"),
    [
        (
            "main kal office aaunga bahut kaam hai",
            "hinglish",
            ("main", "kal", "aaunga", "bahut", "hai"),
            "Main kal office aaunga. Bahut kaam hai.",
        ),
        ("accha theek hai", "hinglish", ("accha", "theek", "hai"), "Accha, theek hai."),
        ("बहुत काम है", "hi", ("बहुत",), "बहुत काम है।"),
        ("npm install karo", "hinglish", ("npm", "karo"), "npm install karo."),
    ],
)
def test_hinglish_lexicon_terms_keep_the_sentence_start_capital(
    monkeypatch: pytest.MonkeyPatch, text: str, language: str, terms: tuple[str, ...], output: str
) -> None:
    body, _ = polish(monkeypatch, {SMALL: said(output), BIG: said(output)}, text, terms=terms, language=language)
    assert (body["text"], body["rejected"]) == (output, False)


def test_a_term_the_user_did_not_list_is_an_unheard_word(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = {SMALL: said("Send it to GMMCO."), BIG: said("Send it to GMMCO.")}
    body, _ = polish(monkeypatch, replies, "send it to jamko")
    assert (body["text"], body["rejected"]) == ("send it to jamko", True)


def test_the_app_style_and_language_reach_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _, asked = polish(monkeypatch, {BIG: said("Kal milte hain.")}, "kal milte hain", language="hinglish")
    last_turn = json.loads(asked[0]["messages"][-1]["content"])
    assert (last_turn["app"], last_turn["style"], last_turn["language"]) == ("Slack", "chat", "hinglish")
    assert "deadline_ms" not in last_turn


@pytest.mark.parametrize(
    ("text", "language", "output"),
    [
        ("sam ship it", "en", "Sam, ship it."),
        ("ship it today", "en", "ship it today."),
        ("rahul kal aayega", "hinglish", "Rahul kal aayega."),
        ("राहुल कल आएगा", "hi", "राहुल कल आएगा।"),
    ],
)
def test_a_first_chunk_mid_sentence_tells_the_model_it_continues(
    monkeypatch: pytest.MonkeyPatch, text: str, language: str, output: str
) -> None:
    body, asked = polish(monkeypatch, {SMALL: said(output), BIG: said(output)}, text, language=language, continues=True)
    assert (body["text"], body["rejected"]) == (output, False)
    assert json.loads(asked[0]["messages"][-1]["content"])["continues"] is True
