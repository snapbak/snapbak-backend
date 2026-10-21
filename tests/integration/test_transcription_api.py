import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import RECORDING, Sql, create_job, failure, put_recording, queued_arq_jobs, register, upload_audio


@pytest.fixture
def auth(api: Any) -> dict[str, str]:
    made = register(api)
    put_recording(api, made)
    return made


def test_trx01_generate_makes_a_queued_job_that_needs_the_audio_and_reserves_its_minutes(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    response = create_job(api, auth)
    body = response.json()
    assert response.status_code == 201
    assert (body["state"], body["version"], body["needsUpload"]) == ("queued", 1, True)
    job = sql("SELECT state, language_requested, billed_seconds, attempt, upload_id FROM transcription_jobs")
    assert job == [
        {"state": "queued", "language_requested": "auto", "billed_seconds": 613, "attempt": 1, "upload_id": None}
    ]
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 613}]
    assert queued_arq_jobs() == []


def test_trx01_a_second_generate_answers_409_with_the_job_and_reserves_nothing(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    first = create_job(api, auth).json()
    second = create_job(api, auth)
    assert failure(second) == (409, "job_exists")
    assert second.json() == {
        "error": {"code": "job_exists", "message": "This recording already has a transcript or one in progress."},
        "job": {"jobId": first["jobId"], "state": "queued", "version": 1},
    }
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 613}]


@pytest.mark.parametrize("language", ["auto", "hinglish", "hi", "en", "ja"])
def test_trx01_auto_hinglish_and_the_18_are_accepted(api: Any, auth: dict[str, str], language: str, sql: Sql) -> None:
    assert create_job(api, auth, language).status_code == 201
    assert sql("SELECT language_requested FROM transcription_jobs") == [{"language_requested": language}]


def test_trx01_a_language_outside_the_18_is_refused_before_upload(api: Any, auth: dict[str, str], sql: Sql) -> None:
    response = create_job(api, auth, "ta")
    assert (response.status_code, response.json()["error"]["code"]) == (422, "unsupported_language")
    assert sql("SELECT count(*) AS n FROM usage_daily") == [{"n": 0}]


def test_cld03_a_recording_over_2_hours_is_refused(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth, duration_ms=7_200_001)
    response = create_job(api, auth)
    assert (response.status_code, response.json()["error"]["code"]) == (413, "too_long")
    assert put_recording(api, auth, duration_ms=7_200_000).status_code == 200
    assert create_job(api, auth).status_code == 201


def test_cld03_past_120_minutes_today_is_refused_with_the_reset_time(api: Any, auth: dict[str, str], sql: Sql) -> None:
    device = sql("SELECT id FROM devices")[0]["id"]
    sql(
        "INSERT INTO usage_daily (device_id, day, audio_seconds) VALUES ($1, (now() AT TIME ZONE 'UTC')::date, 6600)",
        device,
    )
    response = create_job(api, auth)
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    assert (response.status_code, response.json()["error"]["code"]) == (429, "daily_limit")
    assert response.json()["resetsAt"] == tomorrow
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 6600}]


def test_trx01_unknown_recordings_and_bad_bodies(api: Any, auth: dict[str, str]) -> None:
    unknown = create_job(api, auth, recording=str(uuid.uuid4()))
    bad = api.post(f"/v1/recordings/{RECORDING}/transcription", json={"language": "auto", "script": "x"}, headers=auth)
    assert (failure(unknown), failure(bad)) == ((404, "not_found"), (422, "invalid"))


def test_cld02_complete_hands_the_audio_to_the_waiting_job_and_starts_it(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    job = create_job(api, auth).json()["jobId"]
    upload_id, done = upload_audio(api, auth, b"m4a")
    assert done.json()["jobId"] == job
    assert sql("SELECT upload_id::text, version FROM transcription_jobs") == [{"upload_id": upload_id, "version": 2}]
    assert queued_arq_jobs() == [f"start:{job}"]


def test_trx01_retry_after_failure_makes_a_new_job_on_the_audio_already_uploaded(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    first = create_job(api, auth).json()["jobId"]
    upload_id, _ = upload_audio(api, auth, b"m4a")
    sql("UPDATE transcription_jobs SET state = 'failed', failure_reason = 'vendor_error'")
    retried = create_job(api, auth)
    body = retried.json()
    assert (retried.status_code, body["needsUpload"]) == (201, False)
    assert body["jobId"] != first
    rows = sql("SELECT state, attempt, upload_id::text FROM transcription_jobs ORDER BY attempt")
    assert rows == [
        {"state": "failed", "attempt": 1, "upload_id": upload_id},
        {"state": "queued", "attempt": 2, "upload_id": upload_id},
    ]
    assert f"start:{body['jobId']}" in queued_arq_jobs()


def test_trx01_job_state_answers_at_once_with_wait_0(api: Any, auth: dict[str, str]) -> None:
    job = create_job(api, auth).json()["jobId"]
    response = api.get(f"/v1/jobs/{job}", headers=auth)
    assert (response.status_code, response.json()) == (
        200,
        {
            "jobId": job,
            "recordingId": RECORDING,
            "state": "queued",
            "stage": None,
            "failure": None,
            "language": None,
            "version": 1,
        },
    )


def test_trx01_job_state_of_another_device_or_bad_wait(api: Any, auth: dict[str, str]) -> None:
    job = create_job(api, auth).json()["jobId"]
    other = register(api, str(uuid.uuid4()))
    assert failure(api.get(f"/v1/jobs/{job}", headers=other)) == (404, "not_found")
    assert failure(api.get(f"/v1/jobs/{job}?wait=26", headers=auth)) == (422, "invalid")
    assert failure(api.get(f"/v1/jobs/{uuid.uuid4()}", headers=auth)) == (404, "not_found")
