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

from typing import Any, TypeVar

from pydantic import SecretStr

from kuckuck.detectors.base import Detector
from kuckuck.mapping import Mapping
from kuckuck.pseudonymize import pseudonymize_text, restore_text

# Both transforms are structure-preserving: a str maps to a str, a dict to a
# dict with the same keys, a list to a list, and any other scalar to itself.
# The public functions are therefore generic in the value type; the recursive
# work is delegated to Any-typed helpers (mypy cannot prove the per-branch
# reconstruction stays T, but the runtime contract holds).
T = TypeVar("T")


def pseudonymize_value(
    value: T,
    *,
    master: SecretStr,
    mapping: Mapping,
    detectors: list[Detector],
) -> T:
    """Return *value* with every PII string leaf replaced by a Kuckuck token.

    Structure-preserving: the result has the same type as *value* (a ``str``
    stays a ``str``, a ``dict`` keeps its keys, etc.). Recurses into dicts and
    lists. *mapping* is updated in place with every new allocation so callers
    can persist it after the walk. The same *mapping* shared across calls keeps
    token IDs stable (HMAC-deterministic).
    """
    return _pseudonymize_value(value, master=master, mapping=mapping, detectors=detectors)


def _pseudonymize_value(
    value: Any,
    *,
    master: SecretStr,
    mapping: Mapping,
    detectors: list[Detector],
) -> Any:
    if isinstance(value, str):
        return pseudonymize_text(value, master, detectors, mapping=mapping).text
    if isinstance(value, dict):
        return {
            key: _pseudonymize_value(item, master=master, mapping=mapping, detectors=detectors)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_pseudonymize_value(item, master=master, mapping=mapping, detectors=detectors) for item in value]
    # Numbers, bools, None and any other scalar carry no detectable PII.
    return value


def restore_value(value: T, mapping: Mapping) -> T:
    """Return *value* with every known Kuckuck token replaced by its original.

    Structure-preserving inverse of :func:`pseudonymize_value`: the result has
    the same type as *value*. Unknown tokens are left intact (see
    :func:`kuckuck.pseudonymize.restore_text`), so a stray token never raises -
    it just forwards literally.
    """
    return _restore_value(value, mapping)


def _restore_value(value: Any, mapping: Mapping) -> Any:
    if isinstance(value, str):
        return restore_text(value, mapping)
    if isinstance(value, dict):
        return {key: _restore_value(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_value(item, mapping) for item in value]
    return value
