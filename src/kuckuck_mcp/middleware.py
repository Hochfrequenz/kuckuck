"""FastMCP middleware that pseudonymizes PII flowing through a proxied server.

``KuckuckMiddleware`` is attached to a :func:`fastmcp.server.create_proxy`
instance (see :mod:`kuckuck_mcp.proxy`). It sits on the bidirectional pipeline
described at https://gofastmcp.com/servers/middleware.md and rewrites payloads
in both directions:

* **Response direction** (downstream -> model): tool results *and* resource
  contents are walked and PII string leaves are replaced with Kuckuck tokens
  *before* they reach the client. This is the core promise - cleartext customer
  data never enters the model context. Both ``on_call_tool`` and
  ``on_read_resource`` are hooked because Jira / Confluence style backends
  expose their content as resources, not just tool results.
* **Request direction** (model -> downstream): when the backend is marked
  ``trusted``, tokens the model puts into tool-call arguments are expanded back
  to their originals before being forwarded. This lets the model act on real
  entities (write a Jira comment, update a record) without ever having seen the
  underlying PII. Untrusted backends never receive restored cleartext.

Failure handling is **fail-closed** by default: if pseudonymizing a payload
raises, the call is blocked (``ToolError`` / ``ResourceError``) rather than
leaking the raw payload. Setting ``KUCKUCK_PROXY_FAIL_OPEN=1`` (or constructing
with ``fail_open=True``) downgrades this to a logged warning that forwards the
raw payload - documented as UNSAFE, mirroring the Claude Code hook's
``KUCKUCK_HOOK_FAIL_OPEN`` escape hatch. The warning logs the exception only,
never the raw payload.

What is pseudonymized: ``TextContent`` blocks, ``structured_content``, the
``meta`` field, and text inside ``EmbeddedResource`` / resource ``contents``.

Documented non-goals (NOT pseudonymized): binary blocks (image / audio /
``BlobResourceContents``) carry no detectable text; tool / resource
*descriptions* surfaced by the ``list_*`` operations are metadata, not data;
and **prompts** (``on_get_prompt``) are out of scope by design - a prompt
template is author-controlled and not expected to carry customer PII.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import mcp.types as mt
from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.prompts.base import PromptResult
from fastmcp.resources.base import ResourceContent, ResourceResult
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from pydantic import SecretStr

from kuckuck.detectors.base import Detector
from kuckuck.mapping import Mapping, save_mapping
from kuckuck_mcp.transform import pseudonymize_value, restore_value

logger = logging.getLogger(__name__)

_FAIL_OPEN_ENV_VAR = "KUCKUCK_PROXY_FAIL_OPEN"

_FAIL_CLOSED_MESSAGE = (
    "kuckuck: failed to pseudonymize the downstream payload; blocking the call "
    "so no un-pseudonymized PII reaches the model. Set "
    "KUCKUCK_PROXY_FAIL_OPEN=1 to forward the raw payload instead (UNSAFE)."
)


class KuckuckMiddleware(Middleware):
    """Pseudonymize tool/resource payloads and restore tokens in trusted args.

    *master* and *mapping* are shared mutable state: every newly detected PII
    value is allocated a token in *mapping*, which is then persisted to
    *sidecar* (when given) so token IDs survive restarts and interoperate with
    the file-based CLI. A lock serializes all mapping access because calls can
    arrive concurrently.
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
        # The --fail-open flag and the KUCKUCK_PROXY_FAIL_OPEN env var both
        # enable the (UNSAFE) escape hatch; either one is enough.
        self._fail_open = fail_open or os.environ.get(_FAIL_OPEN_ENV_VAR) == "1"
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

        result = await call_next(context)
        await self._pseudonymize(result, self._rewrite_tool_result, ToolError)
        return result

    async def on_read_resource(
        self,
        context: MiddlewareContext[mt.ReadResourceRequestParams],
        call_next: CallNext[mt.ReadResourceRequestParams, ResourceResult],
    ) -> ResourceResult:
        # Resources are the primary content channel for Jira / Confluence style
        # backends, so they must be pseudonymized just like tool results.
        result = await call_next(context)
        await self._pseudonymize(result, self._rewrite_resource_result, ResourceError)
        return result

    async def on_get_prompt(
        self,
        context: MiddlewareContext[mt.GetPromptRequestParams],
        call_next: CallNext[mt.GetPromptRequestParams, PromptResult],
    ) -> PromptResult:
        # Documented non-goal: prompt templates are author-controlled and not
        # expected to carry customer PII, so they pass through unchanged.
        return await call_next(context)

    async def _pseudonymize(
        self,
        payload: object,
        rewrite: Callable[[Any], None],
        error_cls: type[Exception],
    ) -> None:
        """Run *rewrite* over *payload* under the lock, fail-closed by default.

        The CPU-bound rewrite (and any GLiNER inference) and the sidecar write
        run in worker threads so the event loop is never blocked. On failure
        the call is blocked with *error_cls* unless fail-open is enabled, in
        which case the raw payload is forwarded and only the exception (never
        the payload) is logged.
        """
        async with self._lock:
            try:
                before = len(self._mapping)
                await anyio.to_thread.run_sync(rewrite, payload)
                if self._sidecar is not None and len(self._mapping) != before:
                    await anyio.to_thread.run_sync(save_mapping, self._master, self._mapping, self._sidecar)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                if self._fail_open:
                    logger.warning(
                        "kuckuck proxy: pseudonymization failed, forwarding raw payload (FAIL-OPEN): %s", exc
                    )
                    return
                raise error_cls(_FAIL_CLOSED_MESSAGE) from exc

    def _pseudo(self, value: Any) -> Any:
        """Pseudonymize a JSON-ish value against the shared master/mapping."""
        return pseudonymize_value(value, master=self._master, mapping=self._mapping, detectors=self._detectors)

    def _rewrite_tool_result(self, result: ToolResult) -> None:
        """Rewrite PII in a tool result in place (runs in a worker thread)."""
        for block in result.content:
            self._rewrite_content_block(block)
        if result.structured_content is not None:
            result.structured_content = self._pseudo(result.structured_content)
        if result.meta is not None:
            result.meta = self._pseudo(result.meta)

    def _rewrite_resource_result(self, result: ResourceResult) -> None:
        """Rewrite PII in a resource-read result in place (worker thread).

        Proxied reads arrive as fastmcp ``ResourceContent`` (``.content`` is a
        ``str`` for text, ``bytes`` for blobs); raw ``mt.TextResourceContents``
        is handled too for non-proxy callers. Binary contents carry no text.
        """
        for item in result.contents:
            if isinstance(item, ResourceContent):
                if isinstance(item.content, str):
                    item.content = self._pseudo(item.content)
                if item.meta is not None:
                    item.meta = self._pseudo(item.meta)
            elif isinstance(item, mt.TextResourceContents):
                item.text = self._pseudo(item.text)
                if item.meta is not None:
                    item.meta = self._pseudo(item.meta)
        if result.meta is not None:
            result.meta = self._pseudo(result.meta)

    def _rewrite_content_block(self, block: object) -> None:
        """Rewrite PII in a single content block in place where it carries text."""
        if isinstance(block, mt.TextContent):
            block.text = self._pseudo(block.text)
        elif isinstance(block, mt.EmbeddedResource) and isinstance(block.resource, mt.TextResourceContents):
            block.resource.text = self._pseudo(block.resource.text)
        # ImageContent / AudioContent / ResourceLink / BlobResourceContents
        # carry no inline text to pseudonymize.
