"""Span-resolver that turns a union of detector outputs into a non-overlapping set.

Conflict-resolution rules, in order:

1. **Longest span wins.** ``alice@example.com`` from ``EmailDetector`` beats
   ``alice`` from a hypothetical NER that only caught the local part.
2. **Higher priority wins on ties in length.** Priority is set per detector
   (see :class:`~kuckuck.detectors.base.Detector`).
3. **Earlier position wins** as a final deterministic tiebreaker — removes
   any ordering dependence on the iteration order of the source detectors.

The output is sorted by ``start`` so callers can walk it in document order.
"""

from __future__ import annotations

from typing import Iterable

from kuckuck.detectors.base import Span


def resolve_spans(spans: Iterable[Span]) -> list[Span]:
    """Return a non-overlapping subset of *spans* following the rules above."""
    ordered = sorted(spans, key=lambda s: (-s.length, -s.priority, s.start, s.detector_name))
    accepted: list[Span] = []
    for span in ordered:
        if any(span.overlaps(existing) for existing in accepted):
            continue
        accepted.append(span)
    accepted.sort(key=lambda s: s.start)
    return accepted
