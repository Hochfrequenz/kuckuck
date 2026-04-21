"""Cryptographic primitives used by Kuckuck.

Design notes
------------
The user-facing **master secret** lives in ``.kuckuck-key`` as a 64-character
hex string (256 bits of entropy from :func:`secrets.token_hex`). Two subkeys
are derived from it via HKDF-SHA256 so that key rotation and purpose
separation are possible without asking users to manage two secrets:

* ``HMAC`` subkey — used for token fingerprinting. Output is truncated to a
  short hex prefix to keep tokens readable for the LLM while being stable
  across documents. Collisions on the truncated prefix are handled at the
  mapping layer via a counter suffix.
* ``MAP`` subkey — used as the AES-GCM key for the sidecar mapping file.

We intentionally **do not** accept low-entropy passphrases as master secrets:
HKDF has no work factor (it is a KDF for pre-uniform entropy, not a password
hash), so a passphrase-shaped input would make both the AES-GCM sidecar and
the HMAC tokens offline brute-forceable.

All string input to the HMAC is normalized to Unicode NFC to make
``"Müller"`` hash identically regardless of whether the source was
macOS (often NFD) or Windows (usually NFC).

AES-GCM usage binds the mapping-file header (magic bytes, schema version,
key-id) as *associated data* so that an attacker who can rewrite the
sidecar cannot swap key-id fields or schema-version bytes while keeping a
valid authentication tag — see :mod:`kuckuck.mapping` for the wire format.
"""

from __future__ import annotations

import hmac
import re
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

#: Regex that matches *any* whitespace, including NBSP, zero-width spaces
#: (U+200B..U+200D), and the BOM (U+FEFF) — all common copy-paste artifacts
#: from password managers and browsers. Python's ``\s`` does not include the
#: zero-width range by default, so we list them explicitly.
_WHITESPACE_RE = re.compile(r"[\s\u200b\u200c\u200d\ufeff]+")


class InvalidMasterError(ValueError):
    """Raised when the master secret is not a valid 64-char hex key."""


def _master_bytes(master: SecretStr) -> bytes:
    """Return the master secret as raw bytes.

    Accepts **only** a 64-character hex string (32 bytes of entropy). Low-
    entropy passphrases are rejected because HKDF has no work factor; a
    passphrase-shaped master would render both the AES-GCM sidecar and the
    HMAC tokens offline brute-forceable. Use :func:`generate_key` to produce
    a conformant master.

    Trims any Unicode whitespace from both ends so copy-paste through
    password managers or browsers (which frequently inject NBSP, ZWSP, or
    trailing newlines) works reliably.
    """
    raw = _WHITESPACE_RE.sub("", master.get_secret_value())
    if len(raw) != _HEX_MASTER_LEN or not all(c in _HEX_CHARS for c in raw):
        raise InvalidMasterError(
            f"master secret must be exactly {_HEX_MASTER_LEN} hex characters "
            "(256 bits). Generate one with `kuckuck init-key` or "
            "`secrets.token_hex(32)`."
        )
    return bytes.fromhex(raw)


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


def encrypt_mapping_payload(
    master: SecretStr, plaintext: bytes, *, associated_data: bytes = b""
) -> tuple[bytes, bytes]:
    """Encrypt *plaintext* with the AES-GCM mapping subkey.

    Returns a tuple ``(nonce, ciphertext)`` where *ciphertext* includes the
    authentication tag. *associated_data* binds sidecar header fields
    (magic, schema version, key-id) to the tag so that attackers can't tamper
    with those bytes without invalidating the authentication.

    Nonces are pulled from :func:`os.urandom`. NIST SP 800-38D recommends
    random nonces for fewer than 2^32 invocations per key — well outside any
    realistic Kuckuck usage. Documents this cap in
    :mod:`kuckuck.mapping` as a rotation trigger.
    """
    key = derive_map_key(master)
    nonce = _secure_random(AES_NONCE_BYTES)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=associated_data)
    return nonce, ciphertext


def decrypt_mapping_payload(
    master: SecretStr, nonce: bytes, ciphertext: bytes, *, associated_data: bytes = b""
) -> bytes:
    """Decrypt an AES-GCM mapping payload. Raises on authentication failure."""
    key = derive_map_key(master)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, associated_data=associated_data)


def _secure_random(n_bytes: int) -> bytes:
    """Wrapper around :func:`os.urandom` kept local for easier monkeypatching in tests."""
    import os  # pylint: disable=import-outside-toplevel

    return os.urandom(n_bytes)
