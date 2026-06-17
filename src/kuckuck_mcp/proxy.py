"""Build a pseudonymizing FastMCP proxy around another MCP server.

:func:`build_proxy` wraps any backend understood by
:func:`fastmcp.server.create_proxy` (a stdio command via an MCPConfig dict, an
HTTP/SSE URL, a local server path, or - in tests - an in-process
:class:`~fastmcp.FastMCP` instance) and attaches a
:class:`~kuckuck_mcp.middleware.KuckuckMiddleware` so every payload that
crosses the proxy is pseudonymized (responses) and, for trusted backends,
restored (request arguments).

See https://gofastmcp.com/servers/proxy.md and the middleware docstring for
the data-flow contract.

v1 wraps a single backend with one ``trusted`` setting. Per-backend trust
across a multi-server MCPConfig is a documented follow-up (the middleware is
global to the proxy, so honouring per-server trust needs tool-name routing).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import FastMCPProxy
from pydantic import SecretStr

from kuckuck.detectors.base import Detector
from kuckuck.mapping import Mapping, load_mapping
from kuckuck.pseudonymize import build_default_detectors
from kuckuck_mcp.middleware import KuckuckMiddleware


def build_proxy(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    backend: Any,
    *,
    master: SecretStr,
    sidecar: Path | None = None,
    mapping: Mapping | None = None,
    detectors: list[Detector] | None = None,
    use_ner: bool = True,
    denylist: list[str] | None = None,
    trusted: bool = False,
    fail_open: bool = False,
    name: str = "kuckuck-proxy",
) -> FastMCPProxy:
    """Return a :class:`FastMCPProxy` over *backend* with Kuckuck pseudonymization.

    *master* is the Kuckuck master key. The token mapping is taken from
    *mapping* if given, else loaded from *sidecar* when that file exists, else
    started empty; new allocations are persisted back to *sidecar* when set.

    *detectors* overrides the detector set; by default the regex detectors plus
    GLiNER PERSON detection (when the model is available) are used - pass
    ``use_ner=False`` for a regex-only, low-latency proxy. *trusted* enables
    restoring tokens in outgoing tool arguments (cleartext leaves to the
    backend), and *fail_open* downgrades a pseudonymization failure from a
    blocked call to a logged warning (UNSAFE).
    """
    if mapping is None:
        mapping = load_mapping(master, sidecar) if sidecar is not None and sidecar.exists() else Mapping()
    if detectors is None:
        detectors = build_default_detectors(denylist=denylist, use_ner=use_ner)

    proxy: FastMCPProxy = create_proxy(backend, name=name)
    proxy.add_middleware(
        KuckuckMiddleware(
            master=master,
            mapping=mapping,
            detectors=detectors,
            sidecar=sidecar,
            trusted=trusted,
            fail_open=fail_open,
        )
    )
    return proxy
