"""The edits the app's outbox sends after a transcript is ready (14-transcript-editing LLD): line fixes, renames,
new speakers and Roman letters. The backend keeps the current line only; the last write wins."""

import asyncio
import threading
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from conftest import RECORDING, Sql, failure, follow, generate, register

from snapbak_backend.ai.chain import Tiers
from snapbak_backend.ai.providers.base import ProviderError
from snapbak_backend.ai.providers.fake import Fake
from snapbak_backend.services import transliteration

Start = Callable[[str], None]


@pytest.fixture
def ready(api: Any, start_worker: Start) -> tuple[dict[str, str], dict[str, Any]]:
    start_worker("two-speakers-en")
    auth = register(api)
    follow(api, auth, generate(api, auth))
    transcript = api.get(f"/v1/recordings/{RECORDING}/transcript", headers=auth).json()
    return auth, transcript


def fixed_line(line: dict[str, Any], speaker: str, rev: int) -> dict[str, Any]:
    words = [{**line["words"][0], "w": "Hello", "conf": 1}, *line["words"][1:]]
    return {"text": " ".join(word["w"] for word in words), "words": words, "speakerId": speaker, "rev": rev}


def test_trx10_a_line_fix_replaces_the_line_and_bumps_its_rev(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    line = transcript["lines"][0]
    other = transcript["speakers"][1]["id"]
    first = api.patch(f"/v1/lines/{line['id']}", json=fixed_line(line, other, 1), headers=auth)
    again = api.patch(f"/v1/lines/{line['id']}", json=fixed_line(line, other, 1), headers=auth)
    assert (first.status_code, first.json(), again.json()) == (200, {"rev": 2}, {"rev": 3})
    stored = api.get(f"/v1/recordings/{RECORDING}/transcript", headers=auth).json()["lines"][0]
    assert (stored["text"], stored["speakerId"], stored["rev"]) == ("Hello Arjun, um, thanks for joining.", other, 3)
    assert (stored["start"], stored["end"], stored["words"][0]["conf"]) == (1200, 2940, 1)


def test_trx10_a_line_fix_clears_its_roman_words(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    line = transcript["lines"][0]
    sql("UPDATE transcript_lines SET roman_text = 'x', roman_words = '[]'")
    api.patch(f"/v1/lines/{line['id']}", json=fixed_line(line, line["speakerId"], 1), headers=auth)
    assert sql("SELECT roman_text FROM transcript_lines WHERE id = $1", uuid.UUID(line["id"])) == [{"roman_text": None}]


def test_trx10_a_line_fix_to_a_speaker_of_another_recording_or_an_unknown_line_is_refused(
    api: Any, ready: tuple
) -> None:
    auth, transcript = ready
    line = transcript["lines"][0]
    stranger = api.patch(f"/v1/lines/{line['id']}", json=fixed_line(line, str(uuid.uuid4()), 1), headers=auth)
    unknown = api.patch(f"/v1/lines/{uuid.uuid4()}", json=fixed_line(line, line["speakerId"], 1), headers=auth)
    empty = api.patch(
        f"/v1/lines/{line['id']}", json={**fixed_line(line, line["speakerId"], 1), "words": []}, headers=auth
    )
    other_device = api.patch(
        f"/v1/lines/{line['id']}", json=fixed_line(line, line["speakerId"], 1), headers=register(api, str(uuid.uuid4()))
    )
    assert [failure(r) for r in (stranger, unknown, empty, other_device)] == [
        (422, "invalid"),
        (404, "not_found"),
        (422, "invalid"),
        (404, "not_found"),
    ]


def test_trx08_a_rename_is_stored_as_the_users_name(api: Any, ready: tuple) -> None:
    auth, transcript = ready
    speaker = transcript["speakers"][0]
    response = api.patch(f"/v1/speakers/{speaker['id']}", json={"name": "Priya S"}, headers=auth)
    assert (response.status_code, response.json()) == (
        200,
        {"id": speaker["id"], "label": "Speaker 1", "name": "Priya S", "nameSource": "user", "colorIdx": 0},
    )
    assert failure(api.patch(f"/v1/speakers/{speaker['id']}", json={"name": ""}, headers=auth)) == (422, "invalid")


def test_trx08_a_new_speaker_from_the_mac_is_made_once_by_its_id(api: Any, ready: tuple) -> None:
    auth, transcript = ready
    speaker = str(uuid.uuid4())
    body = {"label": "Speaker 3", "name": None, "colorIdx": 2}
    made = api.put(f"/v1/recordings/{RECORDING}/speakers/{speaker}", json=body, headers=auth)
    again = api.put(f"/v1/recordings/{RECORDING}/speakers/{speaker}", json=body, headers=auth)
    taken = api.put(f"/v1/recordings/{RECORDING}/speakers/{uuid.uuid4()}", json=body, headers=auth)
    assert (made.status_code, again.status_code, taken.status_code) == (201, 200, 422)
    assert made.json() == {"id": speaker, "label": "Speaker 3", "name": None, "nameSource": None, "colorIdx": 2}
    line = transcript["lines"][1]
    moved = api.patch(f"/v1/lines/{line['id']}", json=fixed_line(line, speaker, 1), headers=auth)
    assert moved.status_code == 200
    labels = [s["label"] for s in api.get(f"/v1/recordings/{RECORDING}/transcript", headers=auth).json()["speakers"]]
    assert labels == ["Speaker 1", "Speaker 2", "Speaker 3"]


def test_trx09_transliterate_returns_roman_words_for_every_line(api: Any, ready: tuple) -> None:
    auth, transcript = ready
    response = api.post(f"/v1/recordings/{RECORDING}/transliterate", json={"script": "roman"}, headers=auth)
    lines = response.json()["lines"]
    assert response.status_code == 200
    assert [line["id"] for line in lines] == [line["id"] for line in transcript["lines"]]
    # English lines have no Devanagari, so their Roman words are their own words, without asking the model.
    assert lines[0]["romanText"] == "Hi Arjun, um, thanks for joining."
    assert lines[0]["romanWords"] == transcript["lines"][0]["words"]


def test_trx09_transliterate_needs_a_ready_transcript(api: Any, start_worker: Start) -> None:
    auth = register(api)
    response = api.post(f"/v1/recordings/{uuid.uuid4()}/transliterate", json={"script": "roman"}, headers=auth)
    assert failure(response) == (404, "not_found")


def test_trx09_a_model_that_cant_answer_is_a_502_and_leaves_hindi_lines_without_roman_words(
    api: Any, ready: tuple, sql: Sql
) -> None:
    auth, _ = ready
    sql("UPDATE transcript_lines SET text = 'तो यहाँ' WHERE idx = 0")
    api.app.state.tiers = Tiers(polish=Fake("fail"), command=Fake("fail"), fallback=None)
    response = api.post(f"/v1/recordings/{RECORDING}/transliterate", json={"script": "roman"}, headers=auth)
    assert (response.status_code, response.json()["error"]["code"]) == (502, "provider")
    assert sql("SELECT roman_text FROM transcript_lines WHERE idx = 0") == [{"roman_text": None}]


class HeldEcho:
    """The fake model, held until the test lets it answer, so the test can look at the database and change lines
    while the model thinks. `asked` is set when the first request arrives."""

    engine = "fake:held-echo"

    def __init__(self) -> None:
        self.asked = threading.Event()
        self.answer = threading.Event()

    async def complete(self, turns: Any, max_tokens: int, timeout: float, reason: str = "") -> Any:
        self.asked.set()
        while not self.answer.is_set():
            await asyncio.sleep(0.01)
        answer = await Fake("echo").complete(turns, max_tokens, timeout)
        return answer

    async def aclose(self) -> None:
        pass


class FailsAfter:
    """The fake model, answering `answers` requests, then failing every one; it counts what it was asked."""

    engine = "fake:fails-after"

    def __init__(self, answers: int) -> None:
        self.answers = answers
        self.asked = 0

    async def complete(self, turns: Any, max_tokens: int, timeout: float, reason: str = "") -> Any:
        self.asked += 1
        if self.asked > self.answers:
            raise ProviderError("fake: down")
        answer = await Fake("echo").complete(turns, max_tokens, timeout)
        return answer

    async def aclose(self) -> None:
        pass


def transliterate(api: Any, auth: dict[str, str]) -> Any:
    return api.post(f"/v1/recordings/{RECORDING}/transliterate", json={"script": "roman"}, headers=auth)


def test_trx09_transliteration_holds_no_transaction_while_the_model_thinks_and_keeps_concurrent_changes(
    api: Any, ready: tuple, sql: Sql
) -> None:
    auth, transcript = ready
    sql("UPDATE transcript_lines SET text = 'तो यहाँ' WHERE idx IN (0, 1, 2)")
    model = HeldEcho()
    api.app.state.tiers = Tiers(polish=model, command=model, fallback=None)
    answers: list[Any] = []
    asking = threading.Thread(target=lambda: answers.append(transliterate(api, auth)))
    asking.start()
    assert model.asked.wait(5)
    held = sql(
        "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
        " AND state = 'idle in transaction'"
    )
    line = transcript["lines"][0]
    edited = api.patch(f"/v1/lines/{line['id']}", json=fixed_line(line, line["speakerId"], 1), headers=auth)
    sql("UPDATE transcript_lines SET deleted_at = now() WHERE idx = 2")
    model.answer.set()
    asking.join()
    assert held == [{"n": 0}]
    assert edited.json() == {"rev": 2}
    romans = {item["id"]: item["romanText"] for item in answers[0].json()["lines"]}
    # The edited line keeps its edit and no Roman words, the deleted one gets none and isn't listed, and the
    # untouched one gets the model's.
    assert (romans[line["id"]], romans[transcript["lines"][1]["id"]]) == (None, "r:Thanks r:Priya.")
    assert transcript["lines"][2]["id"] not in romans
    assert sql("SELECT idx, text, rev, roman_text FROM transcript_lines WHERE idx IN (0, 2) ORDER BY idx") == [
        {"idx": 0, "text": "Hello Arjun, um, thanks for joining.", "rev": 2, "roman_text": None},
        {"idx": 2, "text": "तो यहाँ", "rev": 1, "roman_text": None},
    ]


def test_trx09_each_batch_is_kept_as_it_arrives_so_a_retry_pays_only_for_the_rest(
    api: Any, ready: tuple, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth, _ = ready
    monkeypatch.setattr(transliteration, "BATCH_LINES", 1)
    sql("UPDATE transcript_lines SET text = 'तो यहाँ' WHERE idx IN (0, 1)")
    first = FailsAfter(1)
    api.app.state.tiers = Tiers(polish=first, command=first, fallback=None)
    assert failure(transliterate(api, auth)) == (502, "provider")
    romans = sql("SELECT idx, roman_text IS NOT NULL AS kept FROM transcript_lines WHERE idx IN (0, 1) ORDER BY idx")
    assert romans == [{"idx": 0, "kept": True}, {"idx": 1, "kept": False}]
    again = FailsAfter(1)
    api.app.state.tiers = Tiers(polish=again, command=again, fallback=None)
    assert transliterate(api, auth).status_code == 200
    assert again.asked == 1


def test_trx10_a_line_fix_to_a_speaker_of_another_of_the_devices_recordings_is_invalid(
    api: Any, ready: tuple, sql: Sql
) -> None:
    auth, transcript = ready
    line = transcript["lines"][0]
    other = str(uuid.uuid4())
    api.put(
        f"/v1/recordings/{other}",
        json={"title": "Other", "durationMs": 1000, "createdAt": "2026-10-17T04:32:11Z"},
        headers=auth,
    )
    theirs = uuid.uuid4()
    sql(
        "INSERT INTO speakers (id, recording_id, label, vendor_label, color_idx) VALUES ($1, $2, 'Speaker 1', 'S0', 0)",
        theirs,
        uuid.UUID(other),
    )
    answer = api.patch(f"/v1/lines/{line['id']}", json=fixed_line(line, str(theirs), 1), headers=auth)
    assert failure(answer) == (422, "invalid")
    assert sql("SELECT speaker_id FROM transcript_lines WHERE id = $1", uuid.UUID(line["id"])) == [
        {"speaker_id": uuid.UUID(line["speakerId"])}
    ]
