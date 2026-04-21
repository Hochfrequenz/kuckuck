"""High-level pseudonymize / restore pipeline."""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from kuckuck.detectors.base import Detector, EntityType, Span
from kuckuck.detectors.denylist import DenylistDetector
from kuckuck.detectors.email import EmailDetector
from kuckuck.detectors.handle import HandleDetector
from kuckuck.detectors.phone import PhoneDetector
from kuckuck.detectors.resolver import resolve_spans
from kuckuck.mapping import Mapping, MappingEntry

#: The wire format for a single pseudonym occurrence in the output text.
TOKEN_TEMPLATE = "[[{entity}_{token}]]"

#: Matches tokens produced by :func:`pseudonymize_text` so we can skip them
#: on subsequent passes (idempotency). Accepts the optional ``-N`` collision
#: counter as well as pure-numeric tokens from sequential mode.
_OWN_TOKEN_RE = re.compile(r"\[\[(?P<entity>[A-Z]+)_(?P<token>[a-z0-9]+(?:-\d+)?)\]\]")


def build_default_detectors(*, denylist: list[str] | None = None, phone_region: str = "DE") -> list[Detector]:
    """Return the built-in detector set for the MVP regex pipeline."""
    detectors: list[Detector] = [
        EmailDetector(),
        PhoneDetector(default_region=phone_region),
        HandleDetector(),
    ]
    if denylist:
        detectors.append(DenylistDetector(denylist))
    return detectors


logger = logging.getLogger(__name__)


class PseudonymizeResult(BaseModel):
    """Return value of :func:`pseudonymize_text`.

    :attr:`text` is the pseudonymized text, :attr:`mapping` is the updated
    :class:`~kuckuck.mapping.Mapping`, and :attr:`replaced` lists every span
    that was replaced (for use in review logs).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    text: str = Field(description="The pseudonymized text.")
    mapping: Mapping = Field(description="The mapping updated with every new allocation.")
    replaced: list[Span] = Field(default_factory=list, description="Spans replaced by tokens, in document order.")


def _find_own_tokens(text: str) -> list[Span]:
    """Return existing Kuckuck tokens in *text* so we don't re-pseudonymize them."""
    spans: list[Span] = []
    for match in _OWN_TOKEN_RE.finditer(text):
        # Own tokens are just structural markers; entity type enum is only
        # relevant for downstream detectors. Build a dummy span per match.
        spans.append(
            Span(
                start=match.start(),
                end=match.end(),
                text=match.group(0),
                entity_type=EntityType.PERSON,
                detector_name="__own_token__",
                priority=9999,
            )
        )
    return spans


def _allocate_token(
    *,
    master: SecretStr,
    mapping: Mapping,
    span: Span,
    sequential_counters: dict[EntityType, int] | None,
) -> str:
    """Return the token suffix for *span*; updates *mapping* and counters.

    Sequential-mode callers pass a dict that is populated on first use from
    the highest existing numeric token in *mapping* per entity type. This
    prevents two calls that share a mapping from silently overwriting each
    other's allocations.
    """
    if sequential_counters is None:
        return mapping.get_or_allocate(master, original=span.text, entity_type=span.entity_type.value)

    entity_type_str = span.entity_type.value
    normalized = span.text
    # Reverse lookup — reuse an existing sequential token for the same value
    # so a shared mapping stays consistent across sequential-mode calls.
    for token, entry in mapping.entries.items():
        if token.isdigit() and entry.entity_type == entity_type_str and entry.original == normalized:
            return token

    if span.entity_type not in sequential_counters:
        existing = [
            int(tok) for tok, entry in mapping.entries.items() if tok.isdigit() and entry.entity_type == entity_type_str
        ]
        sequential_counters[span.entity_type] = max(existing, default=0)

    sequential_counters[span.entity_type] += 1
    token = str(sequential_counters[span.entity_type])
    mapping.entries[token] = MappingEntry(original=normalized, entity_type=entity_type_str)
    return token


def pseudonymize_text(  # pylint: disable=too-many-locals
    text: str,
    master: SecretStr,
    detectors: list[Detector] | None = None,
    *,
    mapping: Mapping | None = None,
    sequential_tokens: bool = False,
) -> PseudonymizeResult:
    """Pseudonymize *text* in-memory and return the result.

    *mapping* can be passed to merge with an existing mapping (cross-document
    consistency). When *sequential_tokens* is ``True``, tokens are assigned as
    sequential per-type counters within the current document instead of HMAC
    fingerprints — this loses cross-doc stability but keeps the output short.
    """
    if detectors is None:
        detectors = build_default_detectors()
    if mapping is None:
        mapping = Mapping()

    own_spans = _find_own_tokens(text)
    raw_spans: list[Span] = []
    for det in detectors:
        raw_spans.extend(det.detect(text))
    filtered = [s for s in raw_spans if not any(s.overlaps(own) for own in own_spans)]
    resolved = resolve_spans(filtered)

    sequential_counters: dict[EntityType, int] | None = {} if sequential_tokens else None
    replaced_ordered: list[Span] = []
    output_chunks: list[str] = []
    cursor = 0
    for span in resolved:
        output_chunks.append(text[cursor : span.start])
        token = _allocate_token(
            master=master,
            mapping=mapping,
            span=span,
            sequential_counters=sequential_counters,
        )
        output_chunks.append(TOKEN_TEMPLATE.format(entity=span.entity_type.value, token=token))
        replaced_ordered.append(span)
        cursor = span.end
    output_chunks.append(text[cursor:])
    return PseudonymizeResult(text="".join(output_chunks), mapping=mapping, replaced=replaced_ordered)


def restore_text(text: str, mapping: Mapping) -> str:
    """Return *text* with every known Kuckuck token replaced by its original value.

    Unknown tokens are left intact — callers can detect them by diffing
    against the pseudonymized input. This keeps the function total and avoids
    raising on partial mappings (e.g. when an LLM roundtrip drops some tokens).
    """

    def _sub(match: re.Match[str]) -> str:
        entry = mapping.resolve_token(match.group("token"))
        if entry is None:
            return match.group(0)
        return entry.original

    return _OWN_TOKEN_RE.sub(_sub, text)
