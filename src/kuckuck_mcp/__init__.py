"""Model Context Protocol server for Kuckuck.

Exposes :class:`kuckuck.runner.run_pseudonymize`, :func:`kuckuck.restore_text`
and helper introspection over the MCP standard so any MCP-aware client
(Claude Desktop, Claude Code, Cursor, Cline, Zed, opencode, ...) can call
Kuckuck directly without a per-client adapter.

The server lives here as a sub-package so it imports the existing
:mod:`kuckuck.runner` API without version-coordination overhead.
Built on FastMCP >= 3 to stay consistent with the Hochfrequenz MCP stack.
"""

from kuckuck_mcp.server import build_server, main

__all__ = ["build_server", "main"]
