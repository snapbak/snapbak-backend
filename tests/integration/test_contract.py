import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from snapbak_backend.ai import languages
from snapbak_backend.schemas import cloud, translations
from snapbak_backend.schemas.command import CommandRequest, CommandResponse
from snapbak_backend.schemas.polish import PolishRequest, PolishResponse

CONTRACT = Path(__file__).parent.parent / "contract"
APP_CONTRACT = Path(__file__).parents[3] / "snapbak" / "apps" / "desktop" / "contracts" / "backend"
# The correction cues: canonical here, embedded by the app from its byte-identical copy (15 Oct).
CUES = Path(__file__).parents[2] / "src" / "snapbak_backend" / "ai" / "cues.json"


@pytest.mark.skipif(not APP_CONTRACT.is_dir(), reason="the app repo is not checked out next to this one")
def test_each_fixture_is_byte_identical_to_the_apps() -> None:
    ours = {path.name: path.read_bytes() for path in [*CONTRACT.glob("*.json"), CUES]}
    apps = {path.name: path.read_bytes() for path in APP_CONTRACT.glob("*.json")}
    assert ours == apps


@pytest.mark.parametrize(
    ("fixture", "model"),
    [
        ("polish.request.json", PolishRequest),
        ("polish.response.json", PolishResponse),
        ("command.request.json", CommandRequest),
        ("command.response.json", CommandResponse),
    ],
)
def test_each_fixture_is_exactly_its_model(fixture: str, model: type[BaseModel]) -> None:
    data = json.loads((CONTRACT / fixture).read_text())
    parsed = model.model_validate(data)
    assert parsed.model_dump() == data
    assert set(data) == set(model.model_fields)


def test_the_error_fixture_is_one_code() -> None:
    data = json.loads((CONTRACT / "error.response.json").read_text())
    assert data == {"error": "unauthorized"}


@pytest.mark.parametrize(
    ("fixture", "model"),
    [
        ("cloud.error.response.json", cloud.CloudError),
        ("devices.request.json", cloud.DeviceRequest),
        ("devices.response.json", cloud.DeviceResponse),
        ("consent.request.json", cloud.ConsentRequest),
        ("consent.response.json", cloud.ConsentResponse),
        ("recording.request.json", cloud.RecordingRequest),
        ("recording.response.json", cloud.RecordingResponse),
        ("upload.request.json", cloud.UploadRequest),
        ("upload.response.json", cloud.UploadResponse),
        ("upload-parts.request.json", cloud.PartsRequest),
        ("upload-complete.request.json", cloud.CompleteRequest),
        ("upload-complete.response.json", cloud.CompleteResponse),
        ("transcription.request.json", cloud.TranscriptionRequest),
        ("transcription.response.json", cloud.TranscriptionResponse),
        ("transcription.conflict.json", cloud.JobConflict),
        ("job.response.json", cloud.JobResponse),
        ("transcript.response.json", cloud.TranscriptResponse),
        ("line.request.json", cloud.LineRequest),
        ("line.response.json", cloud.LineResponse),
        ("speaker-name.request.json", cloud.SpeakerNameRequest),
        ("speaker.request.json", cloud.SpeakerRequest),
        ("speaker.response.json", cloud.SpeakerOut),
        ("speaker-change.request.json", cloud.SpeakerChangeRequest),
        ("speaker-pass.response.json", cloud.SpeakerPass),
        ("speaker-edits.request.json", cloud.SpeakerEditsRequest),
        ("speaker-edits.response.json", cloud.SpeakerEditsResponse),
        ("transliterate.request.json", cloud.TransliterateRequest),
        ("transliterate.response.json", cloud.TransliterateResponse),
        ("share.request.json", cloud.ShareRequest),
        ("share.response.json", cloud.ShareResponse),
        ("share-update.request.json", cloud.ShareUpdateRequest),
        ("share-complete.response.json", cloud.ShareCompleteResponse),
        ("upload-share.request.json", cloud.ShareUploadRequest),
        ("upload-gif.request.json", cloud.GifUploadRequest),
        ("meeting.request.json", cloud.MeetingRequest),
        ("meeting.response.json", cloud.MeetingAccepted),
        ("meeting-detail.response.json", cloud.MeetingResponse),
        ("minutes-edit.request.json", cloud.MinutesEditRequest),
        ("minutes-edit.response.json", cloud.MeetingResponse),
        ("minutes-regenerate.request.json", cloud.RegenerateRequest),
        ("pin.request.json", cloud.PinRequest),
        ("pin.response.json", cloud.PinOut),
        ("ask.request.json", cloud.AskRequest),
        ("ask.response.json", cloud.AskResponse),
        ("meetings-search.request.json", cloud.SearchRequest),
        ("meetings-search.response.json", cloud.SearchResponse),
    ],
)
def test_each_cloud_fixture_is_exactly_its_model(fixture: str, model: type[BaseModel]) -> None:
    """Cloud bodies carry UUIDs and times, so they compare as JSON."""
    data = json.loads((CONTRACT / fixture).read_text())
    parsed = model.model_validate(data)
    assert parsed.model_dump(mode="json") == data
    assert set(data) == set(model.model_fields)


@pytest.mark.parametrize(
    ("part", "model"),
    [
        ("request", translations.TranslateRequest),
        ("response", translations.TranslateResponse),
        ("lineEdit", translations.LineEdit),
        ("line", translations.TrackLine),
    ],
)
def test_trx07_each_translation_body_is_exactly_its_model(part: str, model: type[BaseModel]) -> None:
    data = json.loads((CONTRACT / "translations.json").read_text())[part]
    parsed = model.model_validate(data)
    assert parsed.model_dump(mode="json") == data


def test_trx07_the_languages_fixture_is_the_backends_list() -> None:
    text = (CONTRACT / "languages.json").read_text()
    assert text == json.dumps(languages.payload(), indent=2, ensure_ascii=False) + "\n"
