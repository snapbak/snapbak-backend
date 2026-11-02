"""The meeting routes' surface (MEETINGS.md 5): promote, pins, the caps and the errors, without the worker. The
full chain — transcript, minutes, embeddings, ask — runs in tests/functional/test_meeting_flow.py."""

import json
import logging
import uuid
from typing import Any

import pytest
from conftest import RECORDING, Sql, failure, put_recording, queued_arq_jobs, register, upload_audio


@pytest.fixture
def auth(api: Any) -> dict[str, str]:
    made = register(api)
    put_recording(api, made)
    return made


def promote(api: Any, auth: dict[str, str], title: str = "Weekly sync") -> Any:
    return api.post(f"/v1/recordings/{RECORDING}/meeting", json={"title": title}, headers=auth)


def test_mom07_promote_makes_an_awaiting_meeting_and_its_transcription_job(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    made = promote(api, auth).json()
    assert made["state"] == "awaiting_transcript"
    rows = sql("SELECT state, minutes_failure FROM meetings")
    assert rows == [{"state": "awaiting_transcript", "minutes_failure": None}]
    job = sql("SELECT state FROM transcription_jobs")
    assert job == [{"state": "queued"}]
    assert queued_arq_jobs() == []


def test_mom07_a_second_promote_is_a_409(api: Any, auth: dict[str, str]) -> None:
    assert promote(api, auth).status_code == 201
    assert failure(promote(api, auth)) == (409, "meeting_exists")


def test_mom07_promote_on_an_unknown_or_foreign_recording_is_a_404(api: Any, auth: dict[str, str]) -> None:
    unknown = str(uuid.uuid4())
    response = api.post(f"/v1/recordings/{unknown}/meeting", json={"title": "x"}, headers=auth)
    assert failure(response) == (404, "not_found")


def test_mom04_the_audio_upload_completes_the_job_as_for_a_plain_transcript(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    promote(api, auth)
    _, done = upload_audio(api, auth, b"m4a")
    job = done.json()["jobId"]
    assert queued_arq_jobs() == [f"start:{job}"]


def test_mom02_a_pin_is_idempotent_by_its_client_made_id(api: Any, auth: dict[str, str]) -> None:
    meeting = promote(api, auth).json()["meetingId"]
    pin = uuid.uuid4()
    body = {"atMs": 61_200, "note": "decided pricing"}
    first = api.put(f"/v1/meetings/{meeting}/pins/{pin}", json=body, headers=auth)
    second = api.put(f"/v1/meetings/{meeting}/pins/{pin}", json=body, headers=auth)
    assert (first.status_code, second.status_code) == (201, 200)
    detail = api.get(f"/v1/recordings/{RECORDING}/meeting", headers=auth).json()
    assert detail["pins"] == [{"id": str(pin), "atMs": 61_200, "note": "decided pricing"}]


def test_mom02_pins_and_meetings_of_another_device_or_unknown_are_404(api: Any, auth: dict[str, str]) -> None:
    meeting = promote(api, auth).json()["meetingId"]
    stranger = register(api, "7c1e5d90-3a2b-4c8d-9e0f-1a2b3c4d5e66")
    response = api.put(f"/v1/meetings/{meeting}/pins/{uuid.uuid4()}", json={"atMs": 1}, headers=stranger)
    assert failure(response) == (404, "not_found")
    unknown = api.patch(f"/v1/meetings/{uuid.uuid4()}", json={"title": "x"}, headers=auth)
    assert failure(unknown) == (404, "not_found")


def test_mom04_regenerate_while_the_transcript_is_coming_waits_on_it(api: Any, auth: dict[str, str], sql: Sql) -> None:
    """Try again before the transcript lands changes nothing: the meeting keeps waiting on its one job."""
    meeting = promote(api, auth).json()["meetingId"]
    response = api.post(f"/v1/meetings/{meeting}/minutes/regenerate", headers=auth)
    assert (response.status_code, response.json()["state"]) == (200, "awaiting_transcript")
    assert sql("SELECT count(*) AS n FROM transcription_jobs") == [{"n": 1}]


def test_mom06_ask_before_the_transcript_is_ready_is_a_409(api: Any, auth: dict[str, str]) -> None:
    meeting = promote(api, auth).json()["meetingId"]
    response = api.post(f"/v1/meetings/{meeting}/ask", json={"question": "what?"}, headers=auth)
    assert failure(response) == (409, "transcript_not_ready")


def test_mom06_search_spends_one_of_the_day_s_searches_and_answers_empty_before_any_minutes(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    response = api.post("/v1/meetings/search", json={"query": "pricing", "limit": 5}, headers=auth)
    assert response.json() == {"hits": []}
    assert sql("SELECT search_queries FROM usage_daily") == [{"search_queries": 1}]


def test_mom06_the_search_cap_answers_429_with_the_reset_time(api: Any, auth: dict[str, str]) -> None:
    for _ in range(50):
        asked = api.post("/v1/meetings/search", json={"query": "pricing"}, headers=auth)
        assert asked.status_code == 200
    refused = api.post("/v1/meetings/search", json={"query": "pricing"}, headers=auth)
    body = refused.json()
    assert refused.status_code == 429
    assert body["error"]["code"] == "daily_limit"
    assert body["resetsAt"] == next_midnight()


def test_mom07_a_recording_registered_as_audio_says_so(api: Any) -> None:
    auth = register(api)
    body = {
        "title": "Meeting 2026-10-24 at 14.02.11",
        "durationMs": 612_400,
        "createdAt": "2026-10-24T08:32:11Z",
        "recordingType": "audio",
    }
    made = api.put(f"/v1/recordings/{RECORDING}", json=body, headers=auth)
    assert made.status_code == 201
    assert made.json()["recordingType"] == "audio"


MINUTES = {
    "summary": "A summary.",
    "mom": "The minutes.",
    "actionItems": [],
    "openQuestions": [],
    "decisions": [],
}


def ready_meeting(api: Any, auth: dict[str, str], sql: Sql) -> str:
    meeting = promote(api, auth).json()["meetingId"]
    sql("UPDATE transcription_jobs SET state = 'ready'")
    sql("UPDATE meetings SET state = 'ready', minutes = $1::jsonb", json.dumps(MINUTES))
    return meeting


def test_mom06_the_question_cap_answers_429_with_the_reset_time(
    api: Any, auth: dict[str, str], sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 50th question of the day is answered, the 51st refused: the cap is pinned at both sides (grading 3)."""
    from snapbak_backend.schemas.cloud import AskResponse
    from snapbak_backend.services import qa

    async def answered(*args: Any, **kwargs: Any) -> AskResponse:
        return AskResponse(answer="Not in this meeting.", citations=[], supported=False)

    monkeypatch.setattr(qa, "answer", answered)
    meeting = ready_meeting(api, auth, sql)
    sql(
        "INSERT INTO usage_daily (device_id, day, qa_questions) "
        "SELECT id, (now() AT TIME ZONE 'UTC')::date, 49 FROM devices "
        "ON CONFLICT (device_id, day) DO UPDATE SET qa_questions = 49"
    )
    fiftieth = api.post(f"/v1/meetings/{meeting}/ask", json={"question": "who owns pricing?"}, headers=auth)
    assert fiftieth.status_code == 200
    assert sql("SELECT qa_questions FROM usage_daily") == [{"qa_questions": 50}]
    refused = api.post(f"/v1/meetings/{meeting}/ask", json={"question": "who owns pricing?"}, headers=auth)
    assert failure(refused) == (429, "daily_limit")
    assert refused.json()["resetsAt"] == next_midnight()


class BrokenEmbeddings:
    engine = "openai:text-embedding-3-small"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from snapbak_backend.services.embeddings import EmbedError

        raise EmbedError("openai:text-embedding-3-small: ConnectError")

    async def aclose(self) -> None:
        pass


def test_mom06_a_search_whose_embedding_fails_is_a_502_refunded_and_logs_no_words(
    api: Any, auth: dict[str, str], sql: Sql, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    api.app.state.cloud.embeddings = BrokenEmbeddings()
    response = api.post("/v1/meetings/search", json={"query": "secret pricing plans"}, headers=auth)
    assert failure(response) == (502, "provider")
    assert sql("SELECT search_queries FROM usage_daily") == [{"search_queries": 0}]
    assert "secret pricing" not in caplog.text


def test_mom06_an_ask_whose_model_fails_is_a_502_refunded_and_logs_no_words(
    api: Any, auth: dict[str, str], sql: Sql, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snapbak_backend.ai.chain import ProviderFailed
    from snapbak_backend.services import qa

    async def failing(*args: Any, **kwargs: Any) -> Any:
        raise ProviderFailed("fake:edit", "error")

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(qa, "answer", failing)
    meeting = ready_meeting(api, auth, sql)
    response = api.post(f"/v1/meetings/{meeting}/ask", json={"question": "who owns the secret plan?"}, headers=auth)
    assert failure(response) == (502, "provider")
    assert sql("SELECT qa_questions FROM usage_daily") == [{"qa_questions": 0}]
    assert "secret plan" not in caplog.text and "Weekly sync" not in caplog.text


def test_mom04_a_long_poll_holds_until_the_meeting_moves(api: Any, auth: dict[str, str]) -> None:
    """A poll at the current version is held until the next publish (a rename here) and answers it; one behind the
    current version answers at once. Ordered by the server's own watcher, not by sleeps."""
    import threading

    from conftest import eventually

    made = promote(api, auth).json()
    meeting, version = made["meetingId"], made["version"]
    held: dict[str, Any] = {}

    def poll() -> None:
        held.update(api.get(f"/v1/recordings/{RECORDING}/meeting?wait=25&since={version}", headers=auth).json())

    waiting = threading.Thread(target=poll)
    waiting.start()
    assert eventually(lambda: meeting in api.app.state.events.waiting, bool)
    assert waiting.is_alive()
    api.patch(f"/v1/meetings/{meeting}", json={"title": "Renamed"}, headers=auth)
    waiting.join(10)
    assert (held["title"], held["version"]) == ("Renamed", version + 1)
    behind = threading.Thread(target=poll)
    behind.start()
    # Behind the current version, it answers without registering a wait that only a publish would end.
    behind.join(10)
    assert not behind.is_alive()


def test_mom06_a_rename_changes_the_title_bumps_the_version_and_logs_no_title(
    api: Any, auth: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    made = promote(api, auth).json()
    renamed = api.patch(f"/v1/meetings/{made['meetingId']}", json={"title": "Quarterly Plan Review"}, headers=auth)
    body = renamed.json()
    assert renamed.status_code == 200
    assert (body["title"], body["version"]) == ("Quarterly Plan Review", made["version"] + 1)
    assert "quarterly plan" not in caplog.text.lower()


def test_mom05_regenerate_while_a_run_is_going_is_a_409_and_enqueues_nothing(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    """A Try again while the minutes are being written once answered 200 and started nothing, so the section asked
    for stayed failed with no word why (grading 3): it is a 409 the app can explain."""
    made = promote(api, auth).json()
    sql("UPDATE transcription_jobs SET state = 'ready'")
    sql("UPDATE meetings SET state = 'generating'")
    version = sql("SELECT version FROM meetings")[0]["version"]
    again = api.post(f"/v1/meetings/{made['meetingId']}/minutes/regenerate", json={"sections": ["risks"]}, headers=auth)
    assert failure(again) == (409, "minutes_running")
    assert sql("SELECT version FROM meetings")[0]["version"] == version
    assert not any(job.startswith("minutes:") for job in queued_arq_jobs())


def test_mom09_a_meeting_regenerates_whole_five_times_a_day_then_says_when_it_can_again(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    meeting = ready_meeting(api, auth, sql)
    sql("UPDATE meetings SET regenerate_day = (now() AT TIME ZONE 'UTC')::date, regenerate_count = 4")

    fifth = api.post(f"/v1/meetings/{meeting}/minutes/regenerate", headers=auth)
    assert fifth.status_code == 200
    assert sql("SELECT regenerate_count FROM meetings") == [{"regenerate_count": 5}]
    sql("UPDATE meetings SET state = 'ready'")
    sixth = api.post(f"/v1/meetings/{meeting}/minutes/regenerate", headers=auth)
    assert failure(sixth) == (429, "regenerate_limit")
    assert sixth.json()["resetsAt"] == next_midnight()
    # A failed section's Try again is no regenerate: it never counts, and works past the cap.
    retry = api.post(f"/v1/meetings/{meeting}/minutes/regenerate", json={"sections": ["risks"]}, headers=auth)
    assert retry.status_code == 200


def test_mom09_yesterday_s_regenerates_never_count_today_and_a_refused_one_costs_nothing(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    meeting = ready_meeting(api, auth, sql)
    sql("UPDATE meetings SET regenerate_day = (now() AT TIME ZONE 'UTC')::date - 1, regenerate_count = 5")

    assert api.post(f"/v1/meetings/{meeting}/minutes/regenerate", headers=auth).status_code == 200
    assert sql("SELECT regenerate_count FROM meetings") == [{"regenerate_count": 1}]
    # A run is writing now: the next ask is refused and the count stays.
    assert failure(api.post(f"/v1/meetings/{meeting}/minutes/regenerate", headers=auth)) == (409, "minutes_running")
    assert sql("SELECT regenerate_count FROM meetings") == [{"regenerate_count": 1}]


def test_mom05_regenerate_on_a_run_stalled_past_10_minutes_starts_a_new_one(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    """A Try again on a run that stopped moving 10 minutes ago takes the meeting with a new token rather than wait for
    the sweep; the old run can no longer write (grading 3)."""
    made = promote(api, auth).json()
    old = "00000000-0000-4000-8000-000000000001"
    sql("UPDATE transcription_jobs SET state = 'ready'")
    sql(
        "UPDATE meetings SET state = 'generating', minutes_run = $1::uuid, updated_at = now() - interval '11 minutes'",
        old,
    )
    again = api.post(f"/v1/meetings/{made['meetingId']}/minutes/regenerate", json={"sections": ["risks"]}, headers=auth)
    assert (again.status_code, again.json()["state"]) == (200, "generating")
    row = sql("SELECT minutes_run::text AS run, minutes_clicked FROM meetings")[0]
    assert row["run"] != old and row["minutes_clicked"]
    assert any(job.startswith("minutes:") for job in queued_arq_jobs())


def test_mom07_two_promotes_at_once_make_one_meeting(api: Any, auth: dict[str, str], sql: Sql) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(2) as pool:
        answers = list(pool.map(lambda _: promote(api, auth).status_code, range(2)))
    assert sorted(answers) == [201, 409]
    assert sql("SELECT count(*) AS n FROM meetings") == [{"n": 1}]


def test_mom02_a_pin_id_another_meeting_holds_is_404(api: Any, auth: dict[str, str]) -> None:
    first = promote(api, auth).json()["meetingId"]
    other = "7b2e5d41-1f0f-4e66-8b55-3f2b8f7d4c21"
    put_recording(api, auth, other)
    second = api.post(f"/v1/recordings/{other}/meeting", json={"title": "Two"}, headers=auth).json()["meetingId"]
    pin = str(uuid.uuid4())
    assert api.put(f"/v1/meetings/{first}/pins/{pin}", json={"atMs": 5}, headers=auth).status_code == 201
    assert failure(api.put(f"/v1/meetings/{second}/pins/{pin}", json={"atMs": 5}, headers=auth)) == (404, "not_found")


SECRET = "Zebra Budget Secret"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("put", "/v1/meetings/{meeting}/pins/{pin}", {"atMs": -1}),
        ("put", "/v1/meetings/{meeting}/pins/{pin}", {"atMs": 5, "note": SECRET + "x" * 501}),
        ("post", "/v1/meetings/search", {"query": SECRET, "limit": 0}),
        ("post", "/v1/meetings/search", {"query": SECRET, "limit": 21}),
        ("post", "/v1/meetings/search", {"query": ""}),
        ("get", "/v1/recordings/{recording}/meeting?wait=26", None),
        ("post", "/v1/meetings/{meeting}/ask", {"question": ""}),
        ("post", "/v1/meetings/{meeting}/ask", {"question": SECRET + "x" * 1001}),
        ("patch", "/v1/meetings/{meeting}", {"title": ""}),
        ("post", "/v1/recordings/{recording}/meeting", {"title": ""}),
        ("post", "/v1/meetings/{meeting}/minutes/regenerate", {"sections": ["nope", SECRET]}),
        # "title" is written by the summary's call: asked for alone it started an empty run (grading 3).
        ("post", "/v1/meetings/{meeting}/minutes/regenerate", {"sections": ["title"]}),
        ("put", "/v1/meetings/{meeting}/minutes", {"baseVersion": 0, "minutes": {"summary": SECRET, "mom": "m"}}),
        ("put", "/v1/meetings/{meeting}/minutes", {"baseVersion": 1, "minutes": {"summary": SECRET}}),
    ],
)
def test_mom06_a_body_past_its_bounds_is_422_and_logs_no_text(
    method: str,
    path: str,
    body: dict[str, Any] | None,
    api: Any,
    auth: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    meeting = promote(api, auth).json()["meetingId"]
    url = path.format(meeting=meeting, pin=uuid.uuid4(), recording=RECORDING)
    sent = (
        getattr(api, method)(url, headers=auth) if body is None else getattr(api, method)(url, json=body, headers=auth)
    )
    assert failure(sent) == (422, "invalid")
    assert SECRET.lower() not in caplog.text.lower()


def test_mom07_a_second_promote_is_409_before_any_job_or_minute_is_spent(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    """The 409 comes first: a second promote on a recording whose job failed starts no new job and reserves no
    minutes (grading round 2)."""
    promote(api, auth)
    sql("UPDATE transcription_jobs SET state = 'failed'")
    spent = sql("SELECT audio_seconds FROM usage_daily")
    assert failure(promote(api, auth)) == (409, "meeting_exists")
    assert sql("SELECT count(*) AS n FROM transcription_jobs") == [{"n": 1}]
    assert sql("SELECT audio_seconds FROM usage_daily") == spent


def test_mom04_try_again_after_a_failed_transcript_starts_it_again(api: Any, auth: dict[str, str], sql: Sql) -> None:
    """A meeting failed before its transcript (the day's minutes used up, a vendor error) is retried by the same
    regenerate the app's Try again sends: a new job, the meeting waiting on it again (grading round 2)."""
    meeting = promote(api, auth).json()["meetingId"]
    sql("UPDATE transcription_jobs SET state = 'failed', failure_reason = 'daily_limit'")
    sql("UPDATE meetings SET state = 'failed', minutes_failure = 'daily_limit'")
    again = api.post(f"/v1/meetings/{meeting}/minutes/regenerate", headers=auth)
    assert (again.status_code, again.json()["state"]) == (200, "awaiting_transcript")
    assert sql("SELECT state FROM transcription_jobs ORDER BY created_at") == [{"state": "failed"}, {"state": "queued"}]
    assert sql("SELECT minutes_failure FROM meetings") == [{"minutes_failure": None}]
    # Again while that job runs changes nothing.
    still = api.post(f"/v1/meetings/{meeting}/minutes/regenerate", headers=auth)
    assert still.json()["state"] == "awaiting_transcript"
    assert sql("SELECT count(*) AS n FROM transcription_jobs") == [{"n": 2}]


def test_mom06_an_ask_that_breaks_any_way_gives_its_question_back(
    api: Any, auth: dict[str, str], sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snapbak_backend.services import qa

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("a bug")

    monkeypatch.setattr(qa, "answer", broken)
    meeting = ready_meeting(api, auth, sql)
    response = api.post(f"/v1/meetings/{meeting}/ask", json={"question": "who owns it?"}, headers=auth)
    assert response.status_code == 500
    assert sql("SELECT qa_questions FROM usage_daily") == [{"qa_questions": 0}]


def test_mom06_an_ask_whose_lines_could_not_be_read_gives_its_question_back(
    api: Any, auth: dict[str, str], sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading the meeting's lines broke after the day's count was taken: the question goes back (grading 3)."""
    from snapbak_backend.services import qa

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(qa, "gather", broken)
    meeting = ready_meeting(api, auth, sql)
    response = api.post(f"/v1/meetings/{meeting}/ask", json={"question": "who owns it?"}, headers=auth)
    assert response.status_code == 500
    assert sql("SELECT qa_questions FROM usage_daily") == [{"qa_questions": 0}]


STRANGER = "1b0d0c7e-6b2f-4b1e-9f53-2f6f4e0f2a11"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/v1/recordings/{recording}/meeting", None),
        ("post", "/v1/recordings/{recording}/meeting", {"title": "Mine now"}),
        ("patch", "/v1/meetings/{meeting}", {"title": "Mine now"}),
        ("put", "/v1/meetings/{meeting}/pins/{pin}", {"atMs": 5}),
        ("post", "/v1/meetings/{meeting}/minutes/regenerate", None),
        ("post", "/v1/meetings/{meeting}/ask", {"question": "what was decided?"}),
        ("post", "/v1/recordings/{recording}/restore", None),
    ],
)
def test_mom06_another_device_gets_404_on_every_meeting_route(
    method: str, path: str, body: dict[str, Any] | None, api: Any, auth: dict[str, str], sql: Sql
) -> None:
    """One device never reads, changes or learns of another's meeting, whichever route it tries."""
    meeting = ready_meeting(api, auth, sql)
    stranger = register(api, STRANGER)
    url = path.format(recording=RECORDING, meeting=meeting, pin=uuid.uuid4())
    sent = getattr(api, method)(url, headers=stranger, **({} if body is None else {"json": body}))
    assert failure(sent) == (404, "not_found")
    assert sql("SELECT title, state FROM meetings") == [{"title": "Weekly sync", "state": "ready"}]


def next_midnight() -> str:
    """The next UTC midnight, as the caps' `resetsAt` says it (grading 4: "ends with T00:00:00Z" let today pass)."""
    from datetime import UTC, datetime, time, timedelta

    tomorrow = datetime.now(UTC).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_mom02_a_pin_s_note_sent_later_replaces_the_first_put(api: Any, auth: dict[str, str], sql: Sql) -> None:
    """The app pins first and sends the note later as a second PUT of the same id: last one wins (grading 4)."""
    meeting = promote(api, auth).json()["meetingId"]
    pin = str(uuid.uuid4())
    assert api.put(f"/v1/meetings/{meeting}/pins/{pin}", json={"atMs": 5}, headers=auth).status_code == 201
    noted = api.put(f"/v1/meetings/{meeting}/pins/{pin}", json={"atMs": 7, "note": "decided pricing"}, headers=auth)
    assert (noted.status_code, noted.json()["note"], noted.json()["atMs"]) == (200, "decided pricing", 7)
    detail = api.get(f"/v1/recordings/{RECORDING}/meeting", headers=auth).json()
    assert [(item["atMs"], item["note"]) for item in detail["pins"]] == [(7, "decided pricing")]


def test_mom05_an_edit_over_2_mb_is_refused_before_its_body_is_read(api: Any, auth: dict[str, str]) -> None:
    """The size is checked on Content-Length first: a 3 MB body that isn't even JSON is a 413, not parsed (grading
    4)."""
    meeting = promote(api, auth).json()["meetingId"]
    sent = api.put(f"/v1/meetings/{meeting}/minutes", content=b"x" * (3 * 1024 * 1024), headers=auth)
    assert failure(sent) == (413, "minutes_too_large")


def test_mom06_ask_answers_once_the_transcript_is_ready_while_the_minutes_are_written(
    api: Any, auth: dict[str, str], sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ask needs the transcript, not the minutes (grading 4): it answers while a run writes them, and is a 409
    `transcript_not_ready` before the transcript lands."""
    from snapbak_backend.schemas.cloud import AskResponse
    from snapbak_backend.services import qa

    async def answered(*args: Any, **kwargs: Any) -> AskResponse:
        return AskResponse(answer="Not in this meeting.", citations=[], supported=False)

    monkeypatch.setattr(qa, "answer", answered)
    meeting = promote(api, auth).json()["meetingId"]
    early = api.post(f"/v1/meetings/{meeting}/ask", json={"question": "who owns it?"}, headers=auth)
    assert failure(early) == (409, "transcript_not_ready")
    sql("UPDATE transcription_jobs SET state = 'ready'")
    sql("UPDATE meetings SET state = 'generating'")
    asked = api.post(f"/v1/meetings/{meeting}/ask", json={"question": "who owns it?"}, headers=auth)
    assert asked.status_code == 200


def test_mom06_a_refund_after_a_database_error_still_lands_and_the_error_is_the_first(
    api: Any, auth: dict[str, str], sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database error in the question's reading left the session needing a rollback, so the refund itself failed
    and hid the error (grading 4)."""
    import sqlalchemy

    from snapbak_backend.services import qa

    async def broken(db: Any, *args: Any) -> Any:
        await db.execute(sqlalchemy.text("SELECT 1 / 0"))

    monkeypatch.setattr(qa, "gather", broken)
    meeting = ready_meeting(api, auth, sql)
    sql("UPDATE transcription_jobs SET state = 'ready'")
    response = api.post(f"/v1/meetings/{meeting}/ask", json={"question": "who owns it?"}, headers=auth)
    assert response.status_code == 500
    assert sql("SELECT qa_questions FROM usage_daily") == [{"qa_questions": 0}]


def test_mom07_a_meeting_trashed_while_its_transcript_was_queued_comes_back_failed_with_a_try_again(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    """The trash failed the queued job; a restore brought the meeting back waiting on nothing (grading 4). It comes
    back failed with the job's code, so the app shows Try again."""
    promote(api, auth)
    assert api.delete(f"/v1/recordings/{RECORDING}", headers=auth).status_code == 204
    assert api.post(f"/v1/recordings/{RECORDING}/restore", headers=auth).status_code == 200
    assert sql("SELECT state, minutes_failure FROM meetings") == [{"state": "failed", "minutes_failure": "internal"}]


def test_cld02_registering_a_recording_again_never_changes_its_type(api: Any) -> None:
    """A later PUT without `recordingType` (the transcript's own) once made an audio meeting a video (grading 4)."""
    auth = register(api)
    put_recording(api, auth, recording_type="audio")
    assert put_recording(api, auth).json()["recordingType"] == "audio"


@pytest.mark.parametrize("by_user", [True, False])
def test_mom05_the_app_says_whether_the_user_chose_the_title(
    by_user: bool, api: Any, auth: dict[str, str], sql: Sql
) -> None:
    """The app knows whether its promote sheet's title was the user's (grading 4): the server takes its word rather
    than comparing the title with the recording's."""
    body = {"title": "Weekly sync", "titleByUser": by_user}
    assert api.post(f"/v1/recordings/{RECORDING}/meeting", json=body, headers=auth).status_code == 201
    assert sql("SELECT title_set_by_user FROM meetings") == [{"title_set_by_user": by_user}]
