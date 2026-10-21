"""The cloud routes' request and response bodies, named as on the wire (docs/cloud.md). Times are integer ms."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PlainSerializer

from snapbak_backend.db.models import PageState, ShareState

# Times leave as "2026-10-17T10:02:11Z".
UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
type Utc = Annotated[datetime, PlainSerializer(lambda when: when.strftime(UTC_FORMAT), return_type=str)]


class Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CloudErrorBody(Wire):
    code: str
    message: str


class CloudError(Wire):
    error: CloudErrorBody


class DeviceRequest(Wire):
    installId: UUID
    appVersion: str = Field(max_length=50)


class DeviceResponse(Wire):
    deviceId: UUID
    token: str
    voiceprintConsent: bool | None


class ConsentRequest(Wire):
    voiceprints: bool


class ConsentResponse(Wire):
    voiceprintConsent: bool
    consentAt: Utc


class RecordingRequest(Wire):
    title: str = Field(max_length=300)
    durationMs: int = Field(ge=0, le=2**31 - 1)
    createdAt: AwareDatetime


class RecordingResponse(Wire):
    recordingId: UUID
    title: str
    durationMs: int


class UploadRequest(Wire):
    kind: Literal["audio"]
    bytes: int = Field(gt=0)
    contentType: str = Field(min_length=1, max_length=100)


class ShareUploadRequest(Wire):
    """The share's video, the web encode (12 LLD), with its length and size, which the share takes on complete."""

    kind: Literal["share"]
    bytes: int = Field(gt=0)
    contentType: Literal["video/mp4"]
    shareId: UUID
    durationMs: int = Field(ge=0, le=2**31 - 1)
    width: int = Field(ge=1, le=16_384)
    height: int = Field(ge=1, le=16_384)


type AnyUpload = Annotated[UploadRequest | ShareUploadRequest, Field(discriminator="kind")]


class PartUrl(Wire):
    n: int
    url: str


class UploadResponse(Wire):
    uploadId: UUID
    partSize: int
    expiresAt: Utc
    urls: list[PartUrl]


class PartsRequest(Wire):
    parts: list[int] = Field(min_length=1, max_length=10_000)


class CompletedPart(Wire):
    n: int = Field(ge=1, le=10_000)
    etag: str = Field(min_length=1, max_length=200)


class CompleteRequest(Wire):
    parts: list[CompletedPart] = Field(min_length=1, max_length=10_000)


class CompleteResponse(Wire):
    uploadId: UUID
    state: Literal["complete"]
    jobId: UUID | None


class TranscriptionRequest(Wire):
    """`language` is `auto`, `hinglish` or one of the 18 codes; checked by the route for its own error code."""

    language: str = Field(max_length=20)
    script: Literal["native", "roman"]


class TranscriptionResponse(Wire):
    jobId: UUID
    state: Literal["queued"]
    version: int
    needsUpload: bool


class JobRef(Wire):
    jobId: UUID
    state: str
    version: int


class JobConflict(Wire):
    error: CloudErrorBody
    job: JobRef


class JobResponse(Wire):
    jobId: UUID
    recordingId: UUID
    state: str
    stage: str | None
    failure: str | None
    language: str | None
    version: int


class WordOut(Wire):
    w: str = Field(max_length=200)
    s: int = Field(ge=0)
    e: int = Field(ge=0)
    filler: bool
    conf: float = Field(ge=0, le=1)


class SpeakerOut(Wire):
    id: UUID
    label: str
    name: str | None
    nameSource: Literal["vendor", "user"] | None
    colorIdx: int


class LineOut(Wire):
    id: UUID
    idx: int
    speakerId: UUID
    start: int
    end: int
    text: str
    words: list[WordOut]
    romanText: str | None
    romanWords: list[WordOut] | None
    rev: int


class TranscriptResponse(Wire):
    recordingId: UUID
    language: str | None
    script: Literal["native", "roman"]
    speakers: list[SpeakerOut]
    lines: list[LineOut]


class LineRequest(Wire):
    """A whole line, as the app's outbox sends it; a resend is idempotent and the last write wins (D11, Q19)."""

    text: str = Field(min_length=1, max_length=10_000)
    words: list[WordOut] = Field(min_length=1, max_length=2_000)
    speakerId: UUID
    rev: int = Field(ge=0)


class LineResponse(Wire):
    rev: int


class SpeakerNameRequest(Wire):
    name: str = Field(min_length=1, max_length=100)


class SpeakerRequest(Wire):
    """A speaker made on the Mac with **New speaker** (14 LLD)."""

    label: str = Field(min_length=1, max_length=50)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    colorIdx: int = Field(ge=0, le=7)


class TransliterateRequest(Wire):
    script: Literal["roman"]


class RomanLine(Wire):
    id: UUID
    romanText: str | None
    romanWords: list[WordOut] | None


class TransliterateResponse(Wire):
    lines: list[RomanLine]


type Expiry = Literal["never", "7d", "30d"]


class ShareRequest(Wire):
    """Create the recording's share, or prepare the update of its video (12 LLD). `expiresIn` applies on create."""

    title: str = Field(min_length=1, max_length=300)
    bytes: int = Field(gt=0)
    expiresIn: Expiry = "never"


class ShareRef(Wire):
    shareId: UUID
    slug: str
    url: str
    state: ShareState
    version: int


class ShareResponse(ShareRef):
    """Every share route's answer; `posterUploadUrl` only from create."""

    pageState: PageState
    expiresAt: Utc | None
    posterUploadUrl: str | None


class ShareUpdateRequest(Wire):
    state: Literal["live", "disabled"] | None = None
    expiresIn: Expiry | None = None


class ShareCompleteResponse(Wire):
    uploadId: UUID
    state: Literal["complete"]
    share: ShareRef
