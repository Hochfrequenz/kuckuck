"""End-to-end tests for pseudonymize / restore, with snapshot and round-trip coverage."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import SecretStr

from kuckuck.mapping import Mapping
from kuckuck.pseudonymize import (
    build_default_detectors,
    pseudonymize_text,
    restore_text,
)

MASTER = SecretStr("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff")


class TestBuildDefaultDetectors:
    def test_default_has_email_phone_handle(self) -> None:
        names = {d.name for d in build_default_detectors()}
        assert {"email", "phone", "handle"}.issubset(names)

    def test_denylist_included_when_provided(self) -> None:
        names = {d.name for d in build_default_detectors(denylist=["Alpha"])}
        assert "denylist" in names

    def test_denylist_skipped_when_empty(self) -> None:
        names = {d.name for d in build_default_detectors(denylist=[])}
        assert "denylist" not in names


class TestPseudonymizeText:
    def test_email_is_replaced(self) -> None:
        result = pseudonymize_text("Kontakt: max@firma.de", MASTER)
        assert "max@firma.de" not in result.text
        assert "[[EMAIL_" in result.text
        assert len(result.replaced) == 1

    def test_phone_is_replaced(self) -> None:
        result = pseudonymize_text("Ruf unter +49 40 12345-67 an", MASTER)
        assert "[[PHONE_" in result.text

    def test_handle_is_replaced(self) -> None:
        result = pseudonymize_text("cc @max.mueller", MASTER)
        assert "[[HANDLE_" in result.text
        assert "@max.mueller" not in result.text

    def test_no_entities_unchanged(self) -> None:
        text = "Ein ganz normaler Satz ohne Daten."
        result = pseudonymize_text(text, MASTER)
        assert result.text == text
        assert result.replaced == []

    def test_mapping_entries_populated(self) -> None:
        result = pseudonymize_text("max@firma.de und @eva", MASTER)
        assert len(result.mapping.entries) == 2
        originals = {e.original for e in result.mapping.entries.values()}
        assert "max@firma.de" in originals

    def test_same_value_reuses_token(self) -> None:
        result = pseudonymize_text("max@firma.de max@firma.de", MASTER)
        # both occurrences map to identical token text
        first = result.text.split()[0]
        second = result.text.split()[1]
        assert first == second
        assert len(result.mapping.entries) == 1

    def test_cross_doc_mapping_reuse(self) -> None:
        """Passing the same mapping to two documents keeps the token stable."""
        shared = Mapping()
        r1 = pseudonymize_text("max@firma.de schreibt", MASTER, mapping=shared)
        r2 = pseudonymize_text("antwort an max@firma.de", MASTER, mapping=shared)
        # Extract the EMAIL token from both outputs
        import re

        token_re = re.compile(r"\[\[EMAIL_([^\]]+)\]\]")
        t1 = token_re.search(r1.text).group(1)  # type: ignore[union-attr]
        t2 = token_re.search(r2.text).group(1)  # type: ignore[union-attr]
        assert t1 == t2

    def test_idempotency_skips_own_tokens(self) -> None:
        """Running the pipeline twice must not double-pseudonymize tokens."""
        first = pseudonymize_text("max@firma.de", MASTER).text
        second = pseudonymize_text(first, MASTER).text
        assert first == second

    def test_sequential_mode(self) -> None:
        text = "a@b.de c@d.de"
        result = pseudonymize_text(text, MASTER, sequential_tokens=True)
        assert "[[EMAIL_1]]" in result.text
        assert "[[EMAIL_2]]" in result.text

    def test_overlap_resolved_longest_wins(self) -> None:
        # '@max@firma.de' would match both the email and the handle regexes;
        # the resolver picks the longer email span.
        text = "ping @max@firma.de bitte"
        result = pseudonymize_text(text, MASTER)
        # After resolution: the email captures 'max@firma.de', the '@' before stays.
        assert "[[EMAIL_" in result.text
        assert "[[HANDLE_" not in result.text


class TestRestoreText:
    def test_round_trip_matches_input(self) -> None:
        text = "Kontakt: max@firma.de, cc @eva.schmidt"
        p = pseudonymize_text(text, MASTER)
        restored = restore_text(p.text, p.mapping)
        assert restored == text

    def test_unknown_token_left_alone(self) -> None:
        mapping = Mapping()
        out = restore_text("vor [[PERSON_deadbeef]] nach", mapping)
        assert out == "vor [[PERSON_deadbeef]] nach"

    def test_partial_mapping_restores_known_only(self) -> None:
        text = "max@firma.de und @unbekannt.user"
        p = pseudonymize_text(text, MASTER)
        # Remove one entry to simulate incomplete mapping delivered back from LLM
        handle_tokens = p.mapping.tokens_by_type("HANDLE")
        for t in handle_tokens:
            del p.mapping.entries[t]
        out = restore_text(p.text, p.mapping)
        assert "max@firma.de" in out
        assert "[[HANDLE_" in out  # still placeholder

    def test_counter_suffixed_tokens_restored(self) -> None:
        from kuckuck.mapping import MappingEntry

        mapping = Mapping()
        mapping.entries["abcdef12"] = MappingEntry(original="Alice", entity_type="PERSON")
        mapping.entries["abcdef12-2"] = MappingEntry(original="Bob", entity_type="PERSON")
        text = "[[PERSON_abcdef12]] und [[PERSON_abcdef12-2]]"
        assert restore_text(text, mapping) == "Alice und Bob"


class TestSnapshot:
    @pytest.mark.snapshot
    def test_email_output_snapshot(self, snapshot):  # type: ignore[no-untyped-def]
        text = "Kontakt: max.mueller@firma.de"
        result = pseudonymize_text(text, MASTER)
        assert result.text == snapshot

    @pytest.mark.snapshot
    def test_mixed_entities_snapshot(self, snapshot):  # type: ignore[no-untyped-def]
        text = (
            "Sehr geehrte Damen und Herren,\n"
            "bitte wenden Sie sich an max.mueller@firma.de oder +49 40 123456-78.\n"
            "Bei Rückfragen cc @eva.schmidt oder [~accountid:5b10ac8d82e05b22cc7d4ef5].\n"
            "Mit freundlichen Grüßen"
        )
        result = pseudonymize_text(text, MASTER)
        assert result.text == snapshot

    @pytest.mark.snapshot
    def test_with_denylist(self, snapshot):  # type: ignore[no-untyped-def]
        text = "Unser Kunde Alpha GmbH hat einen Bug gemeldet bei support@alpha.de"
        detectors = build_default_detectors(denylist=["Alpha GmbH"])
        result = pseudonymize_text(text, MASTER, detectors)
        assert result.text == snapshot


class TestRoundTripProperty:
    @given(
        st.lists(
            st.one_of(
                st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=10).map(lambda s: f"{s}@firma.de"),
                st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=3, max_size=10).map(lambda s: f"@{s}"),
                st.text(alphabet="abcdefghijklmnopqrstuvwxyzäöüÄÖÜß ", min_size=1, max_size=20),
            ),
            min_size=0,
            max_size=6,
        )
    )
    @settings(max_examples=30, deadline=1000)
    def test_round_trip_preserves_text(self, fragments: list[str]) -> None:
        text = " / ".join(fragments)
        p = pseudonymize_text(text, MASTER)
        restored = restore_text(p.text, p.mapping)
        assert restored == text
