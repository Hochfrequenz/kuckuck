"""Phone-number detector backed by Google's libphonenumber (Python port)."""

from __future__ import annotations

import phonenumbers

from kuckuck.detectors.base import EntityType, Span


class PhoneDetector:
    """Finds phone numbers via :mod:`phonenumbers`.

    ``default_region`` controls how ambiguous/local-only numbers are parsed.
    ``"DE"`` (the default) is appropriate for German-language documents; set
    it to another ISO country code at construction time if the corpus differs.
    """

    name = "phone"
    entity_type = EntityType.PHONE
    priority = 90

    def __init__(self, default_region: str = "DE") -> None:
        self.default_region = default_region

    def detect(self, text: str) -> list[Span]:
        matches = phonenumbers.PhoneNumberMatcher(text, self.default_region)
        spans: list[Span] = []
        for match in matches:
            spans.append(
                Span(
                    start=match.start,
                    end=match.end,
                    text=match.raw_string,
                    entity_type=self.entity_type,
                    detector_name=self.name,
                    priority=self.priority,
                )
            )
        return spans
