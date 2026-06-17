"""FastMCP middleware that pseudonymizes PII flowing through a proxied server.

``KuckuckMiddleware`` is attached to a :func:`fastmcp.FastMCP.as_proxy`
instance (see :mod:`kuckuck_mcp.proxy`). It sits on the bidirectional pipeline
described at https://gofastmcp.com/servers/middleware.md and rewrites payloads
in both directions:

* **Response direction** (downstream -> model): every tool result is walked and
  PII string leaves are replaced with Kuckuck tokens *before* the result
  reaches the client. This is the core promise - cleartext customer data never
  enters the model context.
* **Request direction** (model -> downstream): when the backend is marked
  ``trusted``, tokens the model puts into tool-call arguments are expanded back
  to their originals before being forwarded. This lets the model act on real
  entities (write a Jira comment, update a record) without ever having seen the
  underlying PII. Untrusted backends never receive restored cleartext.

Failure handling is **fail-closed** by default: if pseudonymizing a result
raises, the tool call is blocked with a :class:`~fastmcp.exceptions.ToolError`
rather than leaking the raw result. Setting ``KUCKUCK_PROXY_FAIL_OPEN=1`` (or
constructing with ``fail_open=True``) downgrades this to a logged warning that
forwards the raw result - documented as UNSAFE, mirroring the Claude Code
hook's ``KUCKUCK_HOOK_FAIL_OPEN`` escape hatch.

Known boundaries (v1): only ``TextContent`` blocks and ``structured_content``
are pseudonymized. Image / audio / embedded-resource blocks pass through
untouched, as do tool *descriptions* surfaced by ``list_tools`` (metadata, not
data).
"""

from __future__ import annotations

import logging
from pathlib import Path

import anyio
import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from pydantic import SecretStr

from kuckuck.detectors.base import Detector
from kuckuck.mapping import Mapping, save_mapping
from kuckuck_mcp.transform import pseudonymize_value, restore_value

logger = logging.getLogger(__name__)

_FAIL_CLOSED_MESSAGE = (
    "kuckuck: failed to pseudonymize the downstream tool result; blocking the "
    "call so no un-pseudonymized PII reaches the model. Set "
    "KUCKUCK_PROXY_FAIL_OPEN=1 to forward the raw result instead (UNSAFE)."
)


class KuckuckMiddleware(Middleware):
    """Pseudonymize tool results and restore tokens in trusted tool arguments.

    *master* and *mapping* are shared mutable state: every newly detected PII
    value is allocated a token in *mapping*, which is then persisted to
    *sidecar* (when given) so token IDs survive restarts and interoperate with
    the file-based CLI. A lock serializes all mapping access because tool calls
    can arrive concurrently.
    """

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        *,
        master: SecretStr,
        mapping: Mapping,
        detectors: list[Detector],
        sidecar: Path | None = None,
        trusted: bool = False,
        fail_open: bool = False,
    ) -> None:
        self._master = master
        self._mapping = mapping
        self._detectors = detectors
        self._sidecar = sidecar
        self._trusted = trusted
        self._fail_open = fail_open
        self._lock = anyio.Lock()

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        # Request direction: expand tokens the model supplied back to real PII,
        # but only for a backend the operator explicitly trusts with cleartext.
        if self._trusted and context.message.arguments:
            async with self._lock:
                context.message.arguments = restore_value(context.message.arguments, self._mapping)

        result: ToolResult = await call_next(context)

        # Response direction: pseudonymize before the result reaches the model.
        async with self._lock:
            try:
                before = len(self._mapping)
                await anyio.to_thread.run_sync(self._pseudonymize_result, result)
                if self._sidecar is not None and len(self._mapping) != before:
                    save_mapping(self._master, self._mapping, self._sidecar)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                if self._fail_open:
                    logger.warning("kuckuck proxy: pseudonymization failed, forwarding raw result (FAIL-OPEN): %s", exc)
                    return result
                raise ToolError(_FAIL_CLOSED_MESSAGE) from exc
        return result

    def _pseudonymize_result(self, result: ToolResult) -> None:
        """Rewrite PII in *result* in place (runs in a worker thread).

        Mutates ``TextContent`` blocks and ``structured_content``. Other block
        types are left untouched (documented boundary).
        """
        for block in result.content:
            if isinstance(block, mt.TextContent):
                block.text = pseudonymize_value(
                    block.text,
                    master=self._master,
                    mapping=self._mapping,
                    detectors=self._detectors,
                )
        if result.structured_content is not None:
            result.structured_content = pseudonymize_value(
                result.structured_content,
                master=self._master,
                mapping=self._mapping,
                detectors=self._detectors,
            )
