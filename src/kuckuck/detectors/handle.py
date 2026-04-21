"""Jira / Confluence user-handle detector.

Catches three mention patterns that escape regular email detection:

* ``@user.name``           — Jira/Confluence Cloud default mentions
* ``[~accountid:abc123]``  — Jira Cloud opaque account IDs
* ``[~user.name]``         — Jira Server mentions

False positives from framework annotations (``@Component``, ``@pytest.fixture``,
``@media``, ``@types/node``, …) are suppressed via a blocklist. Proper
code-block-aware filtering is the job of the Markdown/XML preprocessors in a
later release — until then the blocklist is the cheapest available guard.
"""

from __future__ import annotations

import re

from kuckuck.detectors.base import EntityType, Span

#: ``@user.name`` style — lowercase first char, at least 3 chars total.
_MENTION_RE = re.compile(r"(?<![\w.@/])@([a-z][\w.\-]{2,})")

#: ``[~accountid:xyz]`` style.
_ACCOUNT_RE = re.compile(r"\[~accountid:[a-f0-9:\-]+\]")

#: ``[~username]`` style (Jira Server).
_SHORT_RE = re.compile(r"\[~[\w.\-]+\]")

#: Framework / language annotations that would otherwise be treated as mentions.
_ANNOTATION_BLOCKLIST = frozenset(
    {
        "Component",
        "Override",
        "Deprecated",
        "Nullable",
        "NonNull",
        "SuppressWarnings",
        "Inject",
        "Autowired",
        "param",
        "params",
        "return",
        "returns",
        "throws",
        "throw",
        "raise",
        "raises",
        "see",
        "since",
        "version",
        "author",
        "deprecated",
        "media",
        "import",
        "include",
        "charset",
        "supports",
        "keyframes",
        "font-face",
        "pytest",
        "pytest.fixture",
        "pytest.mark",
        "dataclass",
        "property",
        "staticmethod",
        "classmethod",
        "override",
        "final",
        "abstract",
    }
)

#: Package-manager scope prefixes (``@types/node``, ``@angular/core``).
_SCOPE_PREFIXES = ("types/", "angular/", "babel/", "vue/", "nestjs/", "nuxt/")


def _is_blocked_mention(captured: str, text: str, match_end: int) -> bool:
    if captured in _ANNOTATION_BLOCKLIST:
        return True
    if captured.split(".", 1)[0] in _ANNOTATION_BLOCKLIST:
        return True
    # ``@types/node`` and other package-manager scope prefixes — detected by
    # looking at the character directly after the match: a trailing ``/``
    # signals we've captured a scope name, not a user mention.
    if match_end < len(text) and text[match_end] == "/":
        return True
    for prefix in _SCOPE_PREFIXES:
        if captured.startswith(prefix):
            return True
    return False


class HandleDetector:
    """Regex-based handle detector covering Jira Cloud, Jira Server, and Confluence mentions."""

    name = "handle"
    entity_type = EntityType.HANDLE
    priority = 80

    def detect(self, text: str) -> list[Span]:
        spans: list[Span] = []
        for match in _MENTION_RE.finditer(text):
            if _is_blocked_mention(match.group(1), text, match.end()):
                continue
            spans.append(
                Span(
                    start=match.start(),
                    end=match.end(),
                    text=match.group(0),
                    entity_type=self.entity_type,
                    detector_name=self.name,
                    priority=self.priority,
                )
            )
        for pattern in (_ACCOUNT_RE, _SHORT_RE):
            for match in pattern.finditer(text):
                spans.append(
                    Span(
                        start=match.start(),
                        end=match.end(),
                        text=match.group(0),
                        entity_type=self.entity_type,
                        detector_name=self.name,
                        priority=self.priority,
                    )
                )
        return spans
