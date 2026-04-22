"""End-to-end smoke test for a built Kuckuck binary.

Invoked by the :file:`.github/workflows/build_executable.yml` pipeline after
the PyInstaller stage. Takes the binary path as the sole argument and runs
five checks:

1. ``--version`` exits 0 and prints a non-empty string.
2. ``init-key``, ``run`` and ``restore`` round-trip a document end-to-end.
3. An unpseudonymized input round-trips back to the original after restore.
4. ``install-claude-hook`` writes the bundled hook script and a valid
   ``settings.json`` entry. This guards against PyInstaller regressions
   that would strip ``kuckuck/_hooks/*`` from the bundle (``--collect-data
   kuckuck`` needs the package to be installed, not just on PYTHONPATH).
5. ``kuckuck mcp --help`` works and mentions ``serve``. This guards
   against the MCP subpackage being dropped from the single-binary
   release - the fat ``kuckuck_<os>`` build lives or dies by its
   MCP-server mode working.

Exits non-zero on any failure so CI surfaces the binary as broken. The
script is intentionally dependency-free - it must run against the cold
checkout on a GitHub Actions runner without the project's dev env.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(
    binary: str,
    args: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        print(f"command failed: {binary} {' '.join(args)}", file=sys.stderr)
        print("stdout:", result.stdout, file=sys.stderr)
        print("stderr:", result.stderr, file=sys.stderr)
    return result


def main(binary: str) -> int:  # pylint: disable=too-many-return-statements
    binary = os.path.abspath(binary)
    if not Path(binary).is_file():
        print(f"binary not found: {binary}", file=sys.stderr)
        return 1

    version = _run(binary, ["version"])
    if version.returncode != 0 or not version.stdout.strip():
        print("version check failed", file=sys.stderr)
        return 1
    print(f"version: {version.stdout.strip()}")

    with tempfile.TemporaryDirectory() as workspace:
        key = Path(workspace) / "key"
        init = _run(binary, ["init-key", "--key-file", str(key)])
        if init.returncode != 0 or not key.is_file():
            print("init-key failed", file=sys.stderr)
            return 1

        doc = Path(workspace) / "doc.txt"
        original = "Kontakt: max.mueller@firma.de, cc @eva.schmidt"
        doc.write_text(original, encoding="utf-8")

        run = _run(binary, [str(doc), "--key-file", str(key)])
        if run.returncode != 0:
            print("run failed", file=sys.stderr)
            return 1

        pseudonymized = doc.read_text(encoding="utf-8")
        if "max.mueller@firma.de" in pseudonymized:
            print("original email still present in pseudonymized output", file=sys.stderr)
            return 1
        if "[[EMAIL_" not in pseudonymized:
            print("missing EMAIL token in pseudonymized output", file=sys.stderr)
            return 1
        if "[[HANDLE_" not in pseudonymized:
            print("missing HANDLE token in pseudonymized output", file=sys.stderr)
            return 1

        restore = _run(binary, ["restore", str(doc), "--key-file", str(key)])
        if restore.returncode != 0:
            print("restore failed", file=sys.stderr)
            return 1
        if doc.read_text(encoding="utf-8") != original:
            print("restore did not reproduce original", file=sys.stderr)
            return 1

        if (rc := _check_install_claude_hook(binary, Path(workspace) / "hook-check")) != 0:
            return rc

        if (rc := _check_mcp_subcommand(binary)) != 0:
            return rc

    print("smoke test passed")
    return 0


def _check_mcp_subcommand(binary: str) -> int:
    """Assert that 'kuckuck mcp' is registered AND the server can actually boot.

    - ``kuckuck mcp --help`` lists ``serve`` (Typer registration intact).
    - ``kuckuck mcp serve`` started with empty stdin runs the deferred-import
      codepath, reaches the FastMCP stdio loop, fails fast on the unparseable
      JSON-RPC payload, and exits. The stderr must not contain
      ``ModuleNotFoundError`` / ``ImportError`` - those would indicate a
      PyInstaller bundling bug (e.g. missing --collect-all fastmcp).

    The ``--help`` path alone is NOT enough: Typer/Click resolve ``--help``
    before the command body executes, so the deferred import of
    ``kuckuck_mcp.server`` is never triggered and a broken bundle would pass
    silently. The boot-with-EOF check forces the real codepath.
    """
    help_result = _run(binary, ["mcp", "--help"])
    if help_result.returncode != 0:
        print("kuckuck mcp --help failed", file=sys.stderr)
        return 1
    if "serve" not in help_result.stdout:
        print("kuckuck mcp --help did not list 'serve' subcommand", file=sys.stderr)
        return 1

    # Timeout sized for the worst-case startup path: a ~300 MB PyInstaller
    # onefile binary on Windows NTFS unpacks its _MEI directory on every
    # invocation (typically 30-60 s cold), and only then does our deferred
    # import of fastmcp / pydantic / torch run. 120 s leaves plenty of
    # slack without giving up the "catch an actual hang" property.
    try:
        boot = subprocess.run(
            [binary, "mcp", "serve"],
            input="",
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        print("kuckuck mcp serve did not exit within 120s on empty stdin", file=sys.stderr)
        return 1
    combined_stderr = boot.stderr or ""
    for marker in ("ModuleNotFoundError", "ImportError: "):
        if marker in combined_stderr:
            print(f"kuckuck mcp serve stderr contains '{marker}':\n{combined_stderr}", file=sys.stderr)
            return 1
    return 0


def _check_install_claude_hook(binary: str, hook_workspace: Path) -> int:
    """Assert that install-claude-hook writes a usable script and settings entry.

    Guards against PyInstaller regressions that would strip the bundled
    hook scripts from the binary (``--collect-data kuckuck`` needs the
    package installed, not just on PYTHONPATH).
    """
    hook_workspace.mkdir()
    install = _run(binary, ["install-claude-hook"], cwd=str(hook_workspace))
    if install.returncode != 0:
        print("install-claude-hook failed", file=sys.stderr)
        return 1
    script_name = "kuckuck-pseudo.ps1" if sys.platform == "win32" else "kuckuck-pseudo.sh"
    hook_script = hook_workspace / ".claude" / "hooks" / script_name
    settings_path = hook_workspace / ".claude" / "settings.json"
    if not hook_script.is_file():
        print(f"hook script missing: {hook_script}", file=sys.stderr)
        return 1
    if hook_script.stat().st_size < 500:
        print(f"hook script suspiciously small: {hook_script.stat().st_size} bytes", file=sys.stderr)
        return 1
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"settings.json unreadable: {exc}", file=sys.stderr)
        return 1
    commands = [
        inner.get("command", "")
        for group in settings.get("hooks", {}).get("PreToolUse", [])
        for inner in group.get("hooks", [])
    ]
    if not any("kuckuck-pseudo" in cmd for cmd in commands):
        print("settings.json does not reference the kuckuck hook", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <binary>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
