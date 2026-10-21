"""The vendors' webhooks: authenticated, quick, and only ever a wake-up for the worker (docs/cloud.md)."""

import hashlib
import hmac
import json
import statistics
import time
import uuid
from typing import Any

import pytest
from conftest import RECORDING, Sql, create_job, failure, put_recording, queued_arq_jobs, register, s3

REFS = {
    "language": "auto",
    "assemblyai": {"id": "t-1", "status": "running"},
    "pyannote": {"jobId": "p-1", "status": "running", "rawKey": "raw/r/j/pyannote-diarize.json"},
}


@pytest.fixture
def job(api: Any, sql: Sql) -> str:
    auth = register(api)
    put_recording(api, auth)
    made = create_job(api, auth).json()["jobId"]
    sql(
        "UPDATE transcription_jobs SET state = 'processing', stage = 'transcribing', vendor_refs = $1",
        json.dumps(REFS),
    )
    return made


def aai_header(job: str) -> dict[str, str]:
    digest = hmac.new(b"hook-secret", f"assemblyai:{job}".encode(), hashlib.sha256).hexdigest()
    return {"X-Snapbak-Webhook": digest}


def pyannote_headers(body: bytes) -> dict[str, str]:
    # A live delivery's timestamp is milliseconds, 300 s ahead.
    timestamp = str((int(time.time()) + 300) * 1000)
    signed = hmac.new(b"pyannote-secret", f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Request-Timestamp": timestamp, "X-Signature": signed, "Content-Type": "application/json"}


def test_trx01_an_assemblyai_webhook_with_the_job_hmac_enqueues_the_task_and_answers_204_fast(
    api: Any, job: str
) -> None:
    body = {"transcript_id": "t-1", "status": "completed"}
    times_ms = []
    for _ in range(5):
        started = time.monotonic()
        response = api.post(f"/v1/webhooks/assemblyai?job={job}", json=body, headers=aai_header(job))
        times_ms.append((time.monotonic() - started) * 1000)
        assert response.status_code == 204
    assert queued_arq_jobs() == [f"hook:assemblyai:{job}"]
    # The HLD's budget; AssemblyAI's own limit is 10 s.
    assert statistics.median(times_ms) < 50


def test_trx01_an_assemblyai_webhook_with_a_wrong_hmac_is_401(api: Any, job: str) -> None:
    body = {"transcript_id": "t-1", "status": "completed"}
    other = aai_header(str(uuid.uuid4()))
    for headers in (other, {}):
        response = api.post(f"/v1/webhooks/assemblyai?job={job}", json=body, headers=headers)
        assert (response.status_code, response.json()["error"]["code"]) == (401, "unauthorized")
    assert queued_arq_jobs() == []


def test_trx01_a_stray_assemblyai_webhook_answers_204_and_does_nothing(api: Any, job: str) -> None:
    stray = api.post(
        f"/v1/webhooks/assemblyai?job={job}",
        json={"transcript_id": "t-9", "status": "completed"},
        headers=aai_header(job),
    )
    unknown_job = str(uuid.uuid4())
    unknown = api.post(
        f"/v1/webhooks/assemblyai?job={unknown_job}",
        json={"transcript_id": "t-1", "status": "completed"},
        headers=aai_header(unknown_job),
    )
    assert (stray.status_code, unknown.status_code) == (204, 204)
    assert queued_arq_jobs() == []


def test_trx01_a_signed_pyannote_webhook_keeps_the_output_and_enqueues_the_task(api: Any, job: str) -> None:
    output = {"exclusiveDiarization": [{"speaker": "SPEAKER_00", "start": 0.5, "end": 1.0}]}
    body = json.dumps({"jobId": "p-1", "status": "succeeded", "output": output}).encode()
    response = api.post(f"/v1/webhooks/pyannote?job={job}&kind=diarize", content=body, headers=pyannote_headers(body))
    assert response.status_code == 204
    bucket = api.app.state.cloud.config.bucket
    stored = json.loads(s3().get_object(Bucket=bucket, Key=REFS["pyannote"]["rawKey"])["Body"].read())
    assert stored == output
    assert f"hook:pyannote:{job}" in queued_arq_jobs()


def test_trx01_a_pyannote_webhook_with_a_bad_signature_or_no_timestamp_is_401(api: Any, job: str) -> None:
    body = json.dumps({"jobId": "p-1", "status": "succeeded", "output": {}}).encode()
    tampered = {**pyannote_headers(body), "X-Signature": "00"}
    undated = {key: value for key, value in pyannote_headers(body).items() if key != "X-Request-Timestamp"}
    for headers in (tampered, undated):
        response = api.post(f"/v1/webhooks/pyannote?job={job}&kind=diarize", content=body, headers=headers)
        assert failure(response) == (401, "unauthorized")
    assert queued_arq_jobs() == []


def voiceprint_hook(api: Any, job: str, speaker: object, vendor_job: str) -> int:
    body = json.dumps({"jobId": vendor_job, "status": "succeeded", "output": {"voiceprint": "QUJD"}}).encode()
    response = api.post(
        f"/v1/webhooks/pyannote?job={job}&kind=voiceprint&speaker={speaker}",
        content=body,
        headers=pyannote_headers(body),
    )
    return response.status_code


def test_trx08_a_voiceprint_webhook_before_its_job_is_recorded_is_kept_for_on_voiceprint(
    api: Any, job: str, sql: Sql
) -> None:
    speaker = uuid.uuid4()
    sql(
        "INSERT INTO speakers (id, recording_id, label, vendor_label, color_idx) VALUES ($1, $2, 'Speaker 1', 'S', 0)",
        speaker,
        uuid.UUID(RECORDING),
    )
    assert voiceprint_hook(api, job, speaker, "v-1") == 204
    assert queued_arq_jobs() == [f"vp:{speaker}:v-1"]


def test_trx08_only_the_voiceprint_job_recorded_for_that_speaker_enqueues_on_voiceprint(
    api: Any, job: str, sql: Sql
) -> None:
    speaker = uuid.uuid4()
    sql(
        "INSERT INTO speakers (id, recording_id, label, vendor_label, color_idx) VALUES ($1, $2, 'Speaker 1', 'S', 0)",
        speaker,
        uuid.UUID(RECORDING),
    )
    refs = {**REFS, "voiceprints": {str(speaker): "v-1"}}
    sql("UPDATE transcription_jobs SET vendor_refs = $1", json.dumps(refs))
    assert (voiceprint_hook(api, job, speaker, "v-9"), queued_arq_jobs()) == (204, [])
    assert (voiceprint_hook(api, job, uuid.uuid4(), "v-1"), queued_arq_jobs()) == (204, [])
    assert (voiceprint_hook(api, job, speaker, "v-1"), queued_arq_jobs()) == (204, [f"vp:{speaker}:v-1"])
