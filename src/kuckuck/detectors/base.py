"""Core detector primitives — :class:`Span`, :class:`EntityType`, :class:`Detector` protocol."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class EntityType(StrEnum):
    """Supported entity types.

    ``StrEnum`` (stdlib, 3.11+) gives us JSON-serialisation-friendly string
    values while still allowing enum identity checks. New members can be
    added without breaking persistence — the stored mapping keeps the raw
    string value.

    ``TERM`` covers detections from the user-configured denylist (customer
    names, project codenames). The enum value is deliberately short and
    generic because it is the token prefix that the LLM sees in the
    pseudonymized text — ``[[TERM_...]]`` leaks less implementation detail
    than ``[[DENYLIST_...]]`` would.
    """

    PERSON = "PERSON"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    HANDLE = "HANDLE"
    TERM = "TERM"


class Priority:
    """Default resolver priorities.

    Higher = wins ties in :func:`resolve_spans`. Grouped here so individual
    detectors don't re-invent the ranking every time they're added.
    """

    EMAIL = 100
    PHONE = 90
    HANDLE = 80
    TERM = 70
    PERSON = 10  # NER detector — lowest priority, yields to regex matches on the same span.


class Span(BaseModel):
    """An inclusive/exclusive character span matched by a detector.

    Frozen so span instances can live in sets and be compared by value.
    ``start`` and ``end`` follow Python slice conventions: ``text[start:end]``
    reproduces :attr:`text`. ``priority`` is used by :func:`resolve_spans` to
    break ties between overlapping matches — higher wins.
    """

    model_config = ConfigDict(frozen=True)

    start: int = Field(description="Inclusive start offset into the source text.")
    end: int = Field(description="Exclusive end offset into the source text.")
    text: str = Field(description="The matched substring (equal to source[start:end]).")
    entity_type: EntityType = Field(description="The kind of entity this span represents.")
    detector_name: str = Field(description="Name of the detector that produced this span.")
    priority: int = Field(default=0, description="Resolver tiebreaker — higher wins.")

    @property
    def length(self) -> int:
        """Character length of the span (``end - start``)."""
        return self.end - self.start

    def overlaps(self, other: Span) -> bool:
        """Return ``True`` when *other* shares at least one character position."""
        return self.start < other.end and other.start < self.end


class Detector(Protocol):
    """Minimal detector interface. Production detectors also expose ``name``."""

    name: str
    entity_type: EntityType
    priority: int

    def detect(self, text: str) -> list[Span]:  # pragma: no cover - protocol
        """Return all matches found in *text*, in any order."""
