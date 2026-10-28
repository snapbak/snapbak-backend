"""The cloud transcript end to end in one process: the API, the arq worker and the fake strategy replaying recorded
vendor JSON through the real merge (TRX-01, TRX-08, TRX-09, CLD-01 to CLD-04)."""

import json
import logging
from collections.abc import Callable
from typing import Any

import pytest
from conftest import (
    RECORDING,
    Sql,
    await_tasks,
    create_job,
    eventually,
    failure,
    follow,
    generate,
    on_change,
    queued_arq_jobs,
    ran,
    register,
    watching,
)

from snapbak_backend.services.sealing import unseal
from snapbak_backend.transcription.fake import VOICEPRINT

Start = Callable[[str], None]


def lines_of(transcript: dict[str, Any]) -> list[tuple[str, str]]:
    labels = {speaker["id"]: speaker["label"] for speaker in transcript["speakers"]}
    return [(labels[line["speakerId"]], line["text"]) for line in transcript["lines"]]


def test_trx01_generate_to_ready_gives_the_exact_two_speaker_transcript(
    api: Any, start_worker: Start, sql: Sql
) -> None:
    start_worker("two-speakers-en")
    auth = register(api)
    job = generate(api, auth)
    final = follow(api, auth, job)
    assert (final["state"], final["stage"], final["failure"], final["language"]) == ("ready", None, None, "en_us")
    transcript = api.get(f"/v1/recordings/{RECORDING}/transcript", headers=auth).json()
    assert (transcript["recordingId"], transcript["language"], transcript["script"]) == (RECORDING, "en_us", "native")
    assert [(s["label"], s["name"], s["nameSource"], s["colorIdx"]) for s in transcript["speakers"]] == [
        ("Speaker 1", "Priya", "vendor", 0),
        ("Speaker 2", "Arjun", "vendor", 1),
    ]
    assert lines_of(transcript) == [
        ("Speaker 1", "Hi Arjun, um, thanks for joining."),
        ("Speaker 2", "Thanks Priya."),
        ("Speaker 2", "Uh, shall we start with the dashboard?"),
        ("Speaker 1", "Yes."),
        ("Speaker 1", "So here you can see the numbers."),
    ]
    first = transcript["lines"][0]
    assert (first["idx"], first["start"], first["end"], first["rev"], first["romanText"]) == (0, 1200, 2940, 1, None)
    assert first["words"][2] == {"w": "um,", "s": 1800, "e": 2040, "filler": True, "conf": 0.95}
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 613}]


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [("fail-unsupported", "unsupported_language"), ("fail-no-speech", "no_speech"), ("fail-vendor", "vendor_error")],
)
def test_trx01_each_failure_names_its_reason_refunds_the_minutes_and_a_retry_makes_a_new_job(
    api: Any, start_worker: Start, sql: Sql, fixture: str, reason: str
) -> None:
    start_worker(fixture)
    auth = register(api)
    job = generate(api, auth)
    final = follow(api, auth, job)
    assert (final["state"], final["failure"]) == ("failed", reason)
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 0}]
    assert failure(api.get(f"/v1/recordings/{RECORDING}/transcript", headers=auth)) == (404, "not_found")
    retry = create_job(api, auth)
    assert (retry.status_code, retry.json()["needsUpload"]) == (201, False)
    assert retry.json()["jobId"] != job


def test_trx08_with_consent_each_speaker_gets_a_sealed_voiceprint(api: Any, start_worker: Start, sql: Sql) -> None:
    start_worker("two-speakers-en")
    auth = register(api)
    api.put("/v1/devices/me/consent", json={"voiceprints": True}, headers=auth)
    job = generate(api, auth)
    follow(api, auth, job)
    await_tasks(f"voiceprints:{job}")
    # Speaker 2 speaks 3.1 s at a stretch; Speaker 1's longest run is 2.5 s, under the 3 s a voiceprint needs.
    speaker = sql("SELECT id FROM speakers WHERE label = 'Speaker 2'")[0]["id"]
    await_tasks(f"vp:{speaker}:fake-vp-{speaker}")
    rows = sql("SELECT id, label, voiceprint, voiceprint_key_id FROM speakers WHERE voiceprint IS NOT NULL")
    assert [(row["label"], row["voiceprint_key_id"]) for row in rows] == [("Speaker 2", 1)]
    assert unseal({1: bytes(32)}, rows[0]["id"], rows[0]["voiceprint"]) == VOICEPRINT.encode()
    refs = json.loads(sql("SELECT vendor_refs FROM transcription_jobs")[0]["vendor_refs"])
    assert refs["voiceprints"] == {str(speaker): f"fake-vp-{speaker}"}


def test_trx08_without_consent_no_voiceprint_is_made(api: Any, start_worker: Start, sql: Sql) -> None:
    start_worker("two-speakers-en")
    auth = register(api)
    job = generate(api, auth)
    follow(api, auth, job)
    # merge_and_store enqueues create_voiceprints, if at all, before it finishes.
    await_tasks(f"merge:{job}")
    assert not ran(f"voiceprints:{job}")
    assert f"voiceprints:{job}" not in queued_arq_jobs()
    assert sql("SELECT count(*) AS n FROM speakers WHERE voiceprint IS NOT NULL") == [{"n": 0}]


def test_trx09_a_roman_script_transcript_gets_roman_words_after_ready(api: Any, start_worker: Start, sql: Sql) -> None:
    start_worker("hinglish")
    auth = register(api)
    final = follow(api, auth, generate(api, auth, language="hinglish", script="roman"))
    assert (final["state"], final["language"]) == ("ready", "hi")
    rows = eventually(lambda: sql("SELECT roman_text FROM transcript_lines WHERE roman_text IS NOT NULL"), bool)
    assert rows[0]["roman_text"].split()[:2] == ["r:मेरा", "r:रुकने"]
    transcript = api.get(f"/v1/recordings/{RECORDING}/transcript", headers=auth).json()
    line = transcript["lines"][0]
    assert line["words"][0]["filler"] is False
    assert [w["w"] for w in line["romanWords"]][-1] == "r:tomorrow."
    assert [(w["s"], w["e"]) for w in line["romanWords"]] == [(w["s"], w["e"]) for w in line["words"]]


def test_cld04_deleting_a_recording_while_it_processes_fails_the_job_and_writes_no_lines(
    api: Any, start_worker: Start, sql: Sql
) -> None:
    start_worker("slow")
    auth = register(api)
    job = generate(api, auth)
    answer = api.get(f"/v1/jobs/{job}?wait=5&since=2", headers=auth).json()
    assert (answer["state"], answer["stage"]) == ("processing", "transcribing")
    query = "SELECT state, failure_reason, deleted_at IS NOT NULL AS gone FROM transcription_jobs"
    with watching(job) as changes:
        assert api.delete(f"/v1/recordings/{RECORDING}", headers=auth).status_code == 204
        rows = on_change(changes, lambda: sql(query), lambda rows: rows[0]["state"] == "failed")
    assert rows == [{"state": "failed", "failure_reason": "internal", "gone": True}]
    assert sql("SELECT count(*) AS n FROM transcript_lines") == [{"n": 0}]
    assert sql("SELECT count(*) AS n FROM uploads WHERE deleted_at IS NULL") == [{"n": 0}]
    assert failure(api.get(f"/v1/jobs/{job}", headers=auth)) == (404, "not_found")


def test_trx01_the_logs_hold_no_word_name_or_title(
    api: Any, start_worker: Start, caplog: pytest.LogCaptureFixture
) -> None:
    start_worker("two-speakers-en")
    with caplog.at_level(logging.INFO):
        auth = register(api)
        api.put("/v1/devices/me/consent", json={"voiceprints": True}, headers=auth)
        job = generate(api, auth)
        follow(api, auth, job)
        await_tasks(f"merge:{job}", f"voiceprints:{job}")
    logged = "\n".join(record.getMessage() for record in caplog.records).lower()
    assert "task=merge_and_store" in logged
    for secret in ("priya", "arjun", "dashboard", "joining", "recording 2026", "fake m4a", auth["Authorization"][7:]):
        assert secret.lower() not in logged, secret


def test_trx01_generate_on_a_ready_transcript_follows_it_and_bills_nothing(
    api: Any, start_worker: Start, sql: Sql
) -> None:
    start_worker("two-speakers-en")
    auth = register(api)
    job = generate(api, auth)
    follow(api, auth, job)
    again = create_job(api, auth)
    assert failure(again) == (409, "job_exists")
    # Versions: made 1, audio 2, submitted 3, each vendor done 4 and 5, held for the speaker loop 6, ready 7.
    assert again.json()["job"] == {"jobId": job, "state": "ready", "version": 7}
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 613}]
    assert sql("SELECT count(*) AS n FROM transcription_jobs") == [{"n": 1}]
