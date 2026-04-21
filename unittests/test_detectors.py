"""Tests for built-in detectors and the span resolver."""

from __future__ import annotations

import pytest

from kuckuck.detectors import (
    DenylistDetector,
    EmailDetector,
    EntityType,
    HandleDetector,
    PhoneDetector,
    Span,
    resolve_spans,
)


class TestEmailDetector:
    def setup_method(self) -> None:
        self.det = EmailDetector()

    def test_simple_email(self) -> None:
        text = "Kontakt: max.mueller@firma.de bitte."
        spans = self.det.detect(text)
        assert len(spans) == 1
        assert spans[0].text == "max.mueller@firma.de"
        assert spans[0].entity_type == EntityType.EMAIL

    def test_multiple_emails(self) -> None:
        text = "a@b.de, c@d.de sowie eva.schmidt+filter@example.co.uk"
        texts = [s.text for s in self.det.detect(text)]
        assert set(texts) == {"a@b.de", "c@d.de", "eva.schmidt+filter@example.co.uk"}

    def test_no_email_returns_empty(self) -> None:
        assert self.det.detect("kein kontakt hier") == []

    def test_email_with_umlaut_domain_skipped(self) -> None:
        # We deliberately don't match IDN domains; 'über.de' won't be picked up.
        assert self.det.detect("mail@über.de") == []

    def test_positions_are_correct(self) -> None:
        text = "pre max@firma.de post"
        span = self.det.detect(text)[0]
        assert text[span.start : span.end] == "max@firma.de"


class TestPhoneDetector:
    def setup_method(self) -> None:
        self.det = PhoneDetector(default_region="DE")

    def test_german_landline_international(self) -> None:
        text = "Bitte rufen Sie +49 40 123456-78 an."
        spans = self.det.detect(text)
        assert len(spans) >= 1
        assert spans[0].entity_type == EntityType.PHONE

    def test_local_with_region(self) -> None:
        text = "Wir sind unter 040 12345678 erreichbar."
        spans = self.det.detect(text)
        assert len(spans) >= 1

    def test_plain_text_without_phone(self) -> None:
        assert self.det.detect("Dies ist ein Satz ohne Nummer.") == []


class TestHandleDetector:
    def setup_method(self) -> None:
        self.det = HandleDetector()

    def test_cloud_mention(self) -> None:
        spans = self.det.detect("bitte @max.mueller übernehmen")
        assert len(spans) == 1
        assert spans[0].text == "@max.mueller"
        assert spans[0].entity_type == EntityType.HANDLE

    def test_jira_account_id(self) -> None:
        spans = self.det.detect("Zugewiesen: [~accountid:5b10ac8d82e05b22cc7d4ef5]")
        assert len(spans) == 1
        assert "accountid" in spans[0].text

    def test_jira_server_style(self) -> None:
        spans = self.det.detect("[~mueller] bitte prüfen")
        assert len(spans) == 1

    def test_framework_annotations_are_skipped(self) -> None:
        text = "@Override @Component @Deprecated @pytest.fixture @pytest.mark.parametrize"
        assert self.det.detect(text) == []

    def test_npm_scope_skipped(self) -> None:
        assert self.det.detect("import foo from '@types/node'") == []

    def test_mention_after_email_not_captured_twice(self) -> None:
        # @-in-email should not re-trigger handle detection
        assert self.det.detect("user@firma.de schreibt") == []

    def test_mixed_valid_and_blocked(self) -> None:
        text = "@max.mueller und @Override"
        spans = self.det.detect(text)
        assert len(spans) == 1
        assert spans[0].text == "@max.mueller"

    def test_css_media_not_captured(self) -> None:
        assert self.det.detect("@media (min-width: 600px)") == []


class TestDenylistDetector:
    def test_empty_list_detects_nothing(self) -> None:
        assert DenylistDetector([]).detect("Kunde Alpha GmbH") == []

    def test_single_entry(self) -> None:
        det = DenylistDetector(["Alpha GmbH"])
        spans = det.detect("Wir haben Alpha GmbH als Kunden.")
        assert len(spans) == 1
        assert spans[0].text == "Alpha GmbH"
        assert spans[0].entity_type == EntityType.DENYLIST

    def test_multiple_entries(self) -> None:
        det = DenylistDetector(["Alpha GmbH", "Beta AG"])
        found = {s.text for s in det.detect("Alpha GmbH und Beta AG.")}
        assert found == {"Alpha GmbH", "Beta AG"}

    def test_case_sensitive(self) -> None:
        det = DenylistDetector(["Alpha"])
        assert det.detect("alpha ist klein") == []

    def test_regex_escaping(self) -> None:
        det = DenylistDetector(["a.b.c", "some+thing"])
        assert {s.text for s in det.detect("a.b.c abc some+thing sotxxnething")} == {"a.b.c", "some+thing"}

    def test_longest_first_for_overlapping_entries(self) -> None:
        det = DenylistDetector(["Alpha", "Alpha GmbH"])
        # Both match; the detector itself returns both matches.
        # The resolver's job is to pick the longer one.
        spans = det.detect("Wir haben Alpha GmbH als Kunden.")
        longest = max(spans, key=lambda s: s.length)
        assert longest.text == "Alpha GmbH"

    def test_deduplicates_entries(self) -> None:
        det = DenylistDetector(["Alpha", "Alpha"])
        assert det.entries == ("Alpha",)

    def test_dropping_empty_entries(self) -> None:
        det = DenylistDetector(["", "Beta"])
        assert det.entries == ("Beta",)


class TestResolveSpans:
    def _span(
        self,
        start: int,
        end: int,
        text: str = "x",
        *,
        entity_type: EntityType = EntityType.PERSON,
        priority: int = 0,
        detector_name: str = "mock",
    ) -> Span:
        return Span(
            start=start,
            end=end,
            text=text,
            entity_type=entity_type,
            detector_name=detector_name,
            priority=priority,
        )

    def test_empty_input(self) -> None:
        assert resolve_spans([]) == []

    def test_non_overlapping_spans_kept(self) -> None:
        a = self._span(0, 5)
        b = self._span(10, 15)
        result = resolve_spans([a, b])
        assert result == [a, b]

    def test_longest_span_wins(self) -> None:
        short = self._span(0, 5, priority=100)
        longer = self._span(0, 10, priority=10)
        result = resolve_spans([short, longer])
        assert result == [longer]

    def test_priority_breaks_length_tie(self) -> None:
        low = self._span(0, 5, priority=10, detector_name="a")
        high = self._span(0, 5, priority=100, detector_name="b")
        result = resolve_spans([low, high])
        assert result == [high]

    def test_position_breaks_double_tie(self) -> None:
        a = self._span(0, 5, priority=10, detector_name="a")
        b = self._span(10, 15, priority=10, detector_name="b")
        # no overlap — both stay
        result = resolve_spans([a, b])
        assert result == [a, b]

    def test_output_sorted_by_start(self) -> None:
        later = self._span(20, 25)
        earlier = self._span(5, 10)
        middle = self._span(12, 15)
        result = resolve_spans([later, earlier, middle])
        assert [s.start for s in result] == [5, 12, 20]

    def test_email_wins_over_substring_person(self) -> None:
        email = self._span(0, 20, text="max@firma.de", entity_type=EntityType.EMAIL, priority=100)
        person = self._span(0, 3, text="max", entity_type=EntityType.PERSON, priority=10)
        result = resolve_spans([email, person])
        assert result == [email]

    def test_partial_overlap(self) -> None:
        left = self._span(0, 10, priority=10)
        right = self._span(5, 15, priority=20)
        # right is same length but higher priority → right wins
        result = resolve_spans([left, right])
        assert result == [right]
