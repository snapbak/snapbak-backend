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
    recordingType: Literal["video", "audio", "gif"] = "video"


class RecordingResponse(Wire):
    recordingId: UUID
    title: str
    durationMs: int
    recordingType: Literal["video", "audio", "gif"] = "video"


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


class GifUploadRequest(Wire):
    """The share's GIF, already animated, uploaded as-is (GIFS-AND-REPLAY-60, G7): no length, but a size the
    page's unfurl and player use."""

    kind: Literal["gif"]
    bytes: int = Field(gt=0)
    contentType: Literal["image/gif"]
    shareId: UUID
    width: int = Field(ge=1, le=16_384)
    height: int = Field(ge=1, le=16_384)


type AnyUpload = Annotated[UploadRequest | ShareUploadRequest | GifUploadRequest, Field(discriminator="kind")]


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
    # `guessed`: named by the speaker identification loop from what was said (MOM-08), undoable.
    nameSource: Literal["vendor", "user", "guessed"] | None
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
    # The speaker identification loop's state and changes, or null before it ever ran (MOM-08).
    speakerPass: SpeakerPass | None = None


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
    # Only on an Undo that puts a name back as it was (MOM-08): its earlier source; otherwise a name is the user's.
    nameSource: Literal["vendor", "guessed"] | None = None


class EditedSpeaker(SpeakerRequest):
    """A speaker as the app's edit leaves it (TRX-11); `deleted` when its last line went to another speaker."""

    id: UUID
    deleted: bool = False


class MovedLine(Wire):
    id: UUID
    speakerId: UUID


class SpeakerEditsRequest(Wire):
    """One speaker edit of the app as the speakers and lines stand after it: a merge, its undo, a line given to a
    new speaker (TRX-11). Sent again, it changes nothing."""

    speakers: list[EditedSpeaker] = Field(max_length=100)
    lines: list[MovedLine] = Field(max_length=20_000)


class LineRev(Wire):
    id: UUID
    rev: int


class SpeakerEditsResponse(Wire):
    speakers: list[SpeakerOut]
    lines: list[LineRev]


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
    # What the page shows (D20): `video`, `audio` (live at once, on the recording's own audio) or `meeting`.
    type: Literal["video", "audio", "meeting", "gif"]
    # From create: an audio recording's page has no audio here yet, so the Mac uploads it now.
    needsUpload: bool = False


class ShareUpdateRequest(Wire):
    state: Literal["live", "disabled"] | None = None
    expiresIn: Expiry | None = None


class ShareCompleteResponse(Wire):
    uploadId: UUID
    state: Literal["complete"]
    share: ShareRef


class MeetingRequest(Wire):
    """Promote the recording to a meeting (D13). `language` and `script` start its transcription when there is none,
    as the transcription route's body does."""

    title: str = Field(min_length=1, max_length=300)
    language: str = Field(default="auto", max_length=20)
    script: Literal["native", "roman"] = "native"
    # Whether the user chose `title` in the promote sheet (the app knows; grading 4): the minutes' title never
    # replaces a title the user chose.
    titleByUser: bool = False


# The minutes' sections, one call each, in the order they are written (the summary call writes the title too, so
# the title is no section of its own: nothing can be pending, incomplete or regenerated as "title").
type Section = Literal[
    "summary",
    "mom",
    "actionItems",
    "decisions",
    "openQuestions",
    "risks",
    "followUps",
    "nextAgenda",
    "agenda",
    "contradictions",
]


class DerivationOut(Wire):
    """Where a claim came from (D23): the line, who said it, when, and its verbatim words."""

    lineId: UUID
    speaker: str
    atMs: int = Field(ge=0)
    quote: str


class ActionItemOut(Wire):
    """Something someone is to do. `byUser` when the user wrote or changed it: no derivation is needed then, and a
    regenerate keeps it."""

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    owner: str | None = None
    due: str | None = None
    derivedFrom: list[DerivationOut]
    byUser: bool = False


class ClaimOut(Wire):
    """An open question: what was asked and not settled."""

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    derivedFrom: list[DerivationOut]
    byUser: bool = False


class DecisionOut(ClaimOut):
    """What was decided, why (`rationale`) and what was considered and not chosen (`alternatives`), each only as the
    transcript supports it."""

    rationale: str | None = None
    alternatives: list[str] = Field(default_factory=list)


class RiskOut(ClaimOut):
    kind: Literal["risk", "blocker", "dependency"]
    owner: str | None = None


class TopicOut(Wire):
    """A topic: one discussed (`agenda`, with how far it got) or one proposed for the next meeting (`nextAgenda`,
    no status)."""

    id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    derivedFrom: list[DerivationOut]
    byUser: bool = False


class AgendaOut(TopicOut):
    status: Literal["covered", "partial", "unresolved"]


class Incomplete(Wire):
    """A section whose run failed for good: its failure code; a regenerate of that section alone retries it."""

    section: Section
    code: str


class NotCommitmentOut(Wire):
    """A line the detail guard flagged as a commitment that the completion judged none (a proposal nobody took up,
    a remark): stored so every commitment ends either an action item or this judgment, never silently dropped.
    Server-owned, as the pins are."""

    lineId: UUID
    reason: str


class PinnedOut(Wire):
    """A pin as the minutes show it (MEETINGS.md 7.1): copied from the meeting's pins, never written by the model."""

    pinId: UUID
    atMs: int = Field(ge=0)
    note: str | None


class MinutesOut(Wire):
    """The minutes document (D23, D26; minutes v2, 26 Oct 2026): always English, stored exactly as this shape. Each
    section comes from its own model call, in `minutes.CALLS` order, and is published as it lands: `pending` names the
    sections still to come, `incomplete` the ones that failed. Every AI item carries derivations the validator
    checked; `userEdited` names the text fields (`title`, `summary`, `mom`) the user changed. No field has a length
    cap (the founder's "lots of detail"): the only bound is the edit route's 2 MB for the whole document; `summary`
    and `mom` are empty while still pending."""

    title: str | None = None
    summary: str
    mom: str
    actionItems: list[ActionItemOut]
    openQuestions: list[ClaimOut]
    decisions: list[DecisionOut]
    risks: list[RiskOut] = Field(default_factory=list)
    followUps: list[ActionItemOut] = Field(default_factory=list)
    nextAgenda: list[TopicOut] = Field(default_factory=list)
    agenda: list[AgendaOut] = Field(default_factory=list)
    contradictions: list[ClaimOut] = Field(default_factory=list)
    pinned: list[PinnedOut] = Field(default_factory=list)
    userEdited: list[Literal["title", "summary", "mom"]] = Field(default_factory=list)
    pending: list[Section] = Field(default_factory=list)
    incomplete: list[Incomplete] = Field(default_factory=list)
    notCommitments: list[NotCommitmentOut] = Field(default_factory=list)


class MinutesEditRequest(Wire):
    """The user's whole edited minutes, against the meeting version they edited (409 `version_conflict` when it
    moved)."""

    baseVersion: int = Field(ge=1)
    minutes: MinutesOut


class RegenerateRequest(Wire):
    """Which sections to write again; none means all (user-made and user-edited items are kept)."""

    sections: list[Section] = Field(default_factory=list)


class PinOut(Wire):
    id: UUID
    atMs: int = Field(ge=0)
    note: str | None


class Cue(Wire):
    """Evidence for a speaker change: a transcript line's `i` and its words, verbatim."""

    i: int
    quote: str


class Change(Wire):
    """One change the speaker identification loop made or offers (MEETINGS.md D42, MOM-08). `name`: `speakerId` takes
    `name` (`before` the name it had). `merge`: `speakerId` (`before` its name or label) joins `intoId`, its `lineIds`
    moving with it. `move`: the
    line `lineIds[0]` moves from `speakerId` to `intoId`. `applied` changes are in the transcript, `suggested` ones
    wait for the user; `undone` and `rejected` are history."""

    id: str
    kind: Literal["name", "merge", "move"]
    # `gone`: an undone change the transcript no longer allows back (renamed or merged since); never offered again.
    state: Literal["applied", "suggested", "undone", "rejected", "gone"]
    speakerId: UUID
    name: str | None = None
    before: str | None = None
    intoId: UUID | None = None
    lineIds: list[UUID] = Field(default_factory=list)
    cues: list[Cue] = Field(default_factory=list)


class SpeakerChangeRequest(Wire):
    """The user's answer to one change of the speaker loop: undo an applied one or redo an undone one, accept or
    reject a suggestion."""

    action: Literal["undo", "redo", "accept", "reject"]


class SpeakerPass(Wire):
    """The loop's state on a recording: `rev` moves with every change applied, undone, accepted or rejected, so the
    app knows to read the transcript again; `stage` is where the run is, `round` how many rounds applied."""

    rev: int = 0
    stage: Literal["reading", "listening", "naming", "rewriting", "done"] = "reading"
    round: int = 0
    # Whether this run heard the recording again (so the live story says so only when it did), and whether the
    # loop broke (its draft stands; the app says so rather than "nothing found").
    listened: bool = False
    failed: bool = False
    # Where this run's changes start in `changes`: the live story tells only what this run found.
    start: int = 0
    changes: list[Change] = Field(default_factory=list)


class MeetingResponse(Wire):
    meetingId: UUID
    recordingId: UUID
    title: str
    state: Literal["awaiting_transcript", "generating", "ready", "failed"]
    minutes: MinutesOut | None
    minutesFailure: str | None
    stale: bool
    version: int
    pins: list[PinOut]
    startedAt: Utc | None
    endedAt: Utc | None
    # The recording's speaker identification loop: its stage while the minutes are made, and its changes (MOM-08).
    speakerPass: SpeakerPass | None = None


class MeetingAccepted(Wire):
    """The answer of promote and regenerate: the meeting moved, or was already moving."""

    meetingId: UUID
    state: str
    version: int


class MeetingPatchRequest(Wire):
    title: str = Field(min_length=1, max_length=300)


class PinRequest(Wire):
    atMs: int = Field(ge=0)
    note: str | None = Field(default=None, max_length=500)


class AskRequest(Wire):
    question: str = Field(min_length=1, max_length=1000)


class CitationOut(Wire):
    lineId: UUID
    quote: str


class AskResponse(Wire):
    answer: str
    citations: list[CitationOut]
    supported: bool


class SearchRequest(Wire):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)


class SearchHit(Wire):
    meetingId: UUID
    recordingId: UUID
    title: str
    kind: Literal["summary", "action", "decision", "question"]
    snippet: str
    # An action item's owner from the minutes, so a hit reads "Action item · Priya"; None for other sections.
    owner: str | None = None
    score: float


class SearchResponse(Wire):
    hits: list[SearchHit]
