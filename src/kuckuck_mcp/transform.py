"""Pseudonymize / restore Kuckuck tokens inside live MCP payloads.

The file-oriented pipeline in :mod:`kuckuck.pseudonymize` works on whole
documents. The MCP proxy instead has to transform the structured values that
flow through a tool call: a :class:`~fastmcp.tools.tool.ToolResult` made of
content blocks plus an optional ``structured_content`` JSON object, and the
``arguments`` dict the model sends downstream.

This module walks those JSON-ish structures and applies the existing
text-level primitives to every string leaf:

* :func:`pseudonymize_value` replaces PII with tokens on the way *out* to the
  model (downstream response -> client).
* :func:`restore_value` expands tokens back to their originals on the way *in*
  to a trusted backend (client tool-call arguments -> downstream).

Both reuse the shared :class:`~kuckuck.mapping.Mapping`, so a token allocated
for one tool result resolves to the same value when the model later passes it
as an argument, and across the file-based CLI workflow too.

Only string leaves are touched; numbers, booleans and ``None`` pass through
unchanged. Dict keys are left as-is - they are structural field names, not
data, and rewriting them would break the schema the model relies on.
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from kuckuck.detectors.base import Detector
from kuckuck.mapping import Mapping
from kuckuck.pseudonymize import pseudonymize_text, restore_text


def pseudonymize_value(
    value: Any,
    *,
    master: SecretStr,
    mapping: Mapping,
    detectors: list[Detector],
) -> Any:
    """Return *value* with every PII string leaf replaced by a Kuckuck token.

    Recurses into dicts and lists. *mapping* is updated in place with every
    new allocation so callers can persist it after the walk. The same
    *mapping* shared across calls keeps token IDs stable (HMAC-deterministic).
    """
    if isinstance(value, str):
        return pseudonymize_text(value, master, detectors, mapping=mapping).text
    if isinstance(value, dict):
        return {
            key: pseudonymize_value(item, master=master, mapping=mapping, detectors=detectors)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [pseudonymize_value(item, master=master, mapping=mapping, detectors=detectors) for item in value]
    # Numbers, bools, None and any other scalar carry no detectable PII.
    return value


def restore_value(value: Any, mapping: Mapping) -> Any:
    """Return *value* with every known Kuckuck token replaced by its original.

    The inverse of :func:`pseudonymize_value`. Unknown tokens are left intact
    (see :func:`kuckuck.pseudonymize.restore_text`), so a stray token never
    raises - it just forwards literally.
    """
    if isinstance(value, str):
        return restore_text(value, mapping)
    if isinstance(value, dict):
        return {key: restore_value(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [restore_value(item, mapping) for item in value]
    return value
