"""Tests for the encrypted mapping module — allocation, persistence, collisions."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from pydantic import SecretStr

from kuckuck.crypto import hmac_token
from kuckuck.mapping import (
    MAGIC,
    SCHEMA_VERSION,
    Mapping,
    MappingCorruptError,
    MappingEntry,
    load_mapping,
    save_mapping,
)

MASTER = SecretStr("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff")
OTHER = SecretStr("ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100")


class TestGetOrAllocate:
    def test_new_name_creates_entry(self) -> None:
        m = Mapping()
        token = m.get_or_allocate(MASTER, original="Max Müller", entity_type="PERSON")
        assert len(m.entries) == 1
        assert m.entries[token].original == "Max Müller"
        assert m.entries[token].entity_type == "PERSON"

    def test_same_name_returns_same_token(self) -> None:
        m = Mapping()
        first = m.get_or_allocate(MASTER, original="Max Müller", entity_type="PERSON")
        second = m.get_or_allocate(MASTER, original="Max Müller", entity_type="PERSON")
        assert first == second
        assert len(m.entries) == 1

    def test_different_names_get_different_tokens(self) -> None:
        m = Mapping()
        a = m.get_or_allocate(MASTER, original="Max Müller", entity_type="PERSON")
        b = m.get_or_allocate(MASTER, original="Eva Schmidt", entity_type="PERSON")
        assert a != b
        assert len(m.entries) == 2

    def test_token_matches_hmac(self) -> None:
        m = Mapping()
        token = m.get_or_allocate(MASTER, original="Max Müller", entity_type="PERSON")
        assert token == hmac_token(MASTER, "Max Müller")

    def test_nfc_and_nfd_collide_intentionally(self) -> None:
        # NFC "Müller" and NFD "Müller" produce the same token because
        # crypto.normalize is applied before hashing.
        m = Mapping()
        nfc = m.get_or_allocate(MASTER, original="Müller", entity_type="PERSON")
        nfd = m.get_or_allocate(MASTER, original="Müller", entity_type="PERSON")
        assert nfc == nfd

    def test_collision_disambiguates_with_counter(self) -> None:
        """Manual collision injection to exercise the counter-suffix path."""
        m = Mapping()
        real_token = m.get_or_allocate(MASTER, original="Max Müller", entity_type="PERSON")

        # Inject a fake "other" entry with the same short hash as 'real_token'
        # by direct dict manipulation — simulates a collision.
        m.entries[real_token] = MappingEntry(original="Max Müller", entity_type="PERSON")
        # A second original that somehow maps to the same truncated hash
        # cannot be triggered naturally without brute force, so the collision
        # path is tested by crafting a name whose allocated slot is already
        # taken by an unrelated original value.
        token_for_eva = m.get_or_allocate(MASTER, original="Eva Schmidt", entity_type="PERSON")
        m.entries[token_for_eva] = MappingEntry(original="Faker", entity_type="PERSON")
        disambiguated = m.get_or_allocate(MASTER, original="Eva Schmidt", entity_type="PERSON")
        assert disambiguated.startswith(token_for_eva)
        assert "-" in disambiguated


class TestResolveToken:
    def test_known_token_returns_entry(self) -> None:
        m = Mapping()
        token = m.get_or_allocate(MASTER, original="Max Müller", entity_type="PERSON")
        entry = m.resolve_token(token)
        assert entry is not None
        assert entry.original == "Max Müller"

    def test_unknown_token_returns_none(self) -> None:
        assert Mapping().resolve_token("deadbeef") is None


class TestPersistence:
    def test_round_trip(self, tmp_path: Path) -> None:
        m = Mapping(key_id="v1")
        m.get_or_allocate(MASTER, original="Max Müller", entity_type="PERSON")
        m.get_or_allocate(MASTER, original="max@firma.de", entity_type="EMAIL")
        target = tmp_path / "doc.kuckuck-map.enc"
        save_mapping(MASTER, m, target)
        loaded = load_mapping(MASTER, target)
        assert loaded.entries == m.entries
        assert loaded.key_id == "v1"

    def test_empty_mapping_round_trips(self, tmp_path: Path) -> None:
        m = Mapping()
        target = tmp_path / "empty.enc"
        save_mapping(MASTER, m, target)
        loaded = load_mapping(MASTER, target)
        assert len(loaded) == 0

    def test_wrong_master_fails(self, tmp_path: Path) -> None:
        m = Mapping()
        m.get_or_allocate(MASTER, original="Max Müller", entity_type="PERSON")
        target = tmp_path / "doc.enc"
        save_mapping(MASTER, m, target)
        with pytest.raises(InvalidTag):
            load_mapping(OTHER, target)

    def test_tampered_file_fails(self, tmp_path: Path) -> None:
        m = Mapping()
        m.get_or_allocate(MASTER, original="Max Müller", entity_type="PERSON")
        target = tmp_path / "doc.enc"
        save_mapping(MASTER, m, target)

        blob = bytearray(target.read_bytes())
        blob[-1] ^= 0xFF  # flip last byte of ciphertext
        target.write_bytes(bytes(blob))
        with pytest.raises(InvalidTag):
            load_mapping(MASTER, target)

    def test_unsupported_schema_version_rejected(self, tmp_path: Path) -> None:
        m = Mapping()
        m.get_or_allocate(MASTER, original="Max Müller", entity_type="PERSON")
        target = tmp_path / "v99.enc"
        save_mapping(MASTER, m, target)

        blob = bytearray(target.read_bytes())
        blob[4] = 99  # schema version byte — no longer == SCHEMA_VERSION
        assert blob[4] != SCHEMA_VERSION
        target.write_bytes(bytes(blob))
        with pytest.raises(MappingCorruptError, match="schema version"):
            load_mapping(MASTER, target)

    def test_key_id_length_limit_enforced(self, tmp_path: Path) -> None:
        m = Mapping(key_id="x" * 256)
        target = tmp_path / "big.enc"
        with pytest.raises(ValueError, match="255 bytes"):
            save_mapping(MASTER, m, target)

    def test_non_magic_file_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "bogus.enc"
        target.write_bytes(b"NOT-A-KUCKUCK-FILE")
        with pytest.raises(MappingCorruptError, match="magic"):
            load_mapping(MASTER, target)

    def test_saved_file_starts_with_magic(self, tmp_path: Path) -> None:
        m = Mapping()
        target = tmp_path / "doc.enc"
        save_mapping(MASTER, m, target)
        assert target.read_bytes().startswith(MAGIC)

    def test_truncated_file_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "truncated.enc"
        target.write_bytes(MAGIC + b"\x01\x00")  # header only, no nonce/ct
        with pytest.raises(MappingCorruptError, match="truncated"):
            load_mapping(MASTER, target)

    def test_merge_preserves_old_entries(self, tmp_path: Path) -> None:
        m = Mapping()
        m.get_or_allocate(MASTER, original="Alice", entity_type="PERSON")
        target = tmp_path / "m.enc"
        save_mapping(MASTER, m, target)

        # Reload, add new entry, save again — original entry still there
        reloaded = load_mapping(MASTER, target)
        reloaded.get_or_allocate(MASTER, original="Bob", entity_type="PERSON")
        save_mapping(MASTER, reloaded, target)

        final = load_mapping(MASTER, target)
        originals = {e.original for e in final.entries.values()}
        assert originals == {"Alice", "Bob"}

    def test_key_id_preserved(self, tmp_path: Path) -> None:
        m = Mapping(key_id="team-2026-Q2")
        target = tmp_path / "m.enc"
        save_mapping(MASTER, m, target)
        assert load_mapping(MASTER, target).key_id == "team-2026-Q2"
