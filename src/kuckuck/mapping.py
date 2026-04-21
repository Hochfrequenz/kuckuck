"""Encrypted sidecar mapping file — stores the pseudonym ↔ original correspondence.

File format (binary, little-endian)::

    offset  size  field
    ------  ----  -----------------------------------------
    0       4     magic bytes b"KUCK"
    4       1     schema version (currently 1)
    5       1     key-id length N (may be 0)
    6       N     key-id string (UTF-8)
    6+N    12     AES-GCM nonce
    18+N   ...    AES-GCM ciphertext (includes auth tag)

The ciphertext decrypts to a JSON document with shape::

    {
      "entries": {
        "<token>": {"original": "<str>", "entity_type": "<str>"},
        ...
      }
    }

*Token* is the hex suffix used in the pseudonymized output (e.g. ``a7f3b2c1``)
optionally followed by ``-N`` when a collision was disambiguated with a counter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, Field, SecretStr

from kuckuck.crypto import decrypt_mapping_payload, encrypt_mapping_payload, hmac_token, normalize

MAGIC = b"KUCK"
SCHEMA_VERSION = 1


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
        counter = 2
        while True:
            suffixed = f"{candidate}-{counter}"
            slot = self.entries.get(suffixed)
            if slot is None:
                self.entries[suffixed] = MappingEntry(original=normalized, entity_type=entity_type)
                return suffixed
            if slot.original == normalized:
                return suffixed
            counter += 1

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


def _pack(mapping: Mapping, nonce: bytes, ciphertext: bytes) -> bytes:
    key_id_bytes = mapping.key_id.encode("utf-8")
    if len(key_id_bytes) > 255:
        raise ValueError("key_id longer than 255 bytes is not supported")
    header = MAGIC + bytes([SCHEMA_VERSION, len(key_id_bytes)]) + key_id_bytes
    return header + nonce + ciphertext


def _unpack(blob: bytes) -> tuple[int, str, bytes, bytes]:
    if len(blob) < 6 or blob[:4] != MAGIC:
        raise MappingCorruptError("not a Kuckuck mapping file (magic mismatch)")
    version = blob[4]
    key_id_len = blob[5]
    offset = 6
    if len(blob) < offset + key_id_len + 12:
        raise MappingCorruptError("mapping file truncated")
    key_id = blob[offset : offset + key_id_len].decode("utf-8")
    offset += key_id_len
    nonce = blob[offset : offset + 12]
    offset += 12
    ciphertext = blob[offset:]
    return version, key_id, nonce, ciphertext


def save_mapping(master: SecretStr, mapping: Mapping, path: Path | str) -> Path:
    """Encrypt *mapping* with *master* and write to *path*."""
    plaintext = json.dumps(
        {"entries": {token: entry.model_dump_json_safe() for token, entry in mapping.entries.items()}},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    nonce, ciphertext = encrypt_mapping_payload(master, plaintext)
    target = Path(path)
    target.write_bytes(_pack(mapping, nonce, ciphertext))
    return target


def load_mapping(master: SecretStr, path: Path | str) -> Mapping:
    """Read and decrypt *path*, returning a populated :class:`Mapping`."""
    blob = Path(path).read_bytes()
    version, key_id, nonce, ciphertext = _unpack(blob)
    if version != SCHEMA_VERSION:
        raise MappingCorruptError(f"unsupported mapping schema version: {version}")
    plaintext = decrypt_mapping_payload(master, nonce, ciphertext)
    payload = json.loads(plaintext.decode("utf-8"))
    entries = {token: MappingEntry(**entry) for token, entry in payload.get("entries", {}).items()}
    return Mapping(entries=entries, key_id=key_id)
