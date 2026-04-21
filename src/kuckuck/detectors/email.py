"""Email address detector — candidate regex plus :mod:`email_validator` vetting.

We cannot use ``email_validator`` alone because it validates *single* addresses,
not free-form text. The regex sweeps for ``local@domain.tld``-shaped candidates;
each candidate is then passed through :func:`email_validator.validate_email` so
only RFC-conforming addresses survive. This gives us the recall of a regex
together with the precision of a maintained validation library.
"""

from __future__ import annotations

import re

from email_validator import EmailNotValidError, validate_email

from kuckuck.detectors.base import EntityType, Priority, Span

#: Greedy candidate pattern — anything that looks roughly ``local@domain.tld``.
#: Validation is delegated to :func:`email_validator.validate_email`.
_EMAIL_CANDIDATE_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,}"
)


class EmailDetector:
    """Regex-based email detector with :mod:`email_validator`-backed vetting."""

    name = "email"
    entity_type = EntityType.EMAIL
    priority = Priority.EMAIL

    def detect(self, text: str) -> list[Span]:
        """Return every email address found in *text*."""
        spans: list[Span] = []
        for match in _EMAIL_CANDIDATE_RE.finditer(text):
            candidate = match.group(0)
            try:
                validate_email(candidate, check_deliverability=False)
            except EmailNotValidError:
                continue
            spans.append(
                Span(
                    start=match.start(),
                    end=match.end(),
                    text=candidate,
                    entity_type=self.entity_type,
                    detector_name=self.name,
                    priority=self.priority,
                )
            )
        return spans
