"""Tests for crypto primitives — HMAC determinism, NFC normalization, AES-GCM round-trip."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from kuckuck.crypto import (
    AES_NONCE_BYTES,
    HMAC_HEX_LENGTH,
    decrypt_mapping_payload,
    derive_hmac_key,
    derive_map_key,
    encrypt_mapping_payload,
    full_hmac,
    hmac_token,
    normalize,
)

MASTER_HEX = SecretStr("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff")
OTHER_MASTER = SecretStr("ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100")


class TestNormalize:
    def test_nfc_composed_string_is_unchanged(self) -> None:
        assert normalize("Müller") == "Müller"

    def test_nfd_string_is_composed(self) -> None:
        # 'Mu' + combining diaeresis U+0308 → 'Mü' composed
        decomposed = "Müller"
        assert normalize(decomposed) == "Müller"

    def test_ascii_is_identity(self) -> None:
        assert normalize("Alice Johnson") == "Alice Johnson"


class TestHmacToken:
    def test_is_stable_across_calls(self) -> None:
        first = hmac_token(MASTER_HEX, "Max Müller")
        second = hmac_token(MASTER_HEX, "Max Müller")
        assert first == second

    def test_has_expected_length(self) -> None:
        assert len(hmac_token(MASTER_HEX, "anything")) == HMAC_HEX_LENGTH

    def test_output_is_lowercase_hex(self) -> None:
        token = hmac_token(MASTER_HEX, "Max Müller")
        assert all(c in "0123456789abcdef" for c in token)

    def test_different_input_differs(self) -> None:
        assert hmac_token(MASTER_HEX, "Max Müller") != hmac_token(MASTER_HEX, "Eva Schmidt")

    def test_different_master_differs(self) -> None:
        assert hmac_token(MASTER_HEX, "Max Müller") != hmac_token(OTHER_MASTER, "Max Müller")

    def test_nfc_and_nfd_match(self) -> None:
        composed = hmac_token(MASTER_HEX, "Müller")
        decomposed = hmac_token(MASTER_HEX, "Müller")
        assert composed == decomposed

    def test_passphrase_master_is_accepted(self) -> None:
        # Non-hex master should be treated as UTF-8 passphrase
        passphrase = SecretStr("a simple passphrase")
        token = hmac_token(passphrase, "Max Müller")
        assert len(token) == HMAC_HEX_LENGTH


class TestFullHmac:
    def test_is_64_hex_chars(self) -> None:
        assert len(full_hmac(MASTER_HEX, "anything")) == 64

    def test_truncates_to_hmac_token(self) -> None:
        assert full_hmac(MASTER_HEX, "Max Müller")[:HMAC_HEX_LENGTH] == hmac_token(MASTER_HEX, "Max Müller")


class TestKeyDerivation:
    def test_hmac_and_map_subkeys_differ(self) -> None:
        assert derive_hmac_key(MASTER_HEX) != derive_map_key(MASTER_HEX)

    def test_hmac_subkey_stable(self) -> None:
        assert derive_hmac_key(MASTER_HEX) == derive_hmac_key(MASTER_HEX)

    def test_map_subkey_is_32_bytes(self) -> None:
        assert len(derive_map_key(MASTER_HEX)) == 32


class TestMappingEncryption:
    def test_round_trip(self) -> None:
        plaintext = b'{"hash": "Max Mueller"}'
        nonce, ct = encrypt_mapping_payload(MASTER_HEX, plaintext)
        assert len(nonce) == AES_NONCE_BYTES
        assert ct != plaintext
        out = decrypt_mapping_payload(MASTER_HEX, nonce, ct)
        assert out == plaintext

    def test_decrypt_with_wrong_master_fails(self) -> None:
        plaintext = b"secret"
        nonce, ct = encrypt_mapping_payload(MASTER_HEX, plaintext)
        with pytest.raises(Exception):  # InvalidTag from cryptography
            decrypt_mapping_payload(OTHER_MASTER, nonce, ct)

    def test_each_encryption_uses_fresh_nonce(self) -> None:
        plaintext = b"same input"
        nonce_a, ct_a = encrypt_mapping_payload(MASTER_HEX, plaintext)
        nonce_b, ct_b = encrypt_mapping_payload(MASTER_HEX, plaintext)
        assert nonce_a != nonce_b
        assert ct_a != ct_b

    def test_tampered_ciphertext_fails(self) -> None:
        plaintext = b"secret"
        nonce, ct = encrypt_mapping_payload(MASTER_HEX, plaintext)
        tampered = bytes([ct[0] ^ 0xFF]) + ct[1:]
        with pytest.raises(Exception):
            decrypt_mapping_payload(MASTER_HEX, nonce, tampered)
