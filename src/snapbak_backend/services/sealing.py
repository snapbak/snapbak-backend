"""Voiceprints sealed with AES-256-GCM before they reach Postgres (TRX-08; docs/cloud.md). Sealed bytes are
`key_id (1 byte) || nonce (12 bytes) || ciphertext || tag`; the speaker's UUID is the associated data, so a sealed
value copied to another row fails to open. The highest key ID seals; any opens."""

import os
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12


def seal(keys: dict[int, bytes], speaker_id: UUID, plain: bytes) -> tuple[bytes, int]:
    key_id = max(keys)
    nonce = os.urandom(NONCE_BYTES)
    sealed = bytes([key_id]) + nonce + AESGCM(keys[key_id]).encrypt(nonce, plain, speaker_id.bytes)
    return sealed, key_id


def unseal(keys: dict[int, bytes], speaker_id: UUID, sealed: bytes) -> bytes:
    key_id, nonce, body = sealed[0], sealed[1 : 1 + NONCE_BYTES], sealed[1 + NONCE_BYTES :]
    plain = AESGCM(keys[key_id]).decrypt(nonce, body, speaker_id.bytes)
    return plain
