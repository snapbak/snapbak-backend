"""Caption translation's batching, answer checks, retry and stored lines (TRX-07, 16-caption-translation LLD,
algorithms C and D), without a database."""

import json
from uuid import uuid4

import pytest

from snapbak_backend.ai import languages
from snapbak_backend.ai.prompts import translate as prompts
from snapbak_backend.ai.providers.base import ProviderError, Turn, Usage
from snapbak_backend.ai.providers.fake import Fake
from snapbak_backend.schemas.translations import SourceLine
from snapbak_backend.services import translate


def source(idx: int, text: str = "", source_hash: str = "h") -> SourceLine:
    return SourceLine(idx=idx, lineId=f"ln_{idx}", rev=1, sourceHash=source_hash, text=text or f"Line {idx}.")


def plan(work: list[SourceLine]) -> translate.Plan:
    spanish = languages.BY_CODE["es"]
    made = translate.Plan(uuid4(), spanish, "en", "English", ["Priya"], work, work, {}, None)
    return made


class Scripted:
    """Answers each call with the next of `answers`, an exception raised; records each call's turns and max_tokens."""

    def __init__(self, *answers: str | Exception) -> None:
        self.answers = list(answers)
        self.turns: list[list[Turn]] = []
        self.max_tokens: list[int] = []
        self.engine = "scripted:x"

    async def complete(self, turns: list[Turn], max_tokens: int, timeout: float, reason: str = "") -> tuple[str, Usage]:
        self.turns.append(turns)
        self.max_tokens.append(max_tokens)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, Usage(10, 5)

    async def aclose(self) -> None:
        pass


def answer(*entries: tuple[int, str]) -> str:
    text = json.dumps({"lines": [{"idx": idx, "text": text} for idx, text in entries]})
    return text


def test_trx07_batches_of_40_with_3_context_lines() -> None:
    page = [source(idx) for idx in range(100)]
    made = translate.batches(page, page)
    assert [(b.lines[0].idx, b.lines[-1].idx) for b in made] == [(0, 39), (40, 79), (80, 99)]
    assert [[line.idx for line in b.before] for b in made] == [[], [37, 38, 39], [77, 78, 79]]
    assert [[line.idx for line in b.after] for b in made] == [[40, 41, 42], [80, 81, 82], []]


def test_trx07_context_comes_from_the_page_around_the_work_lines() -> None:
    page = [source(idx) for idx in range(10)]
    made = translate.batches([page[5]], page)
    assert ([line.idx for line in made[0].before], [line.idx for line in made[0].after]) == ([2, 3, 4], [6, 7, 8])


def test_trx07_context_before_the_first_lines_is_only_what_the_page_has() -> None:
    page = [source(idx) for idx in range(10)]
    first, second = translate.batches([page[1]], page)[0], translate.batches([page[2]], page)[0]
    assert [line.idx for line in first.before] == [0]
    assert [line.idx for line in second.before] == [0, 1]


def test_trx07_unchanged_line_skips_model_call() -> None:
    stored = {"sourceHash": "h", "text": "Hola.", "failed": False}
    assert translate.matches(stored, source(1, source_hash="h"))
    assert not translate.matches(stored, source(1, source_hash="other"))
    assert not translate.matches({**stored, "failed": True}, source(1))
    assert not translate.matches({**stored, "text": None}, source(1))
    assert not translate.matches(None, source(1))


def test_trx07_newlines_replaced_and_length_capped() -> None:
    lines = [source(1, "Hi."), source(2, "Go.")]
    crlf, _ = translate.checked(answer((1, "Ho\r\nla."), (2, "Go.")), lines)
    assert crlf[1] == "Ho la.", "a Windows line break is one space"
    good, problems = translate.checked(answer((1, "Ho\nla."), (2, "x" * 53)), lines)
    assert good == {1: "Ho la."}
    assert problems == ["idx 2 is far longer than its source"]
    at_the_cap, _ = translate.checked(answer((1, "x" * 52), (2, "Go.")), lines)
    assert at_the_cap[1] == "x" * 52, "4 characters per source character plus 40 is allowed"


def test_trx07_wrong_idx_set_is_a_problem_but_good_lines_stay() -> None:
    good, problems = translate.checked(answer((2, "Dos."), (1, "Uno.")), [source(1), source(2)])
    assert good == {1: "Uno.", 2: "Dos."}
    assert problems == ["the idx values must be exactly the lines in translate, in order, with no repeats"]


def test_trx07_an_answer_that_isnt_json_lines_is_one_problem() -> None:
    assert translate.checked("no", [source(1)]) == ({}, ['the answer must be JSON with a "lines" list'])
    assert translate.checked('{"lines": 3}', [source(1)]) == ({}, ['the answer must be JSON with a "lines" list'])


@pytest.mark.anyio
async def test_trx07_wrong_idx_set_retries_once_then_fails_lines() -> None:
    lines = [source(1), source(2)]
    model = Scripted(answer((1, "Uno.")), answer((1, "Uno.")))
    good, calls = await translate.translate_batch([model], plan(lines), translate.Batch([], lines, []))
    assert (good, len(calls), len(model.turns)) == ({1: "Uno."}, 2, 2)
    assert model.turns[1][-1][1] == (
        "Your answer broke these rules: the idx values must be exactly the lines in translate, in order, with no "
        "repeats.\nReturn JSON with exactly these idx values, in order: [1, 2]."
    )
    assert model.turns[1][-2] == ("assistant", answer((1, "Uno.")))


@pytest.mark.anyio
async def test_trx07_the_retry_fills_the_missing_line() -> None:
    lines = [source(1), source(2)]
    model = Scripted(answer((1, "Uno.")), answer((1, "Uno"), (2, "Dos.")))
    good, _ = await translate.translate_batch([model], plan(lines), translate.Batch([], lines, []))
    assert good == {1: "Uno", 2: "Dos."}


@pytest.mark.anyio
async def test_trx07_the_retry_keeps_the_first_answers_good_lines() -> None:
    lines = [source(1), source(2), source(3)]
    model = Scripted(answer((1, "Uno."), (2, "Dos.")), answer((3, "Tres.")))
    good, _ = await translate.translate_batch([model], plan(lines), translate.Batch([], lines, []))
    assert good == {1: "Uno.", 2: "Dos.", 3: "Tres."}


@pytest.mark.anyio
async def test_trx07_max_tokens_is_four_per_source_token_plus_200() -> None:
    lines = [source(1, "a" * 10), source(2, "b" * 12)]
    model = Scripted(answer((1, "Uno."), (2, "Dos.")))
    await translate.translate_batch([model], plan(lines), translate.Batch([source(0, "c" * 30)], lines, []))
    # 22 characters are ceil(22 / 3) = 8 tokens; the context lines don't count.
    assert model.max_tokens == [4 * 8 + 200]


@pytest.mark.anyio
async def test_trx07_a_model_that_cant_answer_fails_the_batch() -> None:
    lines = [source(1)]
    good, calls = await translate.translate_batch(
        [Scripted(ProviderError("down"))], plan(lines), translate.Batch([], lines, [])
    )
    assert (good, [call.verdict for call in calls]) == ({}, ["error"])


@pytest.mark.anyio
async def test_trx07_fake_translates_each_line_and_drop_mode_leaves_out_the_last() -> None:
    lines = [source(1, "Hi."), source(2, "Bye.")]
    batch = translate.Batch([source(0, "Context.")], lines, [])
    good, _ = await translate.translate_batch([Fake("echo")], plan(lines), batch)
    dropped, _ = await translate.translate_batch([Fake("translate-drop")], plan(lines), batch)
    assert (good, dropped) == ({1: "[es] Hi.", 2: "[es] Bye."}, {1: "[es] Hi."})


def test_trx07_failed_line_keeps_previous_text() -> None:
    previous = {"text": "Hola.", "edited": True, "editRev": 4}
    failed = translate.entry(source(1, source_hash="new"), None, previous, "T")
    fresh = translate.entry(source(2), None, None, "T")
    done = translate.entry(source(3), "Tres.", previous, "T")
    assert (failed["text"], failed["failed"], failed["edited"], failed["sourceHash"]) == ("Hola.", True, True, "new")
    assert (fresh["text"], fresh["failed"]) == (None, True)
    assert (done["text"], done["failed"], done["edited"], done["editRev"]) == ("Tres.", False, False, 4)


def test_trx07_keep_terms_are_speaker_names_then_capitalised_title_words() -> None:
    terms = translate.keep_terms(["Priya", "Arjun"], "Snapbak demo for Priya, with (Figma).")
    assert terms == ["Priya", "Arjun", "Snapbak", "Figma"]
    assert len(translate.keep_terms([f"N{n}" for n in range(80)], "")) == 50


def test_trx07_translate_prompt_carries_keep_terms_and_script_note() -> None:
    parts = ([{"idx": 9, "text": "Before."}], [{"idx": 10, "text": "Now."}], [])
    turns = prompts.turns("en", "English", "hi", "Hindi", ", Devanagari", ["Snapbak"], parts)
    system, user = turns
    assert system[1].startswith("You translate video captions from English to Hindi.")
    assert system[1].endswith("Write Hindi in its usual script, Devanagari.")
    assert '{"lines":[{"idx":<number>,"text":"<translation>"}]}' in system[1]
    assert json.loads(user[1]) == {
        "source": "en",
        "target": "hi",
        "keep": ["Snapbak"],
        "before": [{"idx": 9, "text": "Before."}],
        "translate": [{"idx": 10, "text": "Now."}],
        "after": [],
    }
