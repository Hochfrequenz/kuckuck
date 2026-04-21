"""Encrypted sidecar mapping file — stores the pseudonym ↔ original correspondence.

File format (binary, little-endian)::

    offset  size  field
    ------  ----  -----------------------------------------
    0       4     magic bytes b"KUCK"
    4       1     schema version (currently 2)
    5       1     key-id length N (may be 0)
    6       N     key-id string (UTF-8)
    6+N    12     AES-GCM nonce
    18+N   ...    AES-GCM ciphertext (includes auth tag)

The header bytes (offsets 0 .. 6+N-1) are bound to the AES-GCM tag as
*associated data*, so tampering with magic, version, or key-id invalidates
authentication even though those bytes live in the clear.

Schema-version history:

* **2** — current. Header is authenticated via AES-GCM AAD.
* **1** — pre-review format without AAD binding. Rejected on read; the PR
  that introduced this file format was the very first release, so no
  persistent v1 files exist in the wild.

The ciphertext decrypts to a JSON document with shape::

    {
      "entries": {
        "<token>": {"original": "<str>", "entity_type": "<str>"},
        ...
      }
    }

*Token* is the hex suffix used in the pseudonymized output (e.g. ``a7f3b2c1``)
optionally followed by ``-N`` when a collision was disambiguated with a counter.

**Operational note:** AES-GCM with random 96-bit nonces is safe for fewer
than 2^32 encryptions per key (NIST SP 800-38D). At one ``save_mapping``
call per document, a team of 100 users writing 100 files per day reaches
that bound in ~1,100 years. Rotate the master secret long before then.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, Field, SecretStr

from kuckuck.crypto import decrypt_mapping_payload, encrypt_mapping_payload, hmac_token, normalize

MAGIC = b"KUCK"
SCHEMA_VERSION = 2

#: Hard cap on collision-counter iterations. Far above any realistic
#: birthday-bound outcome; a legitimate corpus would hit a handful of
#: collisions at most. Exceeding this indicates either a pathological
#: corpus or a corrupted mapping.
_MAX_COLLISION_COUNTER = 10_000


class MappingEntry(BaseModel):
    """One pseudonym ↔ original pair."""

    original: str
    entity_type: str

    def model_dump_json_safe(self) -> dict[str, str]:
        """Return a JSON-serialisable plain ``dict`` of this entry's fields."""
        return {"original": self.original, "entity_type": self.entity_type}


class MappingCorruptError(ValueError):
    """Raised when a mapping file is malformed (wrong magic, unsupported version, …)."""


class Mapping(BaseModel):
    """Pseudonym ↔ original correspondence for one or more documents.

    Build one with :meth:`get_or_allocate` while pseudonymizing and persist via
    :func:`save_mapping`. Reload a previously written mapping with
    :func:`load_mapping` — entries are merged rather than overwritten so that
    cross-document consistency survives repeated runs.
    """

    entries: dict[str, MappingEntry] = Field(default_factory=dict)
    key_id: str = ""

    def get_or_allocate(self, master: SecretStr, *, original: str, entity_type: str) -> str:
        """Return the token for *original*, creating a new one if needed.

        The token is the truncated HMAC fingerprint; on collision (different
        *original* with the same truncated hash) a numeric suffix is appended.
        """
        normalized = normalize(original)
        candidate = hmac_token(master, normalized)
        existing = self.entries.get(candidate)
        if existing is None:
            self.entries[candidate] = MappingEntry(original=normalized, entity_type=entity_type)
            return candidate
        if existing.original == normalized:
            return candidate

        # Collision: different string produced the same truncated hash.
        # Disambiguate with an ever-higher counter suffix until we find a free slot.
        for counter in range(2, _MAX_COLLISION_COUNTER + 1):
            suffixed = f"{candidate}-{counter}"
            slot = self.entries.get(suffixed)
            if slot is None:
                self.entries[suffixed] = MappingEntry(original=normalized, entity_type=entity_type)
                return suffixed
            if slot.original == normalized:
                return suffixed
        raise RuntimeError(
            f"too many collisions on truncated HMAC '{candidate}' "
            "(mapping may be corrupted or adversarial — rotate the master secret)"
        )

    def resolve_token(self, token: str) -> MappingEntry | None:
        """Return the entry for *token* or ``None`` if unknown."""
        return self.entries.get(token)

    def __iter__(self) -> Iterator[str]:  # type: ignore[override]
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def tokens_by_type(self, entity_type: str) -> list[str]:
        """Return every token whose stored entry carries *entity_type*."""
        return [token for token, entry in self.entries.items() if entry.entity_type == entity_type]


def _build_header(key_id: str, schema_version: int = SCHEMA_VERSION) -> bytes:
    """Return the authenticated header bytes for a mapping file."""
    key_id_bytes = key_id.encode("utf-8")
    if len(key_id_bytes) > 255:
        raise ValueError("key_id longer than 255 bytes is not supported")
    return MAGIC + bytes([schema_version, len(key_id_bytes)]) + key_id_bytes


def _unpack(blob: bytes) -> tuple[int, str, bytes, bytes, bytes]:
    """Return ``(version, key_id, header_bytes, nonce, ciphertext)``.

    *header_bytes* is the raw prefix that must be used as AES-GCM AAD on
    read-side for the tag to validate.
    """
    if len(blob) < 6 or blob[:4] != MAGIC:
        raise MappingCorruptError("not a Kuckuck mapping file (magic mismatch)")
    version = blob[4]
    key_id_len = blob[5]
    header_end = 6 + key_id_len
    if len(blob) < header_end + 12:
        raise MappingCorruptError("mapping file truncated")
    key_id = blob[6:header_end].decode("utf-8")
    header_bytes = blob[:header_end]
    nonce = blob[header_end : header_end + 12]
    ciphertext = blob[header_end + 12 :]
    return version, key_id, header_bytes, nonce, ciphertext


def save_mapping(master: SecretStr, mapping: Mapping, path: Path | str) -> Path:
    """Encrypt *mapping* with *master* and write to *path*.

    The header (magic, schema version, key-id) is bound to the AES-GCM tag
    as associated data, so header tampering invalidates authentication.
    """
    plaintext = json.dumps(
        {"entries": {token: entry.model_dump_json_safe() for token, entry in mapping.entries.items()}},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    header = _build_header(mapping.key_id)
    nonce, ciphertext = encrypt_mapping_payload(master, plaintext, associated_data=header)
    target = Path(path)
    target.write_bytes(header + nonce + ciphertext)
    return target


def load_mapping(master: SecretStr, path: Path | str, *, expected_key_id: str | None = None) -> Mapping:
    """Read and decrypt *path*, returning a populated :class:`Mapping`.

    When *expected_key_id* is given, raises :class:`MappingCorruptError` if
    the file's key-id field differs. This is how callers enforce key-rotation
    policy — the key-id alone is *not* authenticated against the real secret,
    but the AES-GCM tag is, so pairing "expected_key_id check" with a
    successful decrypt gives both halves of a rotation guarantee.
    """
    blob = Path(path).read_bytes()
    version, key_id, header_bytes, nonce, ciphertext = _unpack(blob)
    if version != SCHEMA_VERSION:
        raise MappingCorruptError(f"unsupported mapping schema version: {version}")
    if expected_key_id is not None and key_id != expected_key_id:
        raise MappingCorruptError(f"key_id mismatch: expected {expected_key_id!r}, got {key_id!r}")
    plaintext = decrypt_mapping_payload(master, nonce, ciphertext, associated_data=header_bytes)
    payload = json.loads(plaintext.decode("utf-8"))
    entries = {token: MappingEntry(**entry) for token, entry in payload.get("entries", {}).items()}
    return Mapping(entries=entries, key_id=key_id)
