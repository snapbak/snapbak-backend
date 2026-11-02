"""The `TranscriptionStrategy` facade (D10, Q10; docs/cloud.md): one interface in front of the vendors, so a later
strategy (one vendor, or another pair) replaces one class."""

import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from snapbak_backend.db.models import Failure
from snapbak_backend.transcription.merge import Merged

type Refs = dict[str, Any]
type Vendor = Literal["assemblyai", "pyannote"]
# `retry`: the vendor failed its part on its own side and asks for it again (an outage, not the audio).
type VendorState = Literal["running", "done", "failed", "retry"]
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

    async def rediarize(self, audio_url: str, count: int) -> str:
        """Starts pyannote again at exactly `count` voices (MOM-08); the vendor's job ID."""
        ...

    async def rediarization(self, vendor_job: str) -> list[dict[str, Any]] | None:
        """The rerun's `exclusiveDiarization` once done, None while it runs; VendorRefused when it failed."""
        ...

    async def identify(self, audio_url: str, prints: dict[str, str]) -> str:
        """Starts pyannote's identify with each speaker's voiceprint, by label (MOM-08); the vendor's job ID."""
        ...

    async def identification(self, vendor_job: str) -> dict[str, Any] | None:
        """The identify output once done, None while it runs; VendorRefused when it failed."""
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


def assemblyai_retryable(error: str) -> bool:
    """AssemblyAI failing a transcript on its own side, asking for it again: "Internal server error. Please retry your
    request. ..." (seen 3 Nov 2026)."""
    said = error.lower()
    return "internal server error" in said or "please retry" in said


def assemblyai_failure(error: str) -> Failure:
    """AssemblyAI's error text as a reason, matched on whole words ("information" is no "format"). The exact strings
    are unverified, so `just test-cloud` records the real ones (RESEARCH.md)."""
    words = set(re.findall(r"[a-z]+", error.lower()))
    if "language" in words:
        return "unsupported_language"
    if {"speech", "silence"} & words or "no spoken audio" in error.lower():
        return "no_speech"
    if {"audio", "file", "download", "format"} & words:
        return "upload_invalid"
    return "vendor_error"
