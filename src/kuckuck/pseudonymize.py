"""High-level pseudonymize / restore pipeline."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from kuckuck.detectors.base import Detector, EntityType, Span
from kuckuck.detectors.denylist import DenylistDetector
from kuckuck.detectors.email import EmailDetector
from kuckuck.detectors.handle import HandleDetector
from kuckuck.detectors.ner import (
    NerDetector,
    NerModelMissingError,
    NerNotInstalledError,
    is_gliner_installed,
    is_model_available,
)
from kuckuck.detectors.phone import PhoneDetector
from kuckuck.detectors.resolver import resolve_spans
from kuckuck.mapping import Mapping, MappingEntry
from kuckuck.preprocessors.base import Preprocessor

#: The wire format for a single pseudonym occurrence in the output text.
TOKEN_TEMPLATE = "[[{entity}_{token}]]"

#: Matches tokens produced by :func:`pseudonymize_text` so we can skip them
#: on subsequent passes (idempotency). Accepts the optional ``-N`` collision
#: counter as well as pure-numeric tokens from sequential mode.
_OWN_TOKEN_RE = re.compile(r"\[\[(?P<entity>[A-Z]+)_(?P<token>[a-z0-9]+(?:-\d+)?)\]\]")


logger = logging.getLogger(__name__)


def build_default_detectors(
    *,
    denylist: list[str] | None = None,
    phone_region: str = "DE",
    use_ner: bool = False,
) -> list[Detector]:
    """Return the built-in detector set for the regex pipeline.

    With *use_ner* set the GLiNER-backed :class:`NerDetector` is appended
    when both the optional ``gliner`` package and the on-disk model are
    available. If either is missing this function logs a warning and
    skips the NER detector — it never raises. CLI callers that want a
    hard failure check :func:`is_gliner_installed` and
    :func:`is_model_available` themselves before invoking this function.
    """
    detectors: list[Detector] = [
        EmailDetector(),
        PhoneDetector(default_region=phone_region),
        HandleDetector(),
    ]
    if denylist:
        detectors.append(DenylistDetector(denylist))
    if use_ner:
        if not is_gliner_installed():
            logger.warning(
                "NER requested but the optional 'gliner' package is not installed; "
                "skipping. Install it via: pip install 'kuckuck[ner]'"
            )
        elif not is_model_available():
            logger.warning(
                "NER requested but the GLiNER model is not present locally; "
                "skipping. Run 'kuckuck fetch-model' to download it."
            )
        else:
            try:
                detectors.append(NerDetector())
            except (NerModelMissingError, NerNotInstalledError) as exc:
                logger.warning("NER detector unavailable: %s", exc)
    return detectors


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


def pseudonymize_text(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
    text: str,
    master: SecretStr,
    detectors: list[Detector] | None = None,
    *,
    mapping: Mapping | None = None,
    sequential_tokens: bool = False,
    preprocessor: Preprocessor | None = None,
) -> PseudonymizeResult:
    """Pseudonymize *text* in-memory and return the result.

    *mapping* can be passed to merge with an existing mapping (cross-document
    consistency). When *sequential_tokens* is ``True``, tokens are assigned as
    sequential per-type counters within the current document instead of HMAC
    fingerprints — this loses cross-doc stability but keeps the output short.

    When *preprocessor* is provided, *text* is split into format-aware
    chunks (e.g. mail body excluding headers, Markdown excluding code
    fences) before pseudonymization, then reassembled. The shared
    *mapping* keeps token IDs consistent across chunks.
    """
    if detectors is None:
        detectors = build_default_detectors()
    if mapping is None:
        mapping = Mapping()

    if preprocessor is not None:
        return _pseudonymize_with_preprocessor(
            text,
            master,
            detectors,
            mapping=mapping,
            sequential_tokens=sequential_tokens,
            preprocessor=preprocessor,
        )

    return _pseudonymize_chunk(
        text,
        master,
        detectors,
        mapping=mapping,
        sequential_tokens=sequential_tokens,
    )


def _pseudonymize_chunk(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
    text: str,
    master: SecretStr,
    detectors: list[Detector],
    *,
    mapping: Mapping,
    sequential_tokens: bool,
    sequential_counters: dict[EntityType, int] | None = None,
) -> PseudonymizeResult:
    """Run the detector pipeline on a single text region.

    Splitting the per-chunk loop into its own helper lets the
    preprocessor path share the same span-resolution logic without
    re-implementing the cursor walk. *sequential_counters* is threaded
    through so cross-chunk allocations under ``--sequential-tokens``
    keep counting up instead of restarting per chunk.
    """
    own_spans = _find_own_tokens(text)
    raw_spans: list[Span] = []
    for det in detectors:
        raw_spans.extend(det.detect(text))
    filtered = [s for s in raw_spans if not any(s.overlaps(own) for own in own_spans)]
    resolved = resolve_spans(filtered)

    if sequential_counters is None and sequential_tokens:
        sequential_counters = {}
    counters = sequential_counters

    replaced_ordered: list[Span] = []
    output_chunks: list[str] = []
    cursor = 0
    for span in resolved:
        output_chunks.append(text[cursor : span.start])
        token = _allocate_token(
            master=master,
            mapping=mapping,
            span=span,
            sequential_counters=counters,
        )
        output_chunks.append(TOKEN_TEMPLATE.format(entity=span.entity_type.value, token=token))
        replaced_ordered.append(span)
        cursor = span.end
    output_chunks.append(text[cursor:])
    return PseudonymizeResult(text="".join(output_chunks), mapping=mapping, replaced=replaced_ordered)


def _pseudonymize_with_preprocessor(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    text: str,
    master: SecretStr,
    detectors: list[Detector],
    *,
    mapping: Mapping,
    sequential_tokens: bool,
    preprocessor: Preprocessor,
) -> PseudonymizeResult:
    """Drive the pipeline through *preprocessor*'s extract/reassemble cycle."""
    chunks = preprocessor.extract(text)
    sequential_counters: dict[EntityType, int] | None = {} if sequential_tokens else None
    all_replaced: list[Span] = []
    for chunk in chunks:
        result = _pseudonymize_chunk(
            chunk.text,
            master,
            detectors,
            mapping=mapping,
            sequential_tokens=sequential_tokens,
            sequential_counters=sequential_counters,
        )
        chunk.text = result.text
        all_replaced.extend(result.replaced)
    rebuilt = preprocessor.reassemble(text, chunks)
    return PseudonymizeResult(text=rebuilt, mapping=mapping, replaced=all_replaced)


def pseudonymize_msg_file(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    path: Path,
    master: SecretStr,
    detectors: list[Detector] | None = None,
    *,
    mapping: Mapping | None = None,
    sequential_tokens: bool = False,
) -> PseudonymizeResult:
    """Pseudonymize an Outlook ``.msg`` compound document at *path*.

    .msg files are OLE binaries; they cannot be decoded as UTF-8 text.
    This wrapper hands the path directly to :class:`MsgPreprocessor`,
    pseudonymizes each extracted body chunk through the shared
    detector / mapping plumbing, and returns the rebuilt plain-text body.

    Output is intentionally text-only: round-tripping the .msg compound
    structure is out of scope. Callers wanting to preserve the original
    binary file should keep a copy before invoking this helper.
    """
    # Imported here so the optional preprocessors module isn't loaded
    # at import time of pseudonymize (keeps the cold-start cheap).
    from kuckuck.preprocessors.msg import MsgPreprocessor  # pylint: disable=import-outside-toplevel

    if detectors is None:
        detectors = build_default_detectors()
    if mapping is None:
        mapping = Mapping()

    preprocessor = MsgPreprocessor()
    chunks = preprocessor.extract(path)
    sequential_counters: dict[EntityType, int] | None = {} if sequential_tokens else None
    all_replaced: list[Span] = []
    for chunk in chunks:
        result = _pseudonymize_chunk(
            chunk.text,
            master,
            detectors,
            mapping=mapping,
            sequential_tokens=sequential_tokens,
            sequential_counters=sequential_counters,
        )
        chunk.text = result.text
        all_replaced.extend(result.replaced)
    rebuilt = preprocessor.reassemble(path, chunks)
    return PseudonymizeResult(text=rebuilt, mapping=mapping, replaced=all_replaced)


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
