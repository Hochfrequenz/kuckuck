"""Email address detector — pragmatic RFC-5322-flavoured regex.

Not a full parser. Aims for high recall against the kind of addresses that
actually appear in emails, tickets, and wiki exports. Users with exotic
addresses can lower the priority and supply their own detector via the
plugin mechanism.
"""

from __future__ import annotations

import re

from kuckuck.detectors.base import EntityType, Span

#: Matches typical email local-part@domain.tld with optional +tags and dots.
_EMAIL_RE = re.compile(
    r"""
    (?<![\w.+-])                   # no alnum/dot before — avoids matching URL fragments
    [A-Za-z0-9][A-Za-z0-9._%+\-]*  # local-part: start with alnum
    @
    [A-Za-z0-9]                    # domain: start with alnum
    (?:[A-Za-z0-9\-]*[A-Za-z0-9])?
    (?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)+
    """,
    re.VERBOSE,
)


class EmailDetector:
    """Regex-based email detector. Priority 100 — highest among built-ins."""

    name = "email"
    entity_type = EntityType.EMAIL
    priority = 100

    def detect(self, text: str) -> list[Span]:
        return [
            Span(
                start=m.start(),
                end=m.end(),
                text=m.group(0),
                entity_type=self.entity_type,
                detector_name=self.name,
                priority=self.priority,
            )
            for m in _EMAIL_RE.finditer(text)
        ]
