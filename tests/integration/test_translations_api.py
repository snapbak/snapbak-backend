"""Caption translation through the API on the compose stack, with the fake provider (TRX-07, 16-caption-translation
LLD, Backend endpoints): a page into a track, the idempotent resend, line fixes, the limits and the errors."""

import asyncio
import dataclasses
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import asyncpg
import pytest
from conftest import RECORDING, Sql, failure, follow, generate, put_recording, register

from snapbak_backend.ai.providers.base import ProviderError
from snapbak_backend.ai.providers.fake import Fake

Start = Callable[[str], None]
TRANSLATE = f"/v1/recordings/{RECORDING}/captions/translations"


@pytest.fixture
def ready(api: Any, start_worker: Start) -> tuple[dict[str, str], dict[str, Any]]:
    start_worker("two-speakers-en")
    auth = register(api)
    follow(api, auth, generate(api, auth))
    transcript = api.get(f"/v1/recordings/{RECORDING}/transcript", headers=auth).json()
    return auth, transcript


def page(transcript: dict[str, Any], tag: str = "h") -> list[dict[str, Any]]:
    """Every transcript line as the app sends it: its kept text and a hash of it."""
    lines = [
        {
            "idx": line["idx"],
            "lineId": f"ln_{line['id']}",
            "rev": line["rev"],
            "sourceHash": f"{tag}{line['idx']}",
            "text": line["text"],
        }
        for line in transcript["lines"]
    ]
    return lines


def post(api: Any, auth: dict[str, str], lines: list[dict[str, Any]], language: str = "es", **more: Any) -> Any:
    return api.post(TRANSLATE, json={"language": language, "lines": lines, **more}, headers=auth)


def use(api: Any, mode: str) -> None:
    api.app.state.tiers = dataclasses.replace(api.app.state.tiers, translate=Fake(mode))


def translated_lines(sql: Sql) -> list[dict[str, Any]]:
    return sql("SELECT translated_lines FROM usage_daily")


def test_trx07_post_creates_track_and_upserts_lines(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    response = post(api, auth, lines)
    body = response.json()
    assert (response.status_code, body["failed"]) == (200, [])
    track = body["track"]
    assert (track["recordingId"], track["language"], track["sourceLanguage"]) == (RECORDING, "es", "en")
    assert [line["text"] for line in track["lines"]] == [f"[es] {line['text']}" for line in lines]
    first = track["lines"][0]
    assert {key: first[key] for key in ("idx", "lineId", "sourceRev", "sourceHash", "failed", "edited")} == {
        "idx": 0,
        "lineId": lines[0]["lineId"],
        "sourceRev": 1,
        "sourceHash": "h0",
        "failed": False,
        "edited": False,
    }
    assert sql("SELECT language, source_language FROM caption_tracks") == [{"language": "es", "source_language": "en"}]
    assert translated_lines(sql) == [{"translated_lines": len(lines)}]


def test_trx07_second_post_same_language_reuses_track_without_a_model_call(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    first = post(api, auth, lines).json()
    use(api, "fail")
    again = post(api, auth, lines)
    assert (again.status_code, again.json()) == (200, first)
    assert translated_lines(sql) == [{"translated_lines": len(lines)}]


def test_trx07_a_changed_line_alone_is_translated_again(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    post(api, auth, lines)
    changed = [{**lines[0], "sourceHash": "new", "text": "Hello there.", "rev": 2}, *lines[1:]]
    track = post(api, auth, changed).json()["track"]
    assert (track["lines"][0]["text"], track["lines"][0]["sourceRev"]) == ("[es] Hello there.", 2)
    assert translated_lines(sql) == [{"translated_lines": len(lines) + 1}]


def test_trx07_force_retranslates_unchanged_line(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    post(api, auth, lines)
    post(api, auth, lines[:1], force=True)
    assert translated_lines(sql) == [{"translated_lines": len(lines) + 1}]


def test_trx07_a_dropped_line_is_retried_once_then_failed(api: Any, ready: tuple) -> None:
    auth, transcript = ready
    lines = page(transcript)
    use(api, "translate-drop")
    body = post(api, auth, lines).json()
    last = body["track"]["lines"][-1]
    assert body["failed"] == [lines[-1]["idx"]]
    assert (last["text"], last["failed"]) == (None, True)
    use(api, "echo")
    retried = post(api, auth, [lines[-1]]).json()
    assert (retried["failed"], retried["track"]["lines"][-1]["failed"]) == ([], False)


def test_trx07_a_model_that_never_answers_is_502_and_refunds_the_lines(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    use(api, "fail")
    assert failure(post(api, auth, page(transcript))) == (502, "provider")
    assert translated_lines(sql) == [{"translated_lines": 0}]
    assert sql("SELECT jsonb_array_length(lines) AS n FROM caption_tracks") == [{"n": 0}]


def test_trx07_patch_line_marks_edited_last_write_wins(api: Any, ready: tuple) -> None:
    auth, transcript = ready
    track = post(api, auth, page(transcript)).json()["track"]
    path = f"/v1/caption-tracks/{track['id']}/lines/0"
    newer = api.patch(path, json={"text": "Hola Arjun.", "rev": 2}, headers=auth)
    older = api.patch(path, json={"text": "Viejo.", "rev": 1}, headers=auth)
    tied = api.patch(path, json={"text": "Hola de nuevo.", "rev": 2}, headers=auth)
    assert (newer.status_code, newer.json()["text"], newer.json()["edited"]) == (200, "Hola Arjun.", True)
    assert older.json() == newer.json()
    assert tied.json()["text"] == "Hola de nuevo.", "an equal rev is the later write"
    stored = post(api, auth, page(transcript)).json()["track"]["lines"][0]
    assert (stored["text"], stored["edited"]) == ("Hola de nuevo.", True)


def test_trx07_an_undone_fix_puts_the_whole_line_back(api: Any, ready: tuple) -> None:
    auth, transcript = ready
    use(api, "translate-drop")
    lines = page(transcript)
    track = post(api, auth, lines).json()["track"]
    last = len(lines) - 1
    path = f"/v1/caption-tracks/{track['id']}/lines/{last}"
    api.patch(path, json={"text": "Arreglo.", "rev": 1}, headers=auth)
    undone = api.patch(path, json={"text": None, "rev": 2, "edited": False, "failed": True}, headers=auth)
    assert undone.status_code == 200
    assert (undone.json()["text"], undone.json()["edited"], undone.json()["failed"]) == (None, False, True)
    use(api, "echo")
    again = post(api, auth, lines).json()["track"]["lines"][last]
    assert (again["failed"], again["edited"]) == (False, False), "a failed line is translated again"


def test_trx07_patch_of_an_unknown_line_or_track_is_404(api: Any, ready: tuple) -> None:
    auth, transcript = ready
    track = post(api, auth, page(transcript)).json()["track"]
    line = api.patch(f"/v1/caption-tracks/{track['id']}/lines/999", json={"text": "x", "rev": 1}, headers=auth)
    unknown = api.patch(f"/v1/caption-tracks/{uuid.uuid4()}/lines/0", json={"text": "x", "rev": 1}, headers=auth)
    assert [failure(line), failure(unknown)] == [(404, "line_not_found"), (404, "track_not_found")]


def test_trx07_delete_is_soft_and_again_is_204_and_a_new_post_makes_a_new_track(
    api: Any, ready: tuple, sql: Sql
) -> None:
    auth, transcript = ready
    lines = page(transcript)
    track = post(api, auth, lines).json()["track"]
    first = api.delete(f"/v1/caption-tracks/{track['id']}", headers=auth)
    again = api.delete(f"/v1/caption-tracks/{track['id']}", headers=auth)
    patched = api.patch(f"/v1/caption-tracks/{track['id']}/lines/0", json={"text": "x", "rev": 1}, headers=auth)
    remade = post(api, auth, lines).json()["track"]
    stamped = sql("SELECT deleted_at::text AS at FROM caption_tracks WHERE id = $1", track["id"])
    api.delete(f"/v1/caption-tracks/{track['id']}", headers=auth)
    assert sql("SELECT deleted_at::text AS at FROM caption_tracks WHERE id = $1", track["id"]) == stamped
    assert (first.status_code, again.status_code, failure(patched)) == (204, 204, (404, "track_not_found"))
    assert remade["id"] != track["id"]
    assert sql("SELECT count(*) AS n FROM caption_tracks WHERE deleted_at IS NOT NULL") == [{"n": 1}]


def test_trx07_409_when_transcript_not_ready(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    line = {"idx": 0, "lineId": "ln_a", "rev": 1, "sourceHash": "h", "text": "Hi."}
    assert failure(post(api, auth, [line])) == (409, "transcript_not_ready")


def test_trx07_source_unknown_and_oversized_requests_are_refused(api: Any, ready: tuple) -> None:
    auth, transcript = ready
    lines = page(transcript)
    many = [{**lines[0], "idx": idx} for idx in range(201)]
    twice = [lines[0], lines[0]]
    answers = [
        post(api, auth, lines, "en"),
        post(api, auth, lines, "xx"),
        post(api, auth, many),
        post(api, auth, twice),
    ]
    assert [failure(answer) for answer in answers] == [
        (422, "language_unsupported"),
        (422, "language_unsupported"),
        (413, "too_many_lines"),
        (422, "invalid"),
    ]


def test_trx07_429_over_daily_line_limit(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    sql("UPDATE usage_daily SET translated_lines = 19999")
    assert failure(post(api, auth, page(transcript))) == (429, "translation_limit")
    assert sql("SELECT count(*) AS n FROM caption_tracks") == [{"n": 0}]
    assert post(api, auth, page(transcript)[:1]).status_code == 200


def test_trx07_other_devices_track_is_404(api: Any, ready: tuple) -> None:
    auth, transcript = ready
    track = post(api, auth, page(transcript)).json()["track"]
    stranger = register(api, str(uuid.uuid4()))
    posted = post(api, stranger, page(transcript))
    patched = api.patch(f"/v1/caption-tracks/{track['id']}/lines/0", json={"text": "x", "rev": 1}, headers=stranger)
    deleted = api.delete(f"/v1/caption-tracks/{track['id']}", headers=stranger)
    assert [failure(posted), failure(patched), failure(deleted)] == [
        (404, "recording_not_found"),
        (404, "track_not_found"),
        (404, "track_not_found"),
    ]


def test_trx07_a_translation_logs_no_line_text(api: Any, ready: tuple, caplog: pytest.LogCaptureFixture) -> None:
    auth, transcript = ready
    with caplog.at_level(logging.INFO, logger="snapbak_backend"):
        post(api, auth, page(transcript))
    logged = [record.getMessage() for record in caplog.records if record.name == "snapbak_backend"]
    route = [line for line in logged if "provider=fake" in line]
    assert len(route) == 1
    assert "tier=translate" in route[0]
    assert not any(line["text"] in text or "[es]" in text for line in transcript["lines"] for text in logged)


class Held:
    """Answers like the fake once released, so a test can act while the model thinks."""

    def __init__(self) -> None:
        self.asked = threading.Event()
        self.answer = threading.Event()
        self.engine = "fake:held"

    async def complete(self, turns: Any, max_tokens: int, timeout: float, reason: str = "") -> Any:
        import asyncio

        self.asked.set()
        while not self.answer.is_set():
            await asyncio.sleep(0.01)
        answer = await Fake("echo").complete(turns, max_tokens, timeout)
        return answer

    async def aclose(self) -> None:
        pass


def test_trx07_no_transaction_waits_on_the_model_and_an_edit_meanwhile_is_kept(
    api: Any, ready: tuple, sql: Sql
) -> None:
    auth, transcript = ready
    lines = page(transcript)
    track = post(api, auth, lines[:1]).json()["track"]
    model = Held()
    api.app.state.tiers = dataclasses.replace(api.app.state.tiers, translate=model)
    answers: list[Any] = []
    asking = threading.Thread(target=lambda: answers.append(post(api, auth, lines, force=True)))
    asking.start()
    assert model.asked.wait(5)
    held = sql(
        "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
        " AND state = 'idle in transaction'"
    )
    edited = api.patch(f"/v1/caption-tracks/{track['id']}/lines/0", json={"text": "Mío.", "rev": 1}, headers=auth)
    model.answer.set()
    asking.join()
    assert (held, edited.status_code) == ([{"n": 0}], 200)
    stored = answers[0].json()["track"]["lines"]
    assert (stored[0]["text"], stored[1]["text"]) == ("Mío.", f"[es] {lines[1]['text']}")
    kept = json.loads(sql("SELECT lines::text AS lines FROM caption_tracks")[0]["lines"])
    assert [line for line in kept if "inflight" in line] == [], "the answers and the edit both clear the marks"


class Gate:
    """Answers like the fake once released, counting the requests that reached the model meanwhile and keeping each
    one's turns."""

    def __init__(self, mode: str = "echo", refuses: str | None = None) -> None:
        self.mode = mode
        self.refuses = refuses
        self.asks = 0
        self.turns: list[Any] = []
        self.lock = threading.Lock()
        self.asked = threading.Event()
        self.answer = threading.Event()
        self.engine = "fake:gate"

    async def complete(self, turns: Any, max_tokens: int, timeout: float, reason: str = "") -> Any:
        import asyncio

        with self.lock:
            self.asks += 1
            self.turns.append(turns)
        self.asked.set()
        while not self.answer.is_set():
            await asyncio.sleep(0.01)
        if self.refuses is not None and self.refuses in json.dumps(turns):
            raise ProviderError("gate:refuses")
        answer = await Fake(self.mode).complete(turns, max_tokens, timeout)
        return answer

    async def aclose(self) -> None:
        pass


def test_trx07_two_requests_racing_for_the_same_language_make_one_track_with_both_pages(
    api: Any, ready: tuple, sql: Sql
) -> None:
    auth, transcript = ready
    lines = page(transcript)
    model = Gate()
    api.app.state.tiers = dataclasses.replace(api.app.state.tiers, translate=model)
    answers: dict[str, Any] = {}

    def send(name: str, part: list[dict[str, Any]]) -> None:
        answers[name] = post(api, auth, part, language="fr")

    # Two pages that share line 2, both waiting on the model at once.
    racing = [
        threading.Thread(target=send, args=("first", lines[:3])),
        threading.Thread(target=send, args=("second", lines[2:])),
    ]
    for thread in racing:
        thread.start()
    assert model.asked.wait(5)
    threading.Event().wait(1)
    model.answer.set()
    for thread in racing:
        thread.join(10)
    assert [answers[name].status_code for name in ("first", "second")] == [200, 200]
    ids = {answers[name].json()["track"]["id"] for name in ("first", "second")}
    assert len(ids) == 1
    assert sql("SELECT count(*) AS n FROM caption_tracks WHERE language = 'fr'") == [{"n": 1}]
    stored = json.loads(sql("SELECT lines::text AS lines FROM caption_tracks WHERE language = 'fr'")[0]["lines"])
    assert [(line["idx"], line["text"]) for line in stored] == [
        (line["idx"], f"[fr] {line['text']}") for line in lines
    ], "neither page's answers are lost"


def test_trx07_a_resend_racing_a_finished_track_costs_nothing(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    first = post(api, auth, lines, language="de").json()
    use(api, "fail")
    answers: list[Any] = []
    racing = [threading.Thread(target=lambda: answers.append(post(api, auth, lines, language="de"))) for _ in range(2)]
    for thread in racing:
        thread.start()
    for thread in racing:
        thread.join(10)
    assert [(answer.status_code, answer.json()) for answer in answers] == [(200, first), (200, first)]
    assert translated_lines(sql) == [{"translated_lines": len(lines)}]


def asked_idx(model: Gate) -> list[int]:
    """The idx of every line the model was asked to translate, in the order asked."""
    requests = [json.loads(turns[-1][1]) for turns in model.turns]
    return [line["idx"] for request in requests for line in request["translate"]]


def test_trx07_two_first_time_requests_for_the_same_lines_ask_and_charge_once(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    model = Gate()
    api.app.state.tiers = dataclasses.replace(api.app.state.tiers, translate=model)
    answers: list[Any] = []
    racing = [threading.Thread(target=lambda: answers.append(post(api, auth, lines, language="it"))) for _ in range(2)]
    for thread in racing:
        thread.start()
    assert model.asked.wait(5)
    threading.Event().wait(1)
    model.answer.set()
    for thread in racing:
        thread.join(10)
    expected = [(line["idx"], f"[it] {line['text']}") for line in lines]
    assert asked_idx(model) == [line["idx"] for line in lines]
    assert translated_lines(sql) == [{"translated_lines": len(lines)}]
    assert [answer.status_code for answer in answers] == [200, 200]
    for answer in answers:
        body = answer.json()
        assert body["failed"] == []
        assert [(line["idx"], line["text"]) for line in body["track"]["lines"]] == expected
        assert all("inflight" not in line for line in body["track"]["lines"])


def test_trx07_a_line_another_request_failed_is_failed_for_the_waiter_too(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    model = Gate("translate-drop")
    api.app.state.tiers = dataclasses.replace(api.app.state.tiers, translate=model)
    answers: list[Any] = []
    racing = [threading.Thread(target=lambda: answers.append(post(api, auth, lines, language="fr"))) for _ in range(2)]
    for thread in racing:
        thread.start()
    assert model.asked.wait(5)
    threading.Event().wait(1)
    model.answer.set()
    for thread in racing:
        thread.join(10)
    last = lines[-1]["idx"]
    assert [answer.status_code for answer in answers] == [200, 200]
    assert [answer.json()["failed"] for answer in answers] == [[last], [last]], "the waiter lists it too"
    assert translated_lines(sql) == [{"translated_lines": len(lines)}]


def test_trx07_a_live_mark_for_another_text_is_not_waited_on(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    first = post(api, auth, lines).json()["track"]["lines"]
    other = {"hash": "older-text", "until": int(time.time()) + 3}
    sql("UPDATE caption_tracks SET lines = $1::jsonb", json.dumps([{**first[0], "inflight": other}, *first[1:]]))
    changed = {**lines[0], "sourceHash": "new", "text": "Hello there."}
    started = time.monotonic()
    body = post(api, auth, [changed]).json()
    assert body["failed"] == []
    assert body["track"]["lines"][0]["text"] == "[es] Hello there.", "asked the model, not the old mark"
    assert time.monotonic() - started < 2, "no wait on a mark for other text"


def test_trx07_a_waiter_whose_owner_gets_a_502_lists_the_lines_failed(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    model = Gate("fail")
    api.app.state.tiers = dataclasses.replace(api.app.state.tiers, translate=model)
    answers: dict[str, Any] = {}

    def send(name: str) -> None:
        answers[name] = post(api, auth, lines, language="sv")

    owner = threading.Thread(target=send, args=("owner",))
    owner.start()
    assert model.asked.wait(5)
    waiter = threading.Thread(target=send, args=("waiter",))
    waiter.start()
    threading.Event().wait(1)
    model.answer.set()
    owner.join(10)
    waiter.join(10)
    assert failure(answers["owner"]) == (502, "provider")
    assert answers["waiter"].status_code == 200
    assert answers["waiter"].json()["failed"] == [line["idx"] for line in lines]


def test_trx07_a_502_page_leaves_another_requests_marks_alone(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    model = Gate(refuses=lines[0]["text"])
    api.app.state.tiers = dataclasses.replace(api.app.state.tiers, translate=model)
    answers: dict[str, Any] = {}

    def send(name: str, work: list[dict[str, Any]]) -> None:
        answers[name] = post(api, auth, work, language="da")

    first = threading.Thread(target=send, args=("first", lines[:1]))
    first.start()
    assert model.asked.wait(5)
    second = threading.Thread(target=send, args=("second", lines[1:2]))
    second.start()
    deadline = time.monotonic() + 5
    while model.asks < 2 and time.monotonic() < deadline:
        threading.Event().wait(0.02)
    model.answer.set()
    first.join(10)
    second.join(10)
    assert failure(answers["first"]) == (502, "provider")
    kept = answers["second"].json()["track"]["lines"]
    assert [(line["idx"], line["text"]) for line in kept] == [(lines[1]["idx"], f"[da] {lines[1]['text']}")]


def test_trx07_a_mark_left_by_a_crashed_request_is_ignored(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    first = post(api, auth, lines).json()["track"]["lines"]
    gone = {"hash": "new", "until": int(time.time()) - 1}
    crashed = [{**first[0], "inflight": gone}, {"idx": first[1]["idx"], "inflight": {**gone, "hash": "h1"}}, *first[2:]]
    sql("UPDATE caption_tracks SET lines = $1::jsonb", json.dumps(crashed))
    changed = [{**lines[0], "sourceHash": "new", "text": "Hello there."}, *lines[1:]]
    body = post(api, auth, changed).json()
    assert body["failed"] == []
    assert [line["text"] for line in body["track"]["lines"][:2]] == ["[es] Hello there.", f"[es] {lines[1]['text']}"]
    assert translated_lines(sql) == [{"translated_lines": len(lines) + 2}]
    kept = json.loads(sql("SELECT lines::text AS lines FROM caption_tracks")[0]["lines"])
    assert [line for line in kept if "inflight" in line] == []


def test_trx07_a_request_waiting_on_lines_in_flight_is_404_once_the_track_is_deleted(
    api: Any, ready: tuple, sql: Sql
) -> None:
    auth, transcript = ready
    lines = page(transcript)
    model = Gate()
    api.app.state.tiers = dataclasses.replace(api.app.state.tiers, translate=model)
    answers: dict[str, Any] = {}

    def send(name: str) -> None:
        answers[name] = post(api, auth, lines, language="nl")

    asking = threading.Thread(target=send, args=("asking",))
    asking.start()
    assert model.asked.wait(5)
    waiting = threading.Thread(target=send, args=("waiting",))
    waiting.start()
    threading.Event().wait(1)
    track = sql("SELECT id::text AS id FROM caption_tracks WHERE language = 'nl'")[0]["id"]
    assert api.delete(f"/v1/caption-tracks/{track}", headers=auth).status_code == 204
    waiting.join(5)
    model.answer.set()
    asking.join(10)
    assert [failure(answers[name]) for name in ("waiting", "asking")] == [(404, "track_not_found")] * 2
    assert model.asks == 1


def test_trx07_a_page_of_200_lines_is_accepted(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    full = [{**lines[0], "idx": idx, "sourceHash": f"h{idx}"} for idx in range(200)]
    response = post(api, auth, full)
    assert response.status_code == 200
    assert (response.json()["failed"], len(response.json()["track"]["lines"])) == ([], 200)
    assert translated_lines(sql) == [{"translated_lines": 200}]


def test_trx07_a_fixed_failed_line_is_not_failed_and_a_resend_keeps_the_fix(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    use(api, "translate-drop")
    track = post(api, auth, lines).json()["track"]
    last = lines[-1]["idx"]
    use(api, "echo")
    fixed = api.patch(
        f"/v1/caption-tracks/{track['id']}/lines/{last}", json={"text": "Arreglado.", "rev": 1}, headers=auth
    )
    assert (fixed.json()["text"], fixed.json()["failed"], fixed.json()["edited"]) == ("Arreglado.", False, True)
    again = post(api, auth, lines).json()
    stored = again["track"]["lines"][-1]
    assert (again["failed"], stored["text"], stored["failed"]) == ([], "Arreglado.", False)
    assert translated_lines(sql) == [{"translated_lines": len(lines)}], "the resend asks the model for nothing"


def test_trx07_a_hinglish_transcripts_prompt_names_hindi_and_english(api: Any, start_worker: Start) -> None:
    start_worker("hinglish")
    auth = register(api)
    follow(api, auth, generate(api, auth, language="hinglish"))
    transcript = api.get(f"/v1/recordings/{RECORDING}/transcript", headers=auth).json()
    model = Gate()
    model.answer.set()
    api.app.state.tiers = dataclasses.replace(api.app.state.tiers, translate=model)
    assert post(api, auth, page(transcript)).status_code == 200
    system = model.turns[0][0][1]
    assert system.startswith("You translate video captions from Hindi and English to Spanish.")


def test_cld04_a_trashed_recordings_tracks_are_gone(api: Any, ready: tuple) -> None:
    auth, transcript = ready
    lines = page(transcript)
    track = post(api, auth, lines).json()["track"]
    assert api.delete(f"/v1/recordings/{RECORDING}", headers=auth).status_code == 204
    posted = post(api, auth, lines)
    patched = api.patch(f"/v1/caption-tracks/{track['id']}/lines/0", json={"text": "x", "rev": 1}, headers=auth)
    deleted = api.delete(f"/v1/caption-tracks/{track['id']}", headers=auth)
    assert [failure(posted), failure(patched), failure(deleted)] == [
        (404, "recording_not_found"),
        (404, "track_not_found"),
        (404, "track_not_found"),
    ]


def test_trx07_a_502_page_takes_back_its_marks_and_leaves_the_track_as_it_was(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    post(api, auth, lines)
    before = sql("SELECT lines::text AS lines FROM caption_tracks")
    use(api, "fail")
    assert failure(post(api, auth, lines, force=True)) == (502, "provider")
    assert sql("SELECT lines::text AS lines FROM caption_tracks") == before
    assert translated_lines(sql) == [{"translated_lines": len(lines)}]


def test_trx07_another_requests_placeholder_is_never_sent_or_edited(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    track = post(api, auth, lines).json()["track"]
    stored = json.loads(sql("SELECT lines::text AS lines FROM caption_tracks")[0]["lines"])
    flying = {"idx": 99, "inflight": {"hash": "x", "until": 4_000_000_000}}
    sql("UPDATE caption_tracks SET lines = $1::jsonb", json.dumps([*stored, flying]))
    again = post(api, auth, lines)
    patched = api.patch(f"/v1/caption-tracks/{track['id']}/lines/99", json={"text": "x", "rev": 1}, headers=auth)
    assert (again.status_code, again.json()) == (200, {"track": track, "failed": []})
    assert failure(patched) == (404, "line_not_found")


def test_trx07_a_transcript_job_that_is_not_ready_is_409(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    sql("UPDATE transcription_jobs SET state = 'processing'")
    assert failure(post(api, auth, page(transcript))) == (409, "transcript_not_ready")


def test_trx07_a_502_page_refunds_only_the_lines_it_reserved(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    post(api, auth, lines)
    changed = [{**lines[0], "sourceHash": "new"}, *lines[1:]]
    use(api, "fail")
    assert failure(post(api, auth, changed)) == (502, "provider")
    assert translated_lines(sql) == [{"translated_lines": len(lines)}], "one line reserved, one refunded"


def test_trx07_a_fix_is_stamped_with_its_time_and_a_page_moves_the_track(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)
    track = post(api, auth, lines).json()["track"]
    moved = sql("SELECT updated_at::text AS at FROM caption_tracks")
    post(api, auth, [{**lines[0], "sourceHash": "new"}, *lines[1:]])
    assert sql("SELECT updated_at::text AS at FROM caption_tracks") != moved, "a page moves the track's time"
    stored = json.loads(sql("SELECT lines::text AS lines FROM caption_tracks")[0]["lines"])
    old = [{**stored[0], "updatedAt": "2000-01-01T00:00:00Z"}, *stored[1:]]
    sql("UPDATE caption_tracks SET lines = $1::jsonb", json.dumps(old))
    fixed = api.patch(f"/v1/caption-tracks/{track['id']}/lines/0", json={"text": "Hola.", "rev": 1}, headers=auth)
    assert fixed.json()["updatedAt"] != "2000-01-01T00:00:00Z"


def test_trx07_a_waiter_waits_for_the_last_mark_to_run_out(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)[:2]
    model = Gate()
    api.app.state.tiers = dataclasses.replace(api.app.state.tiers, translate=model)
    answers: dict[str, Any] = {}

    def send(name: str) -> None:
        answers[name] = post(api, auth, lines, language="nb")

    owner = threading.Thread(target=send, args=("owner",))
    owner.start()
    assert model.asked.wait(5)
    marked = json.loads(sql("SELECT lines::text AS lines FROM caption_tracks WHERE language = 'nb'")[0]["lines"])
    soon = int(time.time())
    for line, after in zip(marked, (2, 5), strict=True):
        line["inflight"]["until"] = soon + after
    sql("UPDATE caption_tracks SET lines = $1::jsonb WHERE language = 'nb'", json.dumps(marked))
    started = time.monotonic()
    send("waiter")
    waited = time.monotonic() - started
    model.answer.set()
    owner.join(10)
    assert answers["waiter"].status_code == 200
    assert waited >= 3.5, f"waited {waited:.1f} s for the later mark, not the earlier"


def test_trx07_a_fix_that_waited_for_the_lock_keeps_what_was_written_meanwhile(
    api: Any, ready: tuple, sql: Sql, cloud: dict[str, str]
) -> None:
    auth, transcript = ready
    lines = page(transcript)
    track = post(api, auth, lines).json()["track"]
    stored = json.loads(sql("SELECT lines::text AS lines FROM caption_tracks")[0]["lines"])
    written = [stored[0], {**stored[1], "text": "Escrito mientras tanto."}, *stored[2:]]
    locked, release = threading.Event(), threading.Event()

    def hold() -> None:
        async def run() -> None:
            connection = await asyncpg.connect(cloud["DATABASE_URL"].replace("+asyncpg", ""))
            async with connection.transaction():
                await connection.execute("SELECT 1 FROM recordings WHERE id = $1 FOR UPDATE", uuid.UUID(RECORDING))
                locked.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
                await connection.execute("UPDATE caption_tracks SET lines = $1::jsonb", json.dumps(written))
            await connection.close()

        asyncio.run(run())

    holder = threading.Thread(target=hold)
    holder.start()
    assert locked.wait(5)
    fixes: list[Any] = []
    fixing = threading.Thread(
        target=lambda: fixes.append(
            api.patch(f"/v1/caption-tracks/{track['id']}/lines/0", json={"text": "Hola.", "rev": 1}, headers=auth)
        )
    )
    fixing.start()
    threading.Event().wait(1)
    release.set()
    holder.join(10)
    fixing.join(10)
    assert fixes[0].status_code == 200
    after = json.loads(sql("SELECT lines::text AS lines FROM caption_tracks")[0]["lines"])
    assert (after[0]["text"], after[1]["text"]) == ("Hola.", "Escrito mientras tanto.")


def test_trx07_a_mark_lasts_65_seconds_and_a_fix_moves_the_track(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    lines = page(transcript)[:1]
    model = Gate()
    api.app.state.tiers = dataclasses.replace(api.app.state.tiers, translate=model)
    owner = threading.Thread(target=lambda: post(api, auth, lines, language="fi"))
    owner.start()
    assert model.asked.wait(5)
    marked = json.loads(sql("SELECT lines::text AS lines FROM caption_tracks WHERE language = 'fi'")[0]["lines"])
    assert 64 <= marked[0]["inflight"]["until"] - int(time.time()) <= 65, "IN_FLIGHT_S is 65 seconds"
    model.answer.set()
    owner.join(10)
    track = sql("SELECT id::text AS id FROM caption_tracks WHERE language = 'fi'")[0]["id"]
    before = sql("SELECT updated_at::text AS at FROM caption_tracks WHERE language = 'fi'")
    api.patch(f"/v1/caption-tracks/{track}/lines/{lines[0]['idx']}", json={"text": "Hei.", "rev": 1}, headers=auth)
    assert sql("SELECT updated_at::text AS at FROM caption_tracks WHERE language = 'fi'") != before


def test_trx07_the_lines_around_the_work_reach_the_model_as_context(api: Any, ready: tuple) -> None:
    auth, transcript = ready
    lines = page(transcript)
    post(api, auth, lines)
    model = Gate()
    model.answer.set()
    api.app.state.tiers = dataclasses.replace(api.app.state.tiers, translate=model)
    changed = [lines[0], {**lines[1], "sourceHash": "new"}, *lines[2:]]
    post(api, auth, changed)
    sent = json.dumps(model.turns)
    assert lines[0]["text"] in sent and lines[2]["text"] in sent, "the neighbours are the context"


def test_trx07_only_a_live_speakers_name_is_kept_as_a_term(api: Any, ready: tuple, sql: Sql) -> None:
    auth, transcript = ready
    sql("UPDATE speakers SET name = 'Quinn' WHERE deleted_at IS NULL")
    sql(
        "UPDATE speakers SET name = 'Zephyr', deleted_at = now()"
        " WHERE id = (SELECT id FROM speakers ORDER BY id LIMIT 1)"
    )
    model = Gate()
    model.answer.set()
    api.app.state.tiers = dataclasses.replace(api.app.state.tiers, translate=model)
    post(api, auth, page(transcript), language="ko")
    sent = json.dumps(model.turns)
    assert "Quinn" in sent and "Zephyr" not in sent
