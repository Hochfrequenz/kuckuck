"""Install / uninstall helpers for the Claude Code PreToolUse hook.

The shell and PowerShell payloads live in :mod:`kuckuck._hooks` (bundled
into the wheel). This module handles (a) copying the right script into
``.claude/hooks/``, (b) merging an entry into ``.claude/settings.json``
without overwriting user-defined hooks, and (c) the reverse uninstall
path.

Pure functions here are exercised directly in the unit tests; the thin
typer wrapper lives in :mod:`kuckuck.__main__`.
"""

from __future__ import annotations

import json
import re
import stat
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

#: Matcher string used on the outer hook entry in ``settings.json``.
MATCHER = "Read|Edit|Grep"

#: Permission-rule filter that keeps the hook off non-PII tool calls.
IF_FILTER = "Read(*.eml) | Read(*.msg) | Edit(*.eml) | Edit(*.msg) | Grep(*.eml) | Grep(*.msg)"

#: Script filenames under ``src/kuckuck/_hooks/`` and their on-disk names.
POSIX_SCRIPT = "kuckuck-pseudo.sh"
WINDOWS_SCRIPT = "kuckuck-pseudo.ps1"

#: Regex used to spot an installed kuckuck hook entry in ``settings.json``.
#: Requires both a path separator before the filename and a shell-style
#: terminator after it (end-of-string, whitespace, or a closing quote).
#: That way commands like ``cp legacy.sh /tmp/kuckuck-pseudo.sh-backup``
#: or ``echo 'will install kuckuck-pseudo.sh later'`` do not look like
#: ours and survive ``--uninstall``.
_KUCKUCK_COMMAND_RE = re.compile(r"""[/\\]kuckuck-pseudo\.(?:sh|ps1)(?=$|["'\s])""")


@dataclass(frozen=True)
class InstallResult:
    """Return value of :func:`install` and :func:`uninstall`."""

    script_path: Path
    settings_path: Path
    script_changed: bool
    settings_changed: bool


def hook_script_name() -> str:
    """Return the bundled hook script filename for the current platform."""
    return WINDOWS_SCRIPT if sys.platform == "win32" else POSIX_SCRIPT


def _bundled_script_bytes(script_name: str) -> bytes:
    """Return the byte contents of a bundled hook script."""
    return resources.files("kuckuck._hooks").joinpath(script_name).read_bytes()


def _copy_script(target: Path, script_name: str) -> bool:
    """Copy bundled *script_name* to *target*. Return True iff the file changed."""
    bundled = _bundled_script_bytes(script_name)
    if target.is_file() and target.read_bytes() == bundled:
        # Still force the executable bit on POSIX in case the user touched it.
        if sys.platform != "win32":
            _ensure_executable(target)
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bundled)
    if sys.platform != "win32":
        _ensure_executable(target)
    return True


def _ensure_executable(target: Path) -> None:
    """Set the user/group/other execute bit on *target* (POSIX only)."""
    current = target.stat().st_mode
    target.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _is_kuckuck_entry(entry: Any) -> bool:
    """Return True if *entry* is an inner hook handler pointing at the kuckuck script.

    The detector requires a path-separator character immediately before
    the script filename. That covers every command we emit (project-local
    ``"$CLAUDE_PROJECT_DIR"/.claude/hooks/kuckuck-pseudo.sh`` and global
    absolute paths on POSIX or Windows) while rejecting unrelated user
    commands that merely happen to mention the filename in free text -
    e.g. ``echo 'will install kuckuck-pseudo.sh later'`` must survive a
    ``--uninstall`` run.
    """
    if not isinstance(entry, dict):
        return False
    command = entry.get("command")
    if not isinstance(command, str):
        return False
    return _KUCKUCK_COMMAND_RE.search(command) is not None


def _kuckuck_block(command: str) -> dict[str, Any]:
    """Return the full matcher group we append to ``PreToolUse`` on install."""
    return {
        "matcher": MATCHER,
        "hooks": [
            {
                "type": "command",
                "if": IF_FILTER,
                "command": command,
            }
        ],
    }


def command_string(script_path: Path, *, global_scope: bool) -> str:
    """Build the ``command`` string for ``settings.json``.

    For the project-local install we use the Claude-Code-provided
    ``$CLAUDE_PROJECT_DIR`` variable so the rendered ``settings.json``
    is commit-safe and works after clones into different directories.
    For ``--global`` we emit the absolute path since there is no
    project-relative anchor.

    On Windows we prefix with ``powershell -NoProfile
    -ExecutionPolicy Bypass -File`` so Claude Code's hook executor
    spawns ``powershell.exe`` regardless of user PATH associations.
    Forward slashes are used throughout because Claude Code runs hook
    commands through ``bash`` by default on every platform, and bash
    (including Git Bash on native Windows) eats backslashes inside
    double-quoted strings. PowerShell itself accepts forward slashes
    in paths, so the ``-File`` argument parses correctly either way.
    """
    if sys.platform == "win32":
        if global_scope:
            absolute = script_path.as_posix()
            return f'powershell -NoProfile -ExecutionPolicy Bypass -File "{absolute}"'
        inner = '"$CLAUDE_PROJECT_DIR"/.claude/hooks/' + WINDOWS_SCRIPT
        return f"powershell -NoProfile -ExecutionPolicy Bypass -File {inner}"
    if global_scope:
        return f'"{script_path}"'
    return '"$CLAUDE_PROJECT_DIR"/.claude/hooks/' + POSIX_SCRIPT


def merge_hook_into_settings(settings: dict[str, Any], command: str) -> bool:
    """Ensure *settings* declares the kuckuck PreToolUse hook.

    Returns True iff *settings* was mutated. Idempotent: if any existing
    inner hook already references the kuckuck script, the user is
    presumed to have customised it and we leave the block untouched.
    The input dict is not mutated on the False path.
    """
    hooks = settings.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ValueError(f"settings.hooks is not an object: {type(hooks).__name__}")
    pre_tool_use: list[Any] | None = None
    if isinstance(hooks, dict):
        pre_tool_use = hooks.get("PreToolUse")
        if pre_tool_use is not None and not isinstance(pre_tool_use, list):
            raise ValueError(f"settings.hooks.PreToolUse is not a list: {type(pre_tool_use).__name__}")
        for matcher_group in pre_tool_use or []:
            if not isinstance(matcher_group, dict):
                continue
            inner_hooks = matcher_group.get("hooks") or []
            if not isinstance(inner_hooks, list):
                continue
            if any(_is_kuckuck_entry(h) for h in inner_hooks):
                return False

    # Only now that we know we are going to append, create the scaffolding.
    hooks = settings.setdefault("hooks", {})
    pre_tool_use = hooks.setdefault("PreToolUse", [])
    pre_tool_use.append(_kuckuck_block(command))
    return True


def remove_hook_from_settings(settings: dict[str, Any]) -> bool:
    """Strip every kuckuck PreToolUse entry from *settings*.

    Returns True iff *settings* was mutated. Prunes empty matcher groups,
    empty ``PreToolUse`` lists, and an empty ``hooks`` object so the
    output looks like the user never installed us.
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    pre_tool_use = hooks.get("PreToolUse")
    if not isinstance(pre_tool_use, list):
        return False

    changed = False
    kept_groups: list[Any] = []
    for matcher_group in pre_tool_use:
        if not isinstance(matcher_group, dict):
            kept_groups.append(matcher_group)
            continue
        inner_hooks = matcher_group.get("hooks")
        if not isinstance(inner_hooks, list):
            kept_groups.append(matcher_group)
            continue
        kept_inner = [h for h in inner_hooks if not _is_kuckuck_entry(h)]
        if len(kept_inner) != len(inner_hooks):
            changed = True
        if not kept_inner:
            # Drop the matcher group entirely when it held only our hook.
            continue
        matcher_group["hooks"] = kept_inner
        kept_groups.append(matcher_group)

    if not changed:
        return False
    if kept_groups:
        hooks["PreToolUse"] = kept_groups
    else:
        hooks.pop("PreToolUse", None)
    if not hooks:
        settings.pop("hooks", None)
    return True


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level JSON must be an object, got {type(data).__name__}")
    return data


def _write_settings(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.write_text(payload, encoding="utf-8")


def install(claude_dir: Path, *, global_scope: bool) -> InstallResult:
    """Install hook script and settings entry under *claude_dir* (e.g. ``~/.claude``)."""
    script_name = hook_script_name()
    script_path = claude_dir / "hooks" / script_name
    settings_path = claude_dir / "settings.json"

    script_changed = _copy_script(script_path, script_name)

    existing = _load_settings(settings_path)
    command = command_string(script_path, global_scope=global_scope)
    settings_changed = merge_hook_into_settings(existing, command)
    if settings_changed:
        _write_settings(settings_path, existing)

    return InstallResult(
        script_path=script_path,
        settings_path=settings_path,
        script_changed=script_changed,
        settings_changed=settings_changed,
    )


def uninstall(claude_dir: Path) -> InstallResult:
    """Remove the hook script and the matching settings entry from *claude_dir*.

    Missing files are treated as "nothing to do" rather than errors so a
    repeated ``--uninstall`` run is idempotent.
    """
    script_name = hook_script_name()
    script_path = claude_dir / "hooks" / script_name
    settings_path = claude_dir / "settings.json"

    script_changed = False
    if script_path.is_file():
        script_path.unlink()
        script_changed = True

    settings_changed = False
    if settings_path.is_file():
        existing = _load_settings(settings_path)
        settings_changed = remove_hook_from_settings(existing)
        if settings_changed:
            _write_settings(settings_path, existing)

    return InstallResult(
        script_path=script_path,
        settings_path=settings_path,
        script_changed=script_changed,
        settings_changed=settings_changed,
    )
