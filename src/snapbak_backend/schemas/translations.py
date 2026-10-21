"""Caption translation bodies (TRX-07, 16-caption-translation LLD, Types), named as on the wire."""

from uuid import UUID

from pydantic import Field

from snapbak_backend.schemas.cloud import Wire

# A page's limit; more is 413 `too_many_lines`, checked by the service rather than here, for its own code.
PAGE_LINES = 200


class SourceLine(Wire):
    """A transcript line's kept text (its words not cut). `sourceHash` is the app's, compared only for equality."""

    idx: int = Field(ge=0)
    lineId: str = Field(pattern=r"^ln_[0-9A-Za-z-]+$", max_length=64)
    rev: int = Field(ge=0)
    sourceHash: str = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1, max_length=1000)


class TranslateRequest(Wire):
    language: str = Field(max_length=20)
    lines: list[SourceLine] = Field(min_length=1, max_length=10_000)
    # Translate even the lines whose stored translation matches.
    force: bool = False


class TrackLine(Wire):
    idx: int
    lineId: str
    sourceRev: int
    sourceHash: str
    text: str | None
    failed: bool
    edited: bool
    updatedAt: str


class CaptionTrackOut(Wire):
    id: UUID
    recordingId: UUID
    language: str
    sourceLanguage: str
    lines: list[TrackLine]


class TranslateResponse(Wire):
    track: CaptionTrackOut
    # The idx of this request's lines that failed.
    failed: list[int]


class LineEdit(Wire):
    # None only when an undo puts back a line that had no translation.
    text: str | None = Field(max_length=1000)
    # The client's edit counter: an edit older than the stored one is ignored, so the last write wins.
    rev: int = Field(ge=0)
    # A fix is edited and no longer failed; an undo puts the line's earlier state back.
    edited: bool = True
    failed: bool = False
