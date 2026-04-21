"""Cryptographic primitives used by Kuckuck.

Design notes
------------
The user-facing **master secret** lives in ``.kuckuck-key`` as a hex string.
Two subkeys are derived from it via HKDF-SHA256 so that key rotation and
purpose separation are possible without asking users to manage two secrets:

* ``HMAC`` subkey — used for token fingerprinting. Output is truncated to a
  short hex prefix to keep tokens readable for the LLM while being stable
  across documents. Collisions on the truncated prefix are handled at the
  mapping layer via a counter suffix.
* ``MAP`` subkey — used as the AES-GCM key for the sidecar mapping file.

All string input to the HMAC is normalized to Unicode NFC to make
``"Müller"`` hash identically regardless of whether the source was
macOS (often NFD) or Windows (usually NFC).
"""

from __future__ import annotations

import hmac
import unicodedata
from hashlib import sha256

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

#: Number of hex characters kept from the HMAC output. 8 hex = 32 bits —
#: short enough for LLM-readable tokens, collision handling is done at the
#: mapping layer.
HMAC_HEX_LENGTH = 8

#: AES-GCM key size in bytes (256-bit key).
AES_KEY_BYTES = 32

#: AES-GCM nonce size in bytes. 12 bytes is the recommended length.
AES_NONCE_BYTES = 12

#: HKDF info strings — changing these invalidates all previously derived subkeys.
_HKDF_INFO_HMAC = b"kuckuck-hmac-v1"
_HKDF_INFO_MAP = b"kuckuck-mapping-v1"


def normalize(text: str) -> str:
    """Return *text* in Unicode NFC form.

    Called before every HMAC operation so that macOS-NFD and Windows-NFC
    inputs of the same visual string produce identical hashes.
    """
    return unicodedata.normalize("NFC", text)


#: Expected length of a hex-encoded :data:`KEY_BYTES`-sized master (64 chars).
_HEX_MASTER_LEN = 64

_HEX_CHARS = frozenset("0123456789abcdefABCDEF")


def _master_bytes(master: SecretStr) -> bytes:
    """Return the master secret as raw bytes.

    The secret is treated as hex **only** when it is exactly
    :data:`_HEX_MASTER_LEN` characters long and every character is a valid
    hex digit. Any other input is treated as a raw UTF-8 passphrase.

    This strict rule avoids an earlier ambiguity where ``"abc123"`` (valid
    hex, 3 bytes) and ``"abc12"`` (invalid hex, 5 UTF-8 bytes) silently
    produced unrelated keys — a typo would have been undetectable.
    """
    raw = master.get_secret_value().strip()
    if len(raw) == _HEX_MASTER_LEN and all(c in _HEX_CHARS for c in raw):
        return bytes.fromhex(raw)
    return raw.encode("utf-8")


def _derive(master: SecretStr, info: bytes, length: int) -> bytes:
    """Derive a subkey of *length* bytes from the master secret via HKDF-SHA256."""
    hkdf = HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info)
    return hkdf.derive(_master_bytes(master))


def derive_hmac_key(master: SecretStr) -> bytes:
    """Return the HMAC subkey derived from *master* (32 bytes)."""
    return _derive(master, _HKDF_INFO_HMAC, 32)


def derive_map_key(master: SecretStr) -> bytes:
    """Return the AES-GCM subkey for the mapping file (32 bytes)."""
    return _derive(master, _HKDF_INFO_MAP, AES_KEY_BYTES)


def hmac_token(master: SecretStr, value: str) -> str:
    """Return the truncated hex HMAC fingerprint of *value*.

    *value* is NFC-normalized before hashing. The output has length
    :data:`HMAC_HEX_LENGTH` and is the token suffix used in pseudonyms such
    as ``[[PERSON_a7f3b2c1]]``.
    """
    subkey = derive_hmac_key(master)
    normalized = normalize(value).encode("utf-8")
    digest = hmac.new(subkey, normalized, sha256).hexdigest()
    return digest[:HMAC_HEX_LENGTH]


def full_hmac(master: SecretStr, value: str) -> str:
    """Return the full (64 hex chars) HMAC-SHA256 — used for collision disambiguation."""
    subkey = derive_hmac_key(master)
    normalized = normalize(value).encode("utf-8")
    return hmac.new(subkey, normalized, sha256).hexdigest()


def encrypt_mapping_payload(master: SecretStr, plaintext: bytes) -> tuple[bytes, bytes]:
    """Encrypt *plaintext* with the AES-GCM mapping subkey.

    Returns a tuple ``(nonce, ciphertext)`` where *ciphertext* includes the
    authentication tag. The caller is responsible for serializing nonce and
    ciphertext together (see :mod:`kuckuck.mapping`).
    """
    key = derive_map_key(master)
    nonce = _secure_random(AES_NONCE_BYTES)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)
    return nonce, ciphertext


def decrypt_mapping_payload(master: SecretStr, nonce: bytes, ciphertext: bytes) -> bytes:
    """Decrypt an AES-GCM mapping payload. Raises on authentication failure."""
    key = derive_map_key(master)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, associated_data=None)


def _secure_random(n_bytes: int) -> bytes:
    """Wrapper around :func:`os.urandom` kept local for easier monkeypatching in tests."""
    import os  # pylint: disable=import-outside-toplevel

    return os.urandom(n_bytes)
