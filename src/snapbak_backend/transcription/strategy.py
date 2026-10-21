"""The `TranscriptionStrategy` facade (D10, Q10; docs/cloud.md): one interface in front of the vendors, so a later
strategy (one vendor, or another pair) replaces one class."""

from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from snapbak_backend.db.models import Failure
from snapbak_backend.transcription.merge import Merged

type Refs = dict[str, Any]
type Vendor = Literal["assemblyai", "pyannote"]
type VendorState = Literal["running", "done", "failed"]
VENDORS: tuple[Vendor, ...] = ("assemblyai", "pyannote")


class VendorError(Exception):
    """A vendor call failed. From `submit`, `refs` holds the vendors that did accept, so a retry doesn't submit (and
    pay for) them again, and a failed job deletes their copies."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.refs: Refs = {}


class VendorRefused(VendorError):
    """A vendor answered 4xx: asking again won't help, so the job fails with `vendor_error`."""


class VendorUnavailable(VendorError):
    """A vendor answered 429 or 5xx, or the network failed: the task asks again later."""


@dataclass(frozen=True)
class JobInput:
    job_id: UUID
    recording_id: UUID
    audio_url: str  # presigned GET, 2 h
    duration_ms: int
    language: str  # 'auto', 'hinglish' or a code
    webhooks: dict[str, str]  # vendor -> webhook URL with the job
    webhook_auth: str  # the AssemblyAI header value, a per-job HMAC


@dataclass(frozen=True)
class VendorStatus:
    state: VendorState
    failure: Failure | None = None


class TranscriptionStrategy(Protocol):
    name: str  # stored in transcription_jobs.strategy

    async def submit(self, job: JobInput, refs: Refs) -> Refs:
        """Starts every vendor not already in `refs`; the job's vendor_refs."""
        ...

    async def status(self, refs: Refs, vendor: Vendor) -> VendorStatus: ...

    async def result(self, refs: Refs) -> Merged: ...

    async def diarization(self, refs: Refs) -> list[dict[str, Any]]:
        """pyannote's `exclusiveDiarization`, for the voiceprint spans."""
        ...

    async def voiceprint(self, clip_url: str, webhook: str) -> str:
        """Starts a voiceprint of one speaker's clip; the vendor's job ID."""
        ...

    async def voiceprint_output(self, vendor_job: str) -> str | None:
        """The voiceprint string once made, else None."""
        ...

    async def cleanup(self, refs: Refs) -> None:
        """Deletes the vendors' copies; failures are ignored."""
        ...

    async def aclose(self) -> None: ...


def assemblyai_failure(error: str) -> Failure:
    """AssemblyAI's error text as a reason. The exact strings are unverified, so this matches on substrings, and
    `just test-cloud` records the real ones (RESEARCH.md)."""
    said = error.lower()
    if "language" in said:
        return "unsupported_language"
    if "no spoken audio" in said or "speech" in said or "silence" in said:
        return "no_speech"
    if "audio" in said or "file" in said or "download" in said or "format" in said:
        return "upload_invalid"
    return "vendor_error"
