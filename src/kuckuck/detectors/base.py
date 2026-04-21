"""Core detector primitives — :class:`Span`, :class:`EntityType`, :class:`Detector` protocol."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class EntityType(str, Enum):
    """Supported entity types. New members can be added without breaking persistence."""

    PERSON = "PERSON"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    HANDLE = "HANDLE"
    DENYLIST = "DENYLIST"


@dataclass(frozen=True)
class Span:
    """An inclusive/exclusive character span matched by a detector.

    ``start`` and ``end`` follow Python slice conventions: ``text[start:end]``
    yields :attr:`text`. ``priority`` is used by :func:`resolve_spans` to break
    ties between overlapping matches — higher wins.
    """

    start: int
    end: int
    text: str
    entity_type: EntityType
    detector_name: str
    priority: int = 0

    @property
    def length(self) -> int:
        """Character length of the span (``end - start``)."""
        return self.end - self.start

    def overlaps(self, other: "Span") -> bool:
        """Return ``True`` when *other* shares at least one character position."""
        return self.start < other.end and other.start < self.end


class Detector(Protocol):
    """Minimal detector interface. Production detectors also expose ``name``."""

    name: str
    entity_type: EntityType
    priority: int

    def detect(self, text: str) -> list[Span]:  # pragma: no cover - protocol
        """Return all matches found in *text*, in any order."""
