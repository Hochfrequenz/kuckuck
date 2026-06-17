"""Guard: the ``[mcp]`` extra must actually be installed when running in CI.

The MCP server and proxy test modules are skipped when ``fastmcp`` is not
importable, so the core install can stay MCP-free for local development. The
risk is that the extra silently stops installing on some CI matrix leg and all
MCP tests skip *without failing* - hiding regressions.

This guard runs only in CI (GitHub Actions sets ``CI=true``) and there it
*fails* rather than skips when ``fastmcp`` is missing, so a broken ``.[mcp]``
install turns CI red instead of green-with-silent-skips.
"""

from __future__ import annotations

import importlib.util
import os

import pytest


@pytest.mark.skipif(not os.environ.get("CI"), reason="only enforced in CI (GitHub Actions sets CI=true)")
def test_mcp_extra_is_installed_in_ci() -> None:
    assert importlib.util.find_spec("fastmcp") is not None, (
        "The [mcp] extra (fastmcp) is not installed in this CI run, so the MCP "
        "server/proxy tests would silently skip. Ensure 'tox -e tests' installs "
        ".[mcp] on every matrix leg."
    )
