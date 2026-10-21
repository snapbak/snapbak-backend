import uuid

import pytest
from cryptography.exceptions import InvalidTag

from snapbak_backend.services.sealing import seal, unseal

OLD = {1: bytes(32)}
KEYS = {1: bytes(32), 2: bytes(range(32))}
SPEAKER = uuid.UUID("3f2e9a4c-1b7d-4e5f-8a90-1c2d3e4f5a6b")


def test_trx08_a_sealed_voiceprint_opens_and_names_the_newest_key() -> None:
    sealed, key_id = seal(KEYS, SPEAKER, b"U29tZVZvaWNlUHJpbnQ=")
    assert (key_id, sealed[0]) == (2, 2)
    assert len(sealed) == 1 + 12 + len(b"U29tZVZvaWNlUHJpbnQ=") + 16
    assert b"U29tZVZvaWNlUHJpbnQ=" not in sealed
    assert unseal(KEYS, SPEAKER, sealed) == b"U29tZVZvaWNlUHJpbnQ="


def test_trx08_a_voiceprint_sealed_with_an_old_key_still_opens() -> None:
    sealed, key_id = seal(OLD, SPEAKER, b"vp")
    assert key_id == 1
    assert unseal(KEYS, SPEAKER, sealed) == b"vp"


def test_trx08_a_sealed_voiceprint_copied_to_another_speaker_fails() -> None:
    sealed, _ = seal(KEYS, SPEAKER, b"vp")
    with pytest.raises(InvalidTag):
        unseal(KEYS, uuid.uuid4(), sealed)


def test_trx08_two_seals_of_one_voiceprint_differ() -> None:
    assert seal(KEYS, SPEAKER, b"vp")[0] != seal(KEYS, SPEAKER, b"vp")[0]
