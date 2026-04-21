"""Denylist detector — forces literal strings to always be pseudonymized.

For small lists (≤ ``AHOCORASICK_THRESHOLD`` entries) the detector scans with a
pre-compiled alternation regex. Once the list grows past the threshold, it
falls back to :mod:`pyahocorasick`, which scales linearly in the input size
regardless of pattern count.
"""

from __future__ import annotations

import re
from typing import Iterable

import ahocorasick  # type: ignore[import-untyped]

from kuckuck.detectors.base import EntityType, Span

#: Switch-over point between regex alternation and Aho–Corasick.
AHOCORASICK_THRESHOLD = 1000


class DenylistDetector:
    """Case-sensitive, whole-string denylist detector.

    *entries* are de-duplicated and empty strings are skipped. Matching is
    case-sensitive by default to avoid accidentally pseudonymizing common
    English words that happen to overlap with short denylist entries.
    """

    name = "denylist"
    entity_type = EntityType.DENYLIST
    priority = 70

    def __init__(self, entries: Iterable[str]) -> None:
        clean = sorted({e for e in entries if e}, key=len, reverse=True)
        self._entries: tuple[str, ...] = tuple(clean)
        self._automaton: ahocorasick.Automaton | None = None
        self._regex: re.Pattern[str] | None = None
        if not self._entries:
            return
        if len(self._entries) > AHOCORASICK_THRESHOLD:
            automaton = ahocorasick.Automaton()
            for entry in self._entries:
                automaton.add_word(entry, entry)
            automaton.make_automaton()
            self._automaton = automaton
        else:
            self._regex = re.compile("|".join(re.escape(e) for e in self._entries))

    @property
    def entries(self) -> tuple[str, ...]:
        return self._entries

    def detect(self, text: str) -> list[Span]:
        if not self._entries:
            return []
        spans: list[Span] = []
        if self._automaton is not None:
            for end_index, value in self._automaton.iter(text):
                start = end_index - len(value) + 1
                spans.append(self._make_span(start, start + len(value), value))
        else:
            assert self._regex is not None
            for match in self._regex.finditer(text):
                spans.append(self._make_span(match.start(), match.end(), match.group(0)))
        return spans

    def _make_span(self, start: int, end: int, text: str) -> Span:
        return Span(
            start=start,
            end=end,
            text=text,
            entity_type=self.entity_type,
            detector_name=self.name,
            priority=self.priority,
        )
