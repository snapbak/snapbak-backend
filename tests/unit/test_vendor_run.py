"""The vendor run both `test_cloud.py` and `eval_transcript.py` use (13 LLD, Evals): one file through both vendors,
and the answers kept as fixtures. The vendors, the bucket and the clock are fakes."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import vendor_run


class Bucket:
    """Records what is uploaded and which keys are tagged for the bucket's day-later delete."""

    def __init__(self, output: Any = None) -> None:
        self.uploaded: list[str] = []
        self.types: list[str] = []
        self.expiries: list[int] = []
        self.superseded: list[str] = []
        self.output = {"exclusiveDiarization": []} if output is None else output

    async def upload(self, path: str, key: str, content_type: str) -> None:
        self.uploaded.append(key)
        self.types.append(content_type)

    def presign_get(self, key: str, expires_s: int) -> str:
        self.expiries.append(expires_s)
        return f"https://bucket.invalid/{key}"

    async def get_json(self, key: str) -> Any:
        return self.output

    async def supersede(self, keys: list[str]) -> None:
        self.superseded += keys


class Vendors:
    """Both vendors done at once, or pyannote after `slow` polls; `fail` makes the status call raise."""

    def __init__(self, fail: bool = False, slow: int = 0) -> None:
        self.fail = fail
        self.slow = slow
        self.polls: dict[str, int] = {}
        self.assemblyai = SimpleNamespace(delete=self.delete)

    async def delete(self, path: str) -> Any:
        return SimpleNamespace(status_code=204)

    async def submit(self, job: Any, refs: dict[str, Any]) -> dict[str, Any]:
        return {"assemblyai": {"id": "aai-1"}, "pyannote": {"rawKey": "raw/key"}}

    async def status(self, refs: dict[str, Any], vendor: str) -> Any:
        if self.fail:
            raise RuntimeError("vendor down")
        self.polls[vendor] = self.polls.get(vendor, 0) + 1
        running = vendor == "pyannote" and self.polls[vendor] <= self.slow
        return SimpleNamespace(state="running" if running else "done", failure=None)


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    cloud = SimpleNamespace(public_url="https://api.invalid", webhook_secret="secret")
    monkeypatch.setattr(vendor_run, "load", lambda _: SimpleNamespace(cloud=cloud))
    monkeypatch.setattr(vendor_run, "POLL_S", 0)

    async def call(client: Any, method: str, path: str, body: Any = None) -> Any:
        return {"words": []}

    monkeypatch.setattr(vendor_run, "call", call)


def test_trx01_the_audio_it_uploaded_is_tagged_for_deletion_once_both_answers_are_in(tmp_path: Path) -> None:
    bucket, audio = Bucket(), tmp_path / "talk.wav"
    audio.write_bytes(b"RIFF")
    elapsed, transcript, output = asyncio.run(vendor_run.heard(Vendors(), bucket, audio))  # type: ignore[arg-type]
    assert (transcript, output) == ({"words": []}, {"exclusiveDiarization": []})
    assert elapsed >= 0
    assert len(bucket.uploaded) == 1 and bucket.uploaded[0].startswith("test-cloud/")
    assert bucket.uploaded[0].endswith(".wav")
    assert bucket.superseded == bucket.uploaded


def test_trx01_the_audio_is_tagged_even_when_a_vendor_fails(tmp_path: Path) -> None:
    bucket, audio = Bucket(), tmp_path / "talk.wav"
    audio.write_bytes(b"RIFF")
    with pytest.raises(RuntimeError, match="vendor down"):
        asyncio.run(vendor_run.heard(Vendors(fail=True), bucket, audio))  # type: ignore[arg-type]
    assert bucket.superseded == bucket.uploaded and len(bucket.uploaded) == 1


def test_trx01_the_answers_are_kept_as_the_fake_strategys_fixtures(tmp_path: Path) -> None:
    vendor_run.save_answers(tmp_path / "talk", {"text": "héllo"}, {"exclusiveDiarization": []})
    assert json.loads((tmp_path / "talk" / "assemblyai.json").read_text()) == {"text": "héllo"}
    assert "héllo" in (tmp_path / "talk" / "assemblyai.json").read_text()
    assert json.loads((tmp_path / "talk" / "pyannote.json").read_text()) == {"exclusiveDiarization": []}


def test_trx01_extra_words_against_an_empty_reference_are_a_full_error() -> None:
    assert vendor_run.word_error_rate("", "extra") == 1.0


def test_trx01_a_vendor_that_answered_is_not_asked_again_and_the_time_is_to_the_last_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "talk.wav"
    audio.write_bytes(b"RIFF")
    # Started at 0 s, AssemblyAI done at 10 s, pyannote at 20 s, both fetched by 25 s.
    clock = iter([0.0, 10.0, 20.0, 25.0])
    monkeypatch.setattr(vendor_run, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    vendors = Vendors(slow=1)
    elapsed, _, _ = asyncio.run(vendor_run.heard(vendors, Bucket(), audio))  # type: ignore[arg-type]
    assert vendors.polls == {"assemblyai": 1, "pyannote": 2}
    assert elapsed == 25.0


def test_trx01_the_kept_answers_end_with_a_newline_like_the_fixtures_beside_them(tmp_path: Path) -> None:
    vendor_run.save_answers(tmp_path / "talk", {"words": []}, {"exclusiveDiarization": []})
    assert (tmp_path / "talk" / "assemblyai.json").read_text().endswith("}\n")
    assert (tmp_path / "talk" / "pyannote.json").read_text().endswith("}\n")


def test_trx01_deleted_words_count_as_errors() -> None:
    assert vendor_run.word_error_rate("a b c d", "a d") == 0.5
    assert vendor_run.word_error_rate("a b", "") == 1.0


def test_trx01_the_audio_goes_up_with_its_own_type_and_a_link_that_lives_two_hours(tmp_path: Path) -> None:
    for name, kind in [("talk.wav", "audio/wav"), ("talk.m4a", "audio/mp4")]:
        audio, bucket = tmp_path / name, Bucket()
        audio.write_bytes(b"RIFF")
        asyncio.run(vendor_run.heard(Vendors(), bucket, audio))  # type: ignore[arg-type]
        assert (bucket.types, bucket.expiries) == ([kind], [7200])


def test_trx01_a_pyannote_answer_that_is_missing_reads_as_empty(tmp_path: Path) -> None:
    class Gone(Bucket):
        async def get_json(self, key: str) -> Any:
            return None

    audio = tmp_path / "talk.wav"
    audio.write_bytes(b"RIFF")
    _, _, output = asyncio.run(vendor_run.heard(Vendors(), Gone(), audio))  # type: ignore[arg-type]
    assert output == {}


def test_trx01_the_vendors_are_asked_every_two_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the real interval, not the suite's zero
    waits: list[float] = []

    async def asleep(seconds: float) -> None:
        waits.append(seconds)

    cloud = SimpleNamespace(public_url="https://api.invalid", webhook_secret="secret")
    monkeypatch.setattr(vendor_run, "load", lambda _: SimpleNamespace(cloud=cloud))
    monkeypatch.setattr(vendor_run.asyncio, "sleep", asleep)

    async def call(client: Any, method: str, path: str, body: Any = None) -> Any:
        return {"words": []}

    monkeypatch.setattr(vendor_run, "call", call)
    audio = tmp_path / "talk.wav"
    audio.write_bytes(b"RIFF")
    asyncio.run(vendor_run.heard(Vendors(slow=1), Bucket(), audio))  # type: ignore[arg-type]
    assert waits == [2, 2]


def test_trx01_an_apostrophe_is_part_of_a_word() -> None:
    assert vendor_run.word_error_rate("don't", "don t") == 2.0


def test_trx01_the_run_says_when_each_vendor_answered_and_what_the_delete_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    audio = tmp_path / "talk.wav"
    audio.write_bytes(b"RIFF")
    clock = iter([0.0, 10.0, 20.0, 25.0])
    monkeypatch.setattr(vendor_run, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    asyncio.run(vendor_run.heard(Vendors(slow=1), Bucket(), audio))  # type: ignore[arg-type]
    said = capsys.readouterr().out
    assert "assemblyai: done  after 10.0 s" in said
    assert "pyannote: done  after 20.0 s" in said
    assert "answered after 25.0 s; AssemblyAI delete answered 204" in said
