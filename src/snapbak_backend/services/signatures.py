"""Webhook authentication (docs/cloud.md). AssemblyAI signs nothing, so its custom header carries a per-job HMAC
that Snapbak made; pyannoteAI signs `v0:{timestamp}:{raw body}` with the dashboard secret. Both compare in
constant time."""

import base64
import hashlib
import hmac


def assemblyai_header(secret: str, job_id: str) -> str:
    digest = hmac.new(secret.encode(), f"assemblyai:{job_id}".encode(), hashlib.sha256).hexdigest()
    return digest


def assemblyai_ok(secret: str, job_id: str, given: str) -> bool:
    expected = assemblyai_header(secret, job_id)
    same = hmac.compare_digest(expected.encode(), given.encode())
    return same


def pyannote_ok(secret: str, timestamp: str, body: bytes, given: str) -> bool:
    """The docs say base64 in prose and hex in code (RESEARCH.md), so either encoding passes. The timestamp is only
    signed, not checked for age: deliveries carry milliseconds 300 s ahead though the docs say seconds, and the docs'
    own example checks no age either."""
    if not timestamp:
        return False
    digest = hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).digest()
    encodings = (digest.hex().encode(), base64.b64encode(digest))
    matches = [hmac.compare_digest(expected, given.encode()) for expected in encodings]
    return any(matches)
