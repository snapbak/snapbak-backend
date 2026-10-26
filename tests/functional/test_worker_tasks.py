"""The worker's safety nets, called as arq calls them: vendor refusals and outages, tries running out, the timeout
check for a lost webhook, the sweep of uploads that never came, duplicate webhooks, and consent (docs/cloud.md)."""

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from arq import Retry
from conftest import (
    RECORDING,
    Sql,
    admin_on,
    create_job,
    failure,
    fake_cut,
    put_recording,
    queued_arq_jobs,
    register,
    upload_audio,
)

from snapbak_backend import worker
from snapbak_backend.config.settings import load
from snapbak_backend.services.cloud import open_cloud
from snapbak_backend.transcription.merge import Merged, SpeakerOut, Word
from snapbak_backend.transcription.strategy import (
    VendorError,
    VendorRefused,
    VendorState,
    VendorStatus,
    VendorUnavailable,
)

REFS = {
    "language": "auto",
    "assemblyai": {"id": "t-1", "status": "running"},
    "pyannote": {"jobId": "p-1", "status": "running"},
}


class Vendors:
    """A strategy whose vendors answer what the test says; it records what it was asked and what it cleaned up."""

    def __init__(
        self, error: Exception | None = None, diarization: list[dict[str, Any]] | None = None, **states: VendorState
    ) -> None:
        self.error = error
        self.turns = diarization
        self.states = states
        self.asked: list[str] = []
        self.cleaned: list[dict[str, Any]] = []
        self.clips: list[str] = []
        self.while_submitting: Callable[[], Awaitable[Any]] | None = None

    async def submit(self, job: Any, refs: dict[str, Any]) -> dict[str, Any]:
        if self.while_submitting is not None:
            await self.while_submitting()
        if self.error is not None:
            raise self.error
        return {**refs, **REFS}

    async def status(self, refs: dict[str, Any], vendor: str) -> VendorStatus:
        self.asked.append(vendor)
        if self.error is not None:
            raise self.error
        state = self.states.get(vendor, "running")
        return VendorStatus(state, "vendor_error" if state == "failed" else None)

    async def result(self, refs: dict[str, Any]) -> Any:
        if self.error is not None:
            raise self.error
        said = [Word(w="Hi.", s=0, e=400, filler=False, conf=0.9, speaker="SPEAKER_00")]
        merged = Merged(language="en", audio_ms=60_000, speakers=[SpeakerOut("SPEAKER_00", None)], lines=[said])
        return merged

    async def diarization(self, refs: dict[str, Any]) -> list[dict[str, Any]]:
        if self.turns is None:
            raise self.error or AssertionError("no diarization scripted")
        return self.turns

    async def voiceprint(self, clip_url: str, webhook: str) -> str:
        self.clips.append(clip_url)
        return "v-made"

    async def voiceprint_output(self, vendor_job: str) -> str:
        return "QUJD"

    async def cleanup(self, refs: dict[str, Any]) -> None:
        self.cleaned.append(refs)


def partial(error: VendorError) -> VendorError:
    """`error` as a submit raises it when AssemblyAI accepted and pyannoteAI didn't."""
    error.refs = {"language": "auto", "assemblyai": {"id": "t-1", "status": "running"}}
    return error


def run(task: Callable[..., Any], strategy: Any, *args: Any, job_try: int = 1) -> None:
    async def call() -> None:
        whole = load(os.environ)
        config = whole.cloud
        assert config is not None
        cloud = await open_cloud(config, whole.keys)
        try:
            ctx = {"cloud": cloud, "strategy": strategy, "job_try": job_try, "redis": cloud.redis, "cut": fake_cut}
            await task(ctx, *args)
        finally:
            await cloud.close()

    asyncio.run(call())


@pytest.fixture
def processing(api: Any, sql: Sql) -> str:
    auth = register(api)
    put_recording(api, auth, duration_ms=60_000)
    job = create_job(api, auth).json()["jobId"]
    sql(
        "UPDATE transcription_jobs SET state = 'processing', stage = 'transcribing', vendor_refs = $1,"
        " submitted_at = now()",
        json.dumps(REFS),
    )
    return job


def test_trx01_the_timeout_check_asks_both_vendors_and_checks_again_in_a_minute(processing: str, sql: Sql) -> None:
    vendors = Vendors(assemblyai="done")
    run(worker.check_vendor_timeout, vendors, processing, 1)
    assert vendors.asked == ["assemblyai", "pyannote"]
    assert sql("SELECT state, stage, version FROM transcription_jobs") == [
        {"state": "processing", "stage": "diarizing", "version": 2}
    ]
    assert queued_arq_jobs() == [f"timeout:{processing}:2"]


def test_trx01_the_timeout_check_merges_once_both_vendors_are_done(processing: str, sql: Sql) -> None:
    run(worker.check_vendor_timeout, Vendors(assemblyai="done", pyannote="done"), processing, 1)
    assert sql("SELECT stage FROM transcription_jobs") == [{"stage": "merging"}]
    assert queued_arq_jobs() == [f"merge:{processing}"]


def test_trx01_past_the_deadline_the_job_fails_with_vendor_timeout_and_refunds(processing: str, sql: Sql) -> None:
    sql("UPDATE transcription_jobs SET submitted_at = now() - interval '16 minutes'")
    run(worker.check_vendor_timeout, Vendors(), processing, 7)
    assert sql("SELECT state, failure_reason FROM transcription_jobs") == [
        {"state": "failed", "failure_reason": "vendor_timeout"}
    ]
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 0}]
    assert queued_arq_jobs() == []


def test_trx01_a_duplicate_webhook_changes_nothing(processing: str, sql: Sql) -> None:
    vendors = Vendors(assemblyai="done")
    run(worker.on_vendor_webhook, vendors, processing, "assemblyai")
    run(worker.on_vendor_webhook, vendors, processing, "assemblyai")
    assert vendors.asked == ["assemblyai"]
    assert sql("SELECT version FROM transcription_jobs") == [{"version": 2}]


def test_trx01_a_webhook_before_the_submit_is_recorded_is_asked_again(processing: str, sql: Sql) -> None:
    sql("UPDATE transcription_jobs SET state = 'queued'")
    with pytest.raises(Retry):
        run(worker.on_vendor_webhook, Vendors(assemblyai="done"), processing, "assemblyai")


def test_cld03_a_job_waiting_24_hours_for_its_audio_expires_and_refunds(processing: str, sql: Sql) -> None:
    sql(
        "UPDATE transcription_jobs SET state = 'queued', stage = NULL, vendor_refs = '{}',"
        " created_at = now() - interval '25 hours'"
    )
    sql("UPDATE usage_daily SET day = (SELECT (created_at AT TIME ZONE 'UTC')::date FROM transcription_jobs)")
    run(worker.sweep_waiting_jobs, None)
    assert sql("SELECT state, failure_reason FROM transcription_jobs") == [
        {"state": "failed", "failure_reason": "upload_expired"}
    ]
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 0}]


def test_cld03_a_job_waiting_less_than_24_hours_is_left_alone(processing: str, sql: Sql) -> None:
    sql(
        "UPDATE transcription_jobs SET state = 'queued', stage = NULL, vendor_refs = '{}',"
        " created_at = now() - interval '23 hours'"
    )
    run(worker.sweep_waiting_jobs, None)
    assert sql("SELECT state FROM transcription_jobs") == [{"state": "queued"}]


@pytest.fixture
def uploaded(api: Any, sql: Sql) -> str:
    """A queued job with its audio uploaded, as start_transcription finds it."""
    auth = register(api)
    put_recording(api, auth, duration_ms=60_000)
    job = create_job(api, auth).json()["jobId"]
    upload_audio(api, auth, b"m4a")
    return job


def test_trx01_a_vendor_refusing_the_submit_fails_the_job_and_deletes_the_vendor_that_accepted(
    uploaded: str, sql: Sql
) -> None:
    vendors = Vendors(partial(VendorRefused("pyannote: 400")))
    run(worker.start_transcription, vendors, uploaded)
    assert sql("SELECT state, failure_reason FROM transcription_jobs") == [
        {"state": "failed", "failure_reason": "vendor_error"}
    ]
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 0}]
    assert vendors.cleaned == [{"language": "auto", "assemblyai": {"id": "t-1", "status": "running"}}]


def test_trx01_a_vendor_outage_keeps_the_vendor_that_accepted_and_asks_again(uploaded: str, sql: Sql) -> None:
    vendors = Vendors(partial(VendorUnavailable("pyannote: 503")))
    with pytest.raises(Retry):
        run(worker.start_transcription, vendors, uploaded)
    rows = sql("SELECT state, vendor_refs FROM transcription_jobs")
    assert (rows[0]["state"], json.loads(rows[0]["vendor_refs"])["assemblyai"]["id"]) == ("queued", "t-1")
    assert vendors.cleaned == []


def test_trx01_when_the_submits_tries_run_out_the_job_fails_internal_and_is_cleaned_up(uploaded: str, sql: Sql) -> None:
    vendors = Vendors(partial(VendorUnavailable("pyannote: 503")))
    run(worker.start_transcription, vendors, uploaded, job_try=worker.MAX_TRIES)
    assert sql("SELECT state, failure_reason FROM transcription_jobs") == [
        {"state": "failed", "failure_reason": "internal"}
    ]
    assert vendors.cleaned[0]["assemblyai"]["id"] == "t-1"


def test_trx01_when_the_merges_tries_run_out_the_job_fails_internal(processing: str, sql: Sql) -> None:
    sql("UPDATE transcription_jobs SET stage = 'merging'")
    vendors = Vendors(VendorUnavailable("assemblyai: 502"))
    with pytest.raises(Retry):
        run(worker.merge_and_store, vendors, processing)
    run(worker.merge_and_store, vendors, processing, job_try=worker.MAX_TRIES)
    assert sql("SELECT state, failure_reason FROM transcription_jobs") == [
        {"state": "failed", "failure_reason": "internal"}
    ]
    assert vendors.cleaned == [REFS]


def test_trx01_a_vendor_outage_during_the_timeout_check_checks_again_instead_of_retrying(
    processing: str, sql: Sql
) -> None:
    run(worker.check_vendor_timeout, Vendors(VendorUnavailable("assemblyai: 503")), processing, 3)
    assert sql("SELECT state FROM transcription_jobs") == [{"state": "processing"}]
    assert queued_arq_jobs() == [f"timeout:{processing}:4"]


def test_trx01_a_webhook_whose_vendor_stays_down_gives_up_to_the_timeout_check(processing: str, sql: Sql) -> None:
    vendors = Vendors(VendorUnavailable("assemblyai: 503"))
    with pytest.raises(Retry):
        run(worker.on_vendor_webhook, vendors, processing, "assemblyai")
    run(worker.on_vendor_webhook, vendors, processing, "assemblyai", job_try=worker.MAX_TRIES)
    assert sql("SELECT state, version FROM transcription_jobs") == [{"state": "processing", "version": 1}]


def ready_with_speaker(sql: Sql, job: str, voiceprint: bytes | None = None) -> uuid.UUID:
    speaker = uuid.uuid4()
    sql("UPDATE transcription_jobs SET state = 'ready', stage = NULL")
    sql(
        "INSERT INTO speakers (id, recording_id, label, vendor_label, color_idx, voiceprint)"
        " VALUES ($1, $2, 'Speaker 1', 'SPEAKER_00', 0, $3)",
        speaker,
        uuid.UUID(RECORDING),
        voiceprint,
    )
    return speaker


def test_trx08_create_voiceprints_gives_up_after_3_tries_and_leaves_the_transcript(uploaded: str, sql: Sql) -> None:
    ready_with_speaker(sql, uploaded)
    sql("UPDATE devices SET voiceprint_consent = true")
    vendors = Vendors(VendorUnavailable("pyannote: 503"))
    with pytest.raises(Retry):
        run(worker.create_voiceprints, vendors, uploaded, job_try=2)
    run(worker.create_voiceprints, vendors, uploaded, job_try=3)
    assert sql("SELECT state FROM transcription_jobs") == [{"state": "ready"}]
    assert sql("SELECT count(*) AS n FROM speakers WHERE voiceprint IS NOT NULL") == [{"n": 0}]


def test_trx08_create_voiceprints_asks_nothing_once_consent_is_withdrawn(uploaded: str, sql: Sql) -> None:
    ready_with_speaker(sql, uploaded)
    sql("UPDATE devices SET voiceprint_consent = false")
    run(worker.create_voiceprints, Vendors(AssertionError("asked a vendor without consent")), uploaded)


def record(sql: Sql, speaker: uuid.UUID, vendor_job: str) -> None:
    """What create_voiceprints records once pyannoteAI took the speaker's clip."""
    refs = {**REFS, "voiceprints": {str(speaker): vendor_job}}
    sql("UPDATE transcription_jobs SET vendor_refs = $1", json.dumps(refs))


def stored(sql: Sql) -> list[dict[str, Any]]:
    return sql("SELECT voiceprint IS NOT NULL AS stored FROM speakers")


def test_trx08_a_voiceprint_arriving_after_consent_was_withdrawn_is_not_stored(uploaded: str, sql: Sql) -> None:
    speaker = ready_with_speaker(sql, uploaded)
    record(sql, speaker, "v-1")
    sql("UPDATE devices SET voiceprint_consent = true")
    run(worker.on_voiceprint, Vendors(), uploaded, str(speaker), "v-1")
    assert stored(sql) == [{"stored": True}]
    sql("UPDATE speakers SET voiceprint = NULL")
    sql("UPDATE devices SET voiceprint_consent = false")
    run(worker.on_voiceprint, Vendors(), uploaded, str(speaker), "v-1")
    assert stored(sql) == [{"stored": False}]


def test_trx08_on_voiceprint_keeps_a_voiceprint_already_stored(uploaded: str, sql: Sql) -> None:
    speaker = ready_with_speaker(sql, uploaded, voiceprint=b"sealed-before")
    record(sql, speaker, "v-2")
    sql("UPDATE devices SET voiceprint_consent = true")
    run(worker.on_voiceprint, Vendors(), uploaded, str(speaker), "v-2")
    assert sql("SELECT voiceprint FROM speakers") == [{"voiceprint": b"sealed-before"}]


def test_trx08_a_voiceprint_webhook_before_its_job_is_recorded_waits_for_the_record(uploaded: str, sql: Sql) -> None:
    speaker = ready_with_speaker(sql, uploaded)
    sql("UPDATE devices SET voiceprint_consent = true")
    with pytest.raises(Retry):
        run(worker.on_voiceprint, Vendors(), uploaded, str(speaker), "v-1")
    assert stored(sql) == [{"stored": False}]
    record(sql, speaker, "v-1")
    run(worker.on_voiceprint, Vendors(), uploaded, str(speaker), "v-1")
    assert stored(sql) == [{"stored": True}]


def test_trx08_a_voiceprint_job_other_than_the_recorded_one_is_ignored(uploaded: str, sql: Sql) -> None:
    speaker = ready_with_speaker(sql, uploaded)
    record(sql, speaker, "v-1")
    sql("UPDATE devices SET voiceprint_consent = true")
    run(worker.on_voiceprint, Vendors(), uploaded, str(speaker), "v-9")
    assert stored(sql) == [{"stored": False}]


def test_trx08_no_voiceprint_is_stored_on_a_deleted_speaker(uploaded: str, sql: Sql) -> None:
    speaker = ready_with_speaker(sql, uploaded)
    record(sql, speaker, "v-1")
    sql("UPDATE devices SET voiceprint_consent = true")
    sql("UPDATE speakers SET deleted_at = now()")
    run(worker.on_voiceprint, Vendors(), uploaded, str(speaker), "v-1")
    assert stored(sql) == [{"stored": False}]


def test_trx08_create_voiceprints_skips_a_speaker_already_submitted(uploaded: str, sql: Sql) -> None:
    speaker = ready_with_speaker(sql, uploaded)
    record(sql, speaker, "v-1")
    sql("UPDATE devices SET voiceprint_consent = true")
    vendors = Vendors(diarization=[{"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0}])
    run(worker.create_voiceprints, vendors, uploaded)
    assert vendors.clips == []


def test_trx08_create_voiceprints_checks_each_voiceprint_a_minute_later(uploaded: str, sql: Sql) -> None:
    speaker = ready_with_speaker(sql, uploaded)
    sql("UPDATE devices SET voiceprint_consent = true")
    vendors = Vendors(diarization=[{"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0}])
    run(worker.create_voiceprints, vendors, uploaded)
    refs = json.loads(sql("SELECT vendor_refs FROM transcription_jobs")[0]["vendor_refs"])
    assert (len(vendors.clips), refs["voiceprints"]) == (1, {str(speaker): "v-made"})
    assert f"vp-check:{speaker}" in queued_arq_jobs()


def test_trx01_a_vendor_refusing_the_result_fails_the_merge_instead_of_leaving_it(processing: str, sql: Sql) -> None:
    sql("UPDATE transcription_jobs SET stage = 'merging'")
    vendors = Vendors(VendorRefused("assemblyai: 404"))
    run(worker.merge_and_store, vendors, processing)
    assert sql("SELECT state, failure_reason FROM transcription_jobs") == [
        {"state": "failed", "failure_reason": "vendor_error"}
    ]
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 0}]


@pytest.mark.parametrize("stage", ["transcribing", "diarizing", "merging"])
def test_trx01_the_sweep_fails_a_job_processing_past_its_deadline_in_any_stage(
    processing: str, sql: Sql, stage: str
) -> None:
    # 60 s of audio: the deadline is 15 min after the submit, and the sweep allows 5 min more.
    sql("UPDATE transcription_jobs SET stage = $1, submitted_at = now() - interval '21 minutes'", stage)
    vendors = Vendors()
    run(worker.sweep_waiting_jobs, vendors)
    assert sql("SELECT state, failure_reason FROM transcription_jobs") == [
        {"state": "failed", "failure_reason": "vendor_timeout"}
    ]
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 0}]
    assert vendors.cleaned == [REFS]


def test_trx01_the_sweep_leaves_a_job_still_within_its_deadline(processing: str, sql: Sql) -> None:
    sql("UPDATE transcription_jobs SET stage = 'merging', submitted_at = now() - interval '19 minutes'")
    run(worker.sweep_waiting_jobs, Vendors())
    assert sql("SELECT state FROM transcription_jobs") == [{"state": "processing"}]


def test_trx01_the_sweep_starts_again_a_job_left_queued_with_its_audio(uploaded: str, sql: Sql) -> None:
    sql("UPDATE transcription_jobs SET updated_at = now() - interval '6 minutes'")
    run(worker.sweep_waiting_jobs, Vendors())
    assert [job for job in queued_arq_jobs() if job.startswith(f"start:{uploaded}:")] != []


def test_trx01_the_sweep_leaves_a_job_whose_audio_just_arrived(uploaded: str, sql: Sql) -> None:
    run(worker.sweep_waiting_jobs, Vendors())
    assert [job for job in queued_arq_jobs() if job.startswith(f"start:{uploaded}:")] == []


def test_cld04_starting_a_job_whose_recording_was_deleted_fails_it_and_refunds(
    api: Any, uploaded: str, sql: Sql
) -> None:
    sql("UPDATE recordings SET deleted_at = now()")
    sql("UPDATE transcription_jobs SET deleted_at = now()")
    vendors = Vendors(AssertionError("submitted a deleted recording"))
    run(worker.start_transcription, vendors, uploaded)
    assert sql("SELECT state, failure_reason FROM transcription_jobs") == [
        {"state": "failed", "failure_reason": "internal"}
    ]
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 0}]


def test_trx01_the_sweep_fails_a_job_queued_with_its_audio_for_a_day(uploaded: str, sql: Sql) -> None:
    sql(
        "UPDATE transcription_jobs SET created_at = now() - interval '25 hours',"
        " updated_at = now() - interval '25 hours'"
    )
    sql("UPDATE usage_daily SET day = (SELECT (created_at AT TIME ZONE 'UTC')::date FROM transcription_jobs)")
    run(worker.sweep_waiting_jobs, Vendors())
    assert sql("SELECT state, failure_reason FROM transcription_jobs") == [
        {"state": "failed", "failure_reason": "internal"}
    ]
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 0}]
    assert [job for job in queued_arq_jobs() if job.startswith(f"start:{uploaded}:")] == []


def test_trx01_a_start_that_loses_the_race_deletes_the_vendor_jobs_it_made(
    uploaded: str, sql: Sql, cloud: dict[str, str]
) -> None:
    vendors = Vendors()

    async def another_writer() -> None:
        await admin_on(cloud["DATABASE_URL"], "UPDATE transcription_jobs SET version = version + 1")

    vendors.while_submitting = another_writer
    run(worker.start_transcription, vendors, uploaded)
    assert sql("SELECT state FROM transcription_jobs") == [{"state": "queued"}]
    assert vendors.cleaned == [REFS]


def test_trx08_a_voiceprint_whose_vendor_stays_down_gives_up_with_a_log(
    uploaded: str, sql: Sql, caplog: pytest.LogCaptureFixture
) -> None:
    speaker = ready_with_speaker(sql, uploaded)
    record(sql, speaker, "v-1")
    sql("UPDATE devices SET voiceprint_consent = true")

    class Down(Vendors):
        async def voiceprint_output(self, vendor_job: str) -> str:
            raise VendorUnavailable("pyannote: 503")

    with pytest.raises(Retry):
        run(worker.on_voiceprint, Down(), uploaded, str(speaker), "v-1")
    with caplog.at_level(logging.INFO, logger="snapbak_backend"):
        run(worker.on_voiceprint, Down(), uploaded, str(speaker), "v-1", job_try=worker.MAX_TRIES)
    assert stored(sql) == [{"stored": False}]
    lines = [record.getMessage() for record in caplog.records if "task=on_voiceprint" in record.getMessage()]
    shape = rf"task=on_voiceprint job={speaker} vendor=pyannote ms=\d+ verdict=gave_up"
    assert [bool(re.fullmatch(shape, line)) for line in lines] == [True]


def test_trx01_the_timeout_check_leaves_a_merging_job_to_the_merge_even_past_its_deadline(
    processing: str, sql: Sql
) -> None:
    sql("UPDATE transcription_jobs SET stage = 'merging', submitted_at = now() - interval '30 minutes'")
    run(worker.check_vendor_timeout, Vendors(), processing, 4)
    assert sql("SELECT state, stage FROM transcription_jobs") == [{"state": "processing", "stage": "merging"}]
    assert queued_arq_jobs() == []


def test_cld04_a_recording_trashed_while_the_submit_retries_deletes_the_vendor_job_already_made(
    api: Any, sql: Sql
) -> None:
    auth = register(api)
    put_recording(api, auth, duration_ms=60_000)
    job = create_job(api, auth).json()["jobId"]
    upload_audio(api, auth, b"m4a")
    vendors = Vendors(partial(VendorUnavailable("pyannote: 503")))
    with pytest.raises(Retry):
        run(worker.start_transcription, vendors, job)
    assert api.delete(f"/v1/recordings/{RECORDING}", headers=auth).status_code == 204
    run(worker.start_transcription, vendors, job)
    assert sql("SELECT state, failure_reason FROM transcription_jobs") == [
        {"state": "failed", "failure_reason": "internal"}
    ]
    assert vendors.cleaned == [{"language": "auto", "assemblyai": {"id": "t-1", "status": "running"}}]


def test_cld04_a_recording_trashed_while_it_processes_fails_at_the_merge_and_deletes_the_vendor_copies(
    api: Any, sql: Sql
) -> None:
    auth = register(api)
    put_recording(api, auth, duration_ms=60_000)
    job = create_job(api, auth).json()["jobId"]
    sql(
        "UPDATE transcription_jobs SET state = 'processing', stage = 'merging', vendor_refs = $1, submitted_at = now()",
        json.dumps(REFS),
    )
    assert api.delete(f"/v1/recordings/{RECORDING}", headers=auth).status_code == 204
    # The delete leaves a processing job to fail itself, where the vendors' copies are deleted.
    assert sql("SELECT state FROM transcription_jobs") == [{"state": "processing"}]
    vendors = Vendors()
    run(worker.merge_and_store, vendors, job)
    assert sql("SELECT state, failure_reason FROM transcription_jobs") == [
        {"state": "failed", "failure_reason": "internal"}
    ]
    assert vendors.cleaned == [REFS]
    assert sql("SELECT count(*) AS n FROM transcript_lines") == [{"n": 0}]


def test_cld04_a_last_failed_submit_that_loses_to_a_delete_still_deletes_the_vendor_that_accepted(
    uploaded: str, sql: Sql, cloud: dict[str, str]
) -> None:
    vendors = Vendors(partial(VendorUnavailable("pyannote: 503")))

    async def trashed() -> None:
        failed = "UPDATE transcription_jobs SET state = 'failed', failure_reason = 'internal', version = version + 1"
        await admin_on(cloud["DATABASE_URL"], failed)

    vendors.while_submitting = trashed
    run(worker.start_transcription, vendors, uploaded, job_try=worker.MAX_TRIES)
    assert vendors.cleaned == [{"language": "auto", "assemblyai": {"id": "t-1", "status": "running"}}]


def test_cld04_a_merge_racing_the_trash_of_its_recording_fails_and_writes_no_lines(
    api: Any, processing: str, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete holds the recording's row while the merge reaches it: the merge waits, reads the recording again,
    sees it trashed, fails the job and deletes the vendors' copies."""
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from snapbak_backend.services import retention

    auth = register(api)
    real = retention.on_recording_deleted
    entered = threading.Event()

    async def slow(*args: Any) -> None:
        entered.set()
        await asyncio.sleep(0.3)
        await real(*args)

    monkeypatch.setattr(retention, "on_recording_deleted", slow)
    vendors = Vendors()
    with ThreadPoolExecutor(2) as pool:
        trashed = pool.submit(api.delete, f"/v1/recordings/{RECORDING}", headers=auth)
        assert entered.wait(10)
        merged = pool.submit(run, worker.merge_and_store, vendors, processing)
        assert trashed.result().status_code == 204
        merged.result()
    assert sql("SELECT state, failure_reason FROM transcription_jobs") == [
        {"state": "failed", "failure_reason": "internal"}
    ]
    assert sql("SELECT count(*) AS n FROM transcript_lines WHERE deleted_at IS NULL") == [{"n": 0}]
    assert vendors.cleaned == [REFS]


def test_trx01_a_merge_whose_job_failed_while_it_waited_returns_and_writes_nothing(
    api: Any, processing: str, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another writer holds the recording's row and fails the job; the merge, waiting on that row, reads the job
    again and stops."""
    import threading

    import asyncpg

    whole = load(os.environ)
    config = whole.cloud
    assert config is not None
    url = config.database_url.replace("+asyncpg", "")
    holding = threading.Event()

    async def fail_under_the_lock() -> None:
        connection = await asyncpg.connect(url)
        try:
            async with connection.transaction():
                await connection.execute("SELECT 1 FROM recordings FOR UPDATE")
                holding.set()
                await asyncio.sleep(0.3)
                await connection.execute(
                    "UPDATE transcription_jobs SET state = 'failed', failure_reason = 'vendor_timeout',"
                    " version = version + 1"
                )
        finally:
            await connection.close()

    other = threading.Thread(target=asyncio.run, args=(fail_under_the_lock(),))
    other.start()
    assert holding.wait(10)
    run(worker.merge_and_store, Vendors(), processing)
    other.join()
    assert sql("SELECT state, failure_reason FROM transcription_jobs") == [
        {"state": "failed", "failure_reason": "vendor_timeout"}
    ]
    assert sql("SELECT count(*) AS n FROM transcript_lines") == [{"n": 0}]


def test_trx01_a_generate_while_the_worker_fails_the_recordings_job_waits_its_turn(
    api: Any, processing: str, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker locks the recording before it fails the job and refunds its minutes; a generate, which reserves
    minutes and then inserts a job, waits for it instead of deadlocking."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from snapbak_backend.services import usage

    auth = register(api)
    real = usage.refund
    entered = threading.Event()

    async def slow(*args: Any) -> None:
        entered.set()
        await asyncio.sleep(0.3)
        await real(*args)

    monkeypatch.setattr(usage, "refund", slow)
    with ThreadPoolExecutor(2) as pool:
        failing = pool.submit(run, worker.check_vendor_timeout, Vendors(assemblyai="failed"), processing, 1)
        assert entered.wait(10)
        generated = pool.submit(create_job, api, auth)
        failing.result()
        assert generated.result().status_code == 201
    assert sorted(row["state"] for row in sql("SELECT state FROM transcription_jobs")) == ["failed", "queued"]


def test_trx01_a_fail_that_lost_to_another_writer_lets_the_recordings_lock_go(
    processing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep fails many jobs in one session, and a vendor cleanup may follow a lost fail: neither may hold the
    recording's lock meanwhile."""
    import asyncpg

    from snapbak_backend.db.models import TranscriptionJob

    ctx_strategy: list[Vendors] = []

    async def call() -> tuple[bool, bool]:
        whole = load(os.environ)
        config = whole.cloud
        assert config is not None
        cloud = await open_cloud(config, whole.keys)
        try:
            ctx = {"cloud": cloud, "strategy": ctx_strategy[0], "job_try": 1, "redis": cloud.redis, "cut": fake_cut}
            async with cloud.sessions() as db:
                job = await db.get_one(TranscriptionJob, uuid.UUID(processing))
                other = await asyncpg.connect(config.database_url.replace("+asyncpg", ""))
                await other.execute("UPDATE transcription_jobs SET version = version + 1")
                won = await worker.fail_and_publish(ctx, db, job, "vendor_timeout", "test")
                try:
                    async with other.transaction():
                        await other.execute("SELECT 1 FROM recordings FOR NO KEY UPDATE NOWAIT")
                    free = True
                except asyncpg.LockNotAvailableError:
                    free = False
                finally:
                    await other.close()
                return won, free
        finally:
            await cloud.close()

    published: list[Any] = []

    async def note(*args: Any) -> None:
        published.append(args)

    monkeypatch.setattr(worker, "publish", note)
    vendors = Vendors()
    ctx_strategy.append(vendors)
    assert asyncio.run(call()) == (False, True)
    assert (published, vendors.cleaned) == ([], [])


def test_trx10_a_line_edit_racing_a_merge_that_replaces_the_lines_is_not_found(
    api: Any, processing: str, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merge run again replaces the lines a crashed run left, under the recording's lock; an edit to an old line
    waits for it, reads the line again, finds it gone, and answers 404."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    auth = register(api)
    recording = uuid.UUID(RECORDING)
    speaker, line = uuid.uuid4(), uuid.uuid4()
    sql(
        "INSERT INTO speakers (id, recording_id, label, vendor_label, color_idx) VALUES ($1, $2, 'Speaker 1', 'S0', 0)",
        speaker,
        recording,
    )
    sql(
        "INSERT INTO transcript_lines (id, recording_id, speaker_id, idx, start_ms, end_ms, text, words)"
        " VALUES ($1, $2, $3, 0, 0, 400, 'Hi.', '[]')",
        line,
        recording,
        speaker,
    )
    real = worker.store
    storing = threading.Event()

    async def slow(*args: Any) -> None:
        await real(*args)
        storing.set()
        await asyncio.sleep(0.3)

    monkeypatch.setattr(worker, "store", slow)
    words = [{"w": "Bye.", "s": 0, "e": 400, "filler": False, "conf": 0.9}]
    body = {"text": "Bye.", "words": words, "speakerId": str(speaker), "rev": 1}
    with ThreadPoolExecutor(2) as pool:
        merging = pool.submit(run, worker.merge_and_store, Vendors(), processing)
        assert storing.wait(10)
        edited = pool.submit(api.patch, f"/v1/lines/{line}", json=body, headers=auth)
        merging.result()
        assert failure(edited.result()) == (404, "not_found")
    assert sql("SELECT text FROM transcript_lines") == [{"text": "Hi."}]


def test_trx08_a_rename_racing_a_merge_that_replaces_the_speakers_is_not_found(
    api: Any, processing: str, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The speaker twin of the line race: the merge replaces the speakers a crashed run left; a rename of an old one
    waits for it, reads the speaker again, finds it gone, and answers 404."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    auth = register(api)
    speaker = uuid.uuid4()
    sql(
        "INSERT INTO speakers (id, recording_id, label, vendor_label, color_idx) VALUES ($1, $2, 'Speaker 1', 'S0', 0)",
        speaker,
        uuid.UUID(RECORDING),
    )
    real = worker.store
    storing = threading.Event()

    async def slow(*args: Any) -> None:
        await real(*args)
        storing.set()
        await asyncio.sleep(0.3)

    monkeypatch.setattr(worker, "store", slow)
    with ThreadPoolExecutor(2) as pool:
        merging = pool.submit(run, worker.merge_and_store, Vendors(), processing)
        assert storing.wait(10)
        renamed = pool.submit(api.patch, f"/v1/speakers/{speaker}", json={"name": "Ann"}, headers=auth)
        merging.result()
        assert failure(renamed.result()) == (404, "not_found")
    assert [row["name"] for row in sql("SELECT name FROM speakers")] == [None]
