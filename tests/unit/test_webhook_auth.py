import base64
import hashlib
import hmac

from snapbak_backend.services.signatures import assemblyai_header, assemblyai_ok, pyannote_ok

JOB = "5d0a6e1c-8f3b-4a2d-9c7e-1b2a3c4d5e6f"
BODY = b'{"jobId":"p-1","status":"succeeded","output":{}}'
# A live delivery: milliseconds, and 300 s after it arrived, though the docs say seconds.
SENT = "1792578561343"


def pyannote_signature(secret: str, timestamp: str, body: bytes) -> bytes:
    return hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).digest()


def test_trx01_the_assemblyai_header_is_a_per_job_hmac_in_hex() -> None:
    expected = hmac.new(b"secret", f"assemblyai:{JOB}".encode(), hashlib.sha256).hexdigest()
    assert assemblyai_header("secret", JOB) == expected
    assert assemblyai_ok("secret", JOB, expected)
    assert not assemblyai_ok("secret", "another-job", expected)
    assert not assemblyai_ok("secret", JOB, "")


def test_trx01_the_pyannote_signature_is_accepted_in_hex_and_base64() -> None:
    digest = pyannote_signature("whs", SENT, BODY)
    assert pyannote_ok("whs", SENT, BODY, digest.hex())
    assert pyannote_ok("whs", SENT, BODY, base64.b64encode(digest).decode())


def test_trx01_a_pyannote_signature_over_another_body_or_secret_fails() -> None:
    digest = pyannote_signature("whs", SENT, BODY).hex()
    assert not pyannote_ok("whs", SENT, BODY + b" ", digest)
    assert not pyannote_ok("other", SENT, BODY, digest)


def test_trx01_any_pyannote_timestamp_passes_but_a_missing_one_fails() -> None:
    for sent in ("1", "soon", SENT):
        assert pyannote_ok("whs", sent, BODY, pyannote_signature("whs", sent, BODY).hex())
    assert not pyannote_ok("whs", "", BODY, pyannote_signature("whs", "", BODY).hex())
