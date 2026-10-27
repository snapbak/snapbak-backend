"""The vendor clients against the documented request and answer shapes (checked 17 Oct 2026), through respx."""

import json
import uuid
from typing import Any

import httpx
import pytest
import respx

from snapbak_backend.services.storage import Storage
from snapbak_backend.transcription.assemblyai_pyannote import AssemblyAiPyannoteStrategy
from snapbak_backend.transcription.strategy import JobInput, VendorRefused, VendorStatus, VendorUnavailable

JOB = JobInput(
    job_id=uuid.UUID("5d0a6e1c-8f3b-4a2d-9c7e-1b2a3c4d5e6f"),
    recording_id=uuid.UUID("8a1d4c52-0f0e-4d55-9a44-2f1a7e6c3b10"),
    audio_url="https://bucket.s3/audio.m4a?X-Amz-Signature=x",
    duration_ms=612_400,
    language="hinglish",
    webhooks={"assemblyai": "https://api/v1/webhooks/assemblyai?job=5d0a", "pyannote": "https://api/v1/webhooks/py"},
    webhook_auth="abc123",
)
REFS = {
    "language": "auto",
    "assemblyai": {"id": "t-1", "status": "running"},
    "pyannote": {"jobId": "p-1", "status": "running", "rawKey": "raw/r/j/pyannote-diarize.json"},
}


class Stored(Storage):
    """The bucket, held in memory."""

    def __init__(self, stored: Any = None) -> None:
        self.stored = stored
        self.put: list[tuple[str, Any]] = []

    async def get_json(self, key: str) -> Any:
        return self.stored

    async def put_json(self, key: str, data: Any) -> None:
        self.put.append((key, data))


def strategy(stored: Any = None) -> AssemblyAiPyannoteStrategy:
    return AssemblyAiPyannoteStrategy("aai-key", "py-key", Stored(stored))


@pytest.mark.anyio
async def test_trx01_submit_sends_both_vendors_their_documented_requests() -> None:
    with respx.mock() as mock:
        aai = mock.post("https://api.assemblyai.com/v2/transcript").respond(200, json={"id": "t-1", "status": "queued"})
        py = mock.post("https://api.pyannote.ai/v1/diarize").respond(200, json={"jobId": "p-1", "status": "created"})
        refs = await strategy().submit(JOB, {})
    sent = json.loads(aai.calls[0].request.content)
    assert aai.calls[0].request.headers["authorization"] == "aai-key"
    assert sent == {
        "audio_url": JOB.audio_url,
        "speech_models": ["universal-3-5-pro"],
        "language_codes": ["en", "hi"],
        "punctuate": True,
        "format_text": True,
        "disfluencies": True,
        "speaker_labels": True,
        "speech_understanding": {"request": {"speaker_identification": {"speaker_type": "name"}}},
        "webhook_url": JOB.webhooks["assemblyai"],
        "webhook_auth_header_name": "X-Snapbak-Webhook",
        "webhook_auth_header_value": "abc123",
    }
    assert py.calls[0].request.headers["authorization"] == "Bearer py-key"
    assert json.loads(py.calls[0].request.content) == {
        "url": JOB.audio_url,
        "model": "precision-2",
        "exclusive": True,
        "webhook": JOB.webhooks["pyannote"],
    }
    assert refs == {
        "language": "hinglish",
        "assemblyai": {"id": "t-1", "status": "running"},
        "pyannote": {
            "jobId": "p-1",
            "status": "running",
            "rawKey": f"raw/{JOB.recording_id}/{JOB.job_id}/pyannote-diarize.json",
        },
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "error"), [(500, VendorUnavailable), (429, VendorUnavailable), (400, VendorRefused)]
)
async def test_trx01_a_vendor_5xx_or_429_is_asked_again_and_a_4xx_is_final(status: int, error: type) -> None:
    with respx.mock() as mock:
        mock.post("https://api.assemblyai.com/v2/transcript").respond(status)
        mock.post("https://api.pyannote.ai/v1/diarize").respond(200, json={"jobId": "p-1", "status": "created"})
        with pytest.raises(error):
            await strategy().submit(JOB, {})


@pytest.mark.anyio
async def test_trx01_a_network_failure_is_asked_again() -> None:
    with respx.mock() as mock:
        mock.post("https://api.assemblyai.com/v2/transcript").mock(side_effect=httpx.ConnectError("down"))
        mock.post("https://api.pyannote.ai/v1/diarize").respond(200, json={"jobId": "p-1", "status": "created"})
        with pytest.raises(VendorUnavailable):
            await strategy().submit(JOB, {})


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"status": "completed"}, VendorStatus("done")),
        ({"status": "processing"}, VendorStatus("running")),
        (
            {"status": "error", "error": "Language detection failed: unsupported language"},
            VendorStatus("failed", "unsupported_language"),
        ),
        ({"status": "error", "error": "No spoken audio found"}, VendorStatus("failed", "no_speech")),
        (
            {"status": "error", "error": "Download error, unable to download the file"},
            VendorStatus("failed", "upload_invalid"),
        ),
        ({"status": "error", "error": "Internal"}, VendorStatus("failed", "vendor_error")),
    ],
)
async def test_trx01_assemblyai_status_maps_to_a_vendor_state(answer: dict, expected: VendorStatus) -> None:
    with respx.mock() as mock:
        mock.get("https://api.assemblyai.com/v2/transcript/t-1").respond(200, json={"id": "t-1", **answer})
        assert await strategy().status(REFS, "assemblyai") == expected


@pytest.mark.anyio
async def test_trx01_pyannote_is_done_once_its_output_is_stored() -> None:
    with respx.mock() as mock:
        assert await strategy({"exclusiveDiarization": []}).status(REFS, "pyannote") == VendorStatus("done")
        assert not mock.calls


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("running", VendorStatus("running")),
        ("failed", VendorStatus("failed", "vendor_error")),
        ("canceled", VendorStatus("failed", "vendor_error")),
    ],
)
async def test_trx01_pyannote_status_falls_back_to_the_job(status: str, expected: VendorStatus) -> None:
    with respx.mock() as mock:
        mock.get("https://api.pyannote.ai/v1/jobs/p-1").respond(200, json={"jobId": "p-1", "status": status})
        assert await strategy().status(REFS, "pyannote") == expected


@pytest.mark.anyio
async def test_trx01_a_late_pyannote_success_is_stored_before_its_output_expires() -> None:
    bucket = Stored()
    made = AssemblyAiPyannoteStrategy("aai-key", "py-key", bucket)
    output = {"exclusiveDiarization": [{"speaker": "SPEAKER_00", "start": 0.5, "end": 1.0}]}
    with respx.mock() as mock:
        mock.get("https://api.pyannote.ai/v1/jobs/p-1").respond(200, json={"status": "succeeded", "output": output})
        assert await made.status(REFS, "pyannote") == VendorStatus("done")
    assert bucket.put == [("raw/r/j/pyannote-diarize.json", output)]


@pytest.mark.anyio
async def test_trx01_result_merges_the_transcript_with_the_stored_turns() -> None:
    transcript = {
        "status": "completed",
        "language_code": "en_us",
        "audio_duration": 3,
        "words": [{"text": "Hi.", "start": 500, "end": 900, "confidence": 0.9, "speaker": "A"}],
    }
    stored = {"exclusiveDiarization": [{"speaker": "SPEAKER_00", "start": 0.4, "end": 1.0}]}
    with respx.mock() as mock:
        mock.get("https://api.assemblyai.com/v2/transcript/t-1").respond(200, json=transcript)
        merged = await strategy(stored).result(REFS)
    assert [[w.w for w in line] for line in merged.lines] == [["Hi."]]
    assert merged.speakers[0].key == "SPEAKER_00"


@pytest.mark.anyio
async def test_trx08_voiceprints_start_with_a_webhook_and_read_the_output() -> None:
    with respx.mock() as mock:
        started = mock.post("https://api.pyannote.ai/v1/voiceprint").respond(200, json={"jobId": "v-1"})
        mock.get("https://api.pyannote.ai/v1/jobs/v-1").respond(
            200, json={"status": "succeeded", "output": {"voiceprint": "QUJD"}}
        )
        made = strategy()
        vendor_job = await made.voiceprint("https://clip", "https://hook")
        output = await made.voiceprint_output(vendor_job)
    assert json.loads(started.calls[0].request.content) == {"url": "https://clip", "webhook": "https://hook"}
    assert (vendor_job, output) == ("v-1", "QUJD")


@pytest.mark.anyio
async def test_trx01_cleanup_deletes_the_assemblyai_copy_and_ignores_failures() -> None:
    with respx.mock() as mock:
        deleted = mock.delete("https://api.assemblyai.com/v2/transcript/t-1").respond(500)
        await strategy().cleanup(REFS)
    assert deleted.called


@pytest.mark.anyio
async def test_trx01_when_one_vendor_fails_the_other_ones_job_is_kept_and_not_submitted_again() -> None:
    with respx.mock() as mock:
        aai = mock.post("https://api.assemblyai.com/v2/transcript").respond(200, json={"id": "t-1", "status": "queued"})
        py = mock.post("https://api.pyannote.ai/v1/diarize").respond(503)
        made = strategy()
        with pytest.raises(VendorUnavailable) as failed:
            await made.submit(JOB, {})
        py.respond(200, json={"jobId": "p-1", "status": "created"})
        refs = await made.submit(JOB, failed.value.refs)
    assert failed.value.refs == {"language": "hinglish", "assemblyai": {"id": "t-1", "status": "running"}}
    assert (aai.call_count, py.call_count) == (1, 2)
    assert refs["pyannote"]["jobId"] == "p-1"


@pytest.mark.anyio
async def test_trx01_a_refusal_wins_over_an_outage_so_the_job_fails_instead_of_retrying() -> None:
    with respx.mock() as mock:
        mock.post("https://api.assemblyai.com/v2/transcript").respond(503)
        mock.post("https://api.pyannote.ai/v1/diarize").respond(400)
        with pytest.raises(VendorRefused):
            await strategy().submit(JOB, {})


@pytest.mark.anyio
async def test_trx01_cleanup_without_an_assemblyai_job_asks_nothing() -> None:
    with respx.mock() as mock:
        await strategy().cleanup({"language": "auto", "pyannote": {"jobId": "p-1"}})
    assert not mock.calls


@pytest.mark.anyio
@pytest.mark.parametrize("answer", [{"content": b"<html>busy</html>"}, {"json": {"status": "created"}}])
async def test_trx01_a_garbled_vendor_answer_is_an_outage_that_keeps_the_other_vendor(answer: dict) -> None:
    with respx.mock() as mock:
        mock.post("https://api.assemblyai.com/v2/transcript").respond(200, json={"id": "t-1", "status": "queued"})
        mock.post("https://api.pyannote.ai/v1/diarize").respond(200, **answer)
        with pytest.raises(VendorUnavailable) as failed:
            await strategy().submit(JOB, {})
    assert failed.value.refs["assemblyai"] == {"id": "t-1", "status": "running"}
    assert "pyannote" not in failed.value.refs


@pytest.mark.anyio
async def test_mom08_a_rerun_asks_pyannote_for_the_voices_counted_and_reads_its_turns_when_done() -> None:
    with respx.mock() as mock:
        asked = mock.post("https://api.pyannote.ai/v1/diarize").respond(200, json={"jobId": "p-2", "status": "created"})
        job = await strategy().rediarize(JOB.audio_url, 4)
    assert job == "p-2"
    assert json.loads(asked.calls[0].request.content) == {
        "url": JOB.audio_url,
        "model": "precision-2",
        "exclusive": True,
        "numSpeakers": 4,
    }
    turns = [{"speaker": "SPEAKER_01", "start": 0.5, "end": 1.0}]
    with respx.mock() as mock:
        polled = mock.get("https://api.pyannote.ai/v1/jobs/p-2")
        polled.respond(200, json={"status": "running"})
        assert await strategy().rediarization("p-2") is None
        polled.respond(200, json={"status": "succeeded", "output": {"exclusiveDiarization": turns}})
        assert await strategy().rediarization("p-2") == turns
        polled.respond(200, json={"status": "failed"})
        with pytest.raises(VendorRefused):
            await strategy().rediarization("p-2")


@pytest.mark.anyio
async def test_mom08_identify_sends_every_speaker_s_voiceprint_and_reads_turns_and_confidences() -> None:
    with respx.mock() as mock:
        asked = mock.post("https://api.pyannote.ai/v1/identify").respond(
            200, json={"jobId": "p-3", "status": "created"}
        )
        job = await strategy().identify(JOB.audio_url, {"p1": "vp-one", "p4": "vp-four"})
    assert job == "p-3"
    assert json.loads(asked.calls[0].request.content) == {
        "url": JOB.audio_url,
        "model": "precision-2",
        "exclusive": True,
        "voiceprints": [{"label": "p1", "voiceprint": "vp-one"}, {"label": "p4", "voiceprint": "vp-four"}],
        "matching": {"exclusive": False, "threshold": 0},
    }
    output = {
        "exclusiveDiarization": [{"speaker": "SPEAKER_00", "start": 0.5, "end": 1.0}],
        "voiceprints": [{"speaker": "SPEAKER_00", "match": "p1", "confidence": {"p1": 86, "p4": 81}}],
    }
    with respx.mock() as mock:
        polled = mock.get("https://api.pyannote.ai/v1/jobs/p-3")
        polled.respond(200, json={"status": "running"})
        assert await strategy().identification("p-3") is None
        polled.respond(200, json={"status": "succeeded", "output": output})
        assert await strategy().identification("p-3") == output
