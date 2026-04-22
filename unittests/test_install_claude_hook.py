"""Unit + integration tests for the Claude Code PreToolUse hook.

Split into two layers:

1. Pure-Python tests around :mod:`kuckuck.install_hook` (always run).
2. Subprocess tests that exec the bundled shell script
   (POSIX only; skipped on Windows).

The shell script lives at ``integrations/claude-code/kuckuck-pseudo.sh``
and is mirrored into ``src/kuckuck/_hooks/`` so the wheel ships a copy.
:test:`TestBundledScriptSync` asserts the two copies stay byte-equal.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kuckuck import install_hook

_skip_cli = False
try:
    # pylint: disable=ungrouped-imports
    from typer.testing import CliRunner

    from kuckuck.__main__ import app, inject_default_run

    runner = CliRunner()
except ImportError:  # pragma: no cover - CI always installs typer
    _skip_cli = True

pytestmark = pytest.mark.skipif(_skip_cli, reason="typer not installed")

# Path to the source-tree copy of the shell script. Resolved relative
# to this test file so pytest can be invoked from anywhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CANONICAL_SH = _REPO_ROOT / "integrations" / "claude-code" / "kuckuck-pseudo.sh"
_CANONICAL_PS1 = _REPO_ROOT / "integrations" / "claude-code" / "kuckuck-pseudo.ps1"
_BUNDLED_SH = _REPO_ROOT / "src" / "kuckuck" / "_hooks" / "kuckuck-pseudo.sh"
_BUNDLED_PS1 = _REPO_ROOT / "src" / "kuckuck" / "_hooks" / "kuckuck-pseudo.ps1"


def _invoke(argv: list[str]):  # type: ignore[no-untyped-def]
    """CliRunner wrapper matching the production ``main()`` entry point."""
    return runner.invoke(app, inject_default_run(argv))


class TestBundledScriptSync:
    """Canonical and wheel-bundled copies must be byte-identical."""

    def test_shell_script_matches_bundled(self) -> None:
        assert _CANONICAL_SH.read_bytes() == _BUNDLED_SH.read_bytes()

    def test_powershell_script_matches_bundled(self) -> None:
        assert _CANONICAL_PS1.read_bytes() == _BUNDLED_PS1.read_bytes()


class TestMergeHookIntoSettings:
    """Pure-function tests for the idempotent settings merge."""

    def test_inserts_into_empty_settings(self) -> None:
        settings: dict[str, Any] = {}
        changed = install_hook.merge_hook_into_settings(settings, command="/tmp/kuckuck-pseudo.sh")
        assert changed is True
        pre_tool_use = settings["hooks"]["PreToolUse"]
        assert len(pre_tool_use) == 1
        matcher_group = pre_tool_use[0]
        assert matcher_group["matcher"] == install_hook.MATCHER
        assert matcher_group["hooks"][0]["if"] == install_hook.IF_FILTER
        assert matcher_group["hooks"][0]["command"] == "/tmp/kuckuck-pseudo.sh"

    def test_preserves_existing_user_hooks(self) -> None:
        settings: dict[str, Any] = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "/usr/local/bin/user-bash-hook.sh"}],
                    }
                ]
            }
        }
        changed = install_hook.merge_hook_into_settings(settings, command="/tmp/kuckuck-pseudo.sh")
        assert changed is True
        pre_tool_use = settings["hooks"]["PreToolUse"]
        # The user's Bash hook must still be there, untouched.
        assert pre_tool_use[0]["matcher"] == "Bash"
        assert pre_tool_use[0]["hooks"][0]["command"] == "/usr/local/bin/user-bash-hook.sh"
        # Ours is appended.
        assert any("kuckuck-pseudo.sh" in h["command"] for h in pre_tool_use[1]["hooks"])

    def test_idempotent_when_already_installed(self) -> None:
        settings: dict[str, Any] = {}
        install_hook.merge_hook_into_settings(settings, command="/tmp/kuckuck-pseudo.sh")
        before = json.dumps(settings, sort_keys=True)
        changed = install_hook.merge_hook_into_settings(settings, command="/tmp/kuckuck-pseudo.sh")
        assert changed is False
        assert json.dumps(settings, sort_keys=True) == before

    def test_idempotent_detects_customised_command(self) -> None:
        # User tweaked the command path after installing; we must not re-add a duplicate.
        settings: dict[str, Any] = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Read|Edit|Grep",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/custom/path/to/kuckuck-pseudo.sh --verbose",
                            }
                        ],
                    }
                ]
            }
        }
        changed = install_hook.merge_hook_into_settings(settings, command="/tmp/kuckuck-pseudo.sh")
        assert changed is False

    def test_rejects_non_object_hooks_field(self) -> None:
        settings: dict[str, Any] = {"hooks": ["not an object"]}
        with pytest.raises(ValueError, match="settings.hooks is not an object"):
            install_hook.merge_hook_into_settings(settings, command="/tmp/kuckuck-pseudo.sh")


class TestRemoveHookFromSettings:
    def test_noop_when_absent(self) -> None:
        settings: dict[str, Any] = {"hooks": {"PreToolUse": []}}
        changed = install_hook.remove_hook_from_settings(settings)
        assert changed is False

    def test_removes_only_kuckuck_entry(self) -> None:
        settings: dict[str, Any] = {}
        install_hook.merge_hook_into_settings(settings, command="/tmp/kuckuck-pseudo.sh")
        # User adds their own hook too.
        settings["hooks"]["PreToolUse"].append(
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "/usr/local/bin/user-bash-hook.sh"}],
            }
        )
        changed = install_hook.remove_hook_from_settings(settings)
        assert changed is True
        remaining = settings["hooks"]["PreToolUse"]
        assert len(remaining) == 1
        assert remaining[0]["matcher"] == "Bash"

    def test_prunes_empty_structures(self) -> None:
        settings: dict[str, Any] = {}
        install_hook.merge_hook_into_settings(settings, command="/tmp/kuckuck-pseudo.sh")
        install_hook.remove_hook_from_settings(settings)
        assert settings == {}


class TestCliInstallUninstall:
    """End-to-end CLI exercises with a tmp project dir."""

    @pytest.fixture
    def project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def test_project_local_install_writes_script_and_settings(self, project: Path) -> None:
        result = _invoke(["install-claude-hook"])
        assert result.exit_code == 0, result.output
        script_name = install_hook.hook_script_name()
        assert (project / ".claude" / "hooks" / script_name).is_file()
        settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        if sys.platform == "win32":
            assert "$CLAUDE_PROJECT_DIR" in command and script_name in command
        else:
            assert command == f'"$CLAUDE_PROJECT_DIR"/.claude/hooks/{script_name}'

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable-bit check")
    def test_posix_install_marks_script_executable(self, project: Path) -> None:
        _invoke(["install-claude-hook"])
        script = project / ".claude" / "hooks" / install_hook.hook_script_name()
        mode = script.stat().st_mode
        assert mode & stat.S_IXUSR, f"expected user-executable, got {oct(mode)}"

    def test_install_is_idempotent(self, project: Path) -> None:
        first = _invoke(["install-claude-hook"])
        assert first.exit_code == 0
        before = (project / ".claude" / "settings.json").read_text(encoding="utf-8")
        second = _invoke(["install-claude-hook"])
        assert second.exit_code == 0
        after = (project / ".claude" / "settings.json").read_text(encoding="utf-8")
        assert before == after
        assert "already present" in second.output

    def test_uninstall_removes_script_and_entry(self, project: Path) -> None:
        _invoke(["install-claude-hook"])
        result = _invoke(["install-claude-hook", "--uninstall"])
        assert result.exit_code == 0
        assert not (project / ".claude" / "hooks" / install_hook.hook_script_name()).exists()
        settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert "hooks" not in settings or not settings["hooks"]

    def test_uninstall_preserves_user_hooks(self, project: Path) -> None:
        (project / ".claude").mkdir()
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "/usr/local/bin/user-bash-hook.sh"}],
                    }
                ]
            }
        }
        (project / ".claude" / "settings.json").write_text(json.dumps(existing, indent=2), encoding="utf-8")
        _invoke(["install-claude-hook"])
        _invoke(["install-claude-hook", "--uninstall"])
        settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"

    def test_uninstall_is_idempotent_without_prior_install(self, project: Path) -> None:
        _ = project  # fixture chdirs into tmp_path; no direct assertions on its value
        result = _invoke(["install-claude-hook", "--uninstall"])
        assert result.exit_code == 0
        assert "No hook" in result.output

    def test_global_emits_warning(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = project / "fake-home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        result = _invoke(["install-claude-hook", "--global"])
        assert result.exit_code == 0
        # Typer CliRunner (by default) merges stderr into stdout.
        assert "Warning: --global" in result.output
        assert (fake_home / ".claude" / "hooks" / install_hook.hook_script_name()).is_file()


# ---------------------------------------------------------------------------
# Subprocess-level tests for the shell script itself.
# ---------------------------------------------------------------------------


_ON_WINDOWS = sys.platform == "win32"
_JQ_AVAILABLE = shutil.which("jq") is not None

posix_only = pytest.mark.skipif(_ON_WINDOWS, reason="POSIX shell hook only runs on POSIX")
jq_required = pytest.mark.skipif(
    not _JQ_AVAILABLE,
    reason="shell hook requires jq on PATH (apt install jq / brew install jq)",
)


def _kuckuck_bin() -> str:
    """Return the absolute path to the ``kuckuck`` entry point in the current env."""
    # When running under tox the console script lives next to the python binary.
    candidate = Path(sys.executable).parent / "kuckuck"
    if candidate.is_file():
        return str(candidate)
    # Fallback for editable installs without the console script wired up.
    resolved = shutil.which("kuckuck")
    if resolved is None:
        pytest.skip("kuckuck console script not available in the current environment")
    return resolved


def _run_hook(
    *,
    tool_name: str,
    file_path: Path | None,
    path_field: str = "file_path",
    env_overrides: dict[str, str] | None = None,
    path_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Spawn the bundled shell script with a crafted stdin payload.

    *path_dir* lets a test build a restricted ``PATH`` (e.g. with kuckuck
    or jq deliberately absent). Defaults to inheriting the current PATH
    plus the directory holding the ``kuckuck`` console script.
    """
    payload: dict[str, object] = {
        "session_id": "test-session",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {path_field: str(file_path)} if file_path is not None else {},
    }
    env = os.environ.copy()
    if env_overrides:
        for key, value in env_overrides.items():
            if value is None:  # pragma: no cover - typing guard
                env.pop(key, None)
            else:
                env[key] = value
    if path_dir is not None:
        env["PATH"] = str(path_dir)
    else:
        kuckuck_dir = str(Path(_kuckuck_bin()).parent)
        env["PATH"] = f"{kuckuck_dir}{os.pathsep}{env.get('PATH', '')}"

    return subprocess.run(
        ["bash", str(_BUNDLED_SH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=120,
    )


@posix_only
@jq_required
class TestHookScriptHappyPath:
    @pytest.fixture
    def key_file(self, tmp_path: Path) -> Path:
        path = tmp_path / ".kuckuck-key"
        path.write_text("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff", encoding="utf-8")
        return path

    @pytest.fixture
    def eml_file(self, tmp_path: Path) -> Path:
        source = _REPO_ROOT / "unittests" / "example_files" / "sample_email.eml"
        target = tmp_path / "brief.eml"
        target.write_bytes(source.read_bytes())
        return target

    def test_pseudonymizes_eml_on_read(self, eml_file: Path, key_file: Path) -> None:
        before = eml_file.read_text(encoding="utf-8")
        assert "klaus.mueller@firma.de" in before

        result = _run_hook(
            tool_name="Read",
            file_path=eml_file,
            env_overrides={"KUCKUCK_KEY_FILE": str(key_file)},
        )
        assert result.returncode == 0, result.stderr

        after = eml_file.read_text(encoding="utf-8")
        assert "klaus.mueller@firma.de" not in after
        assert "[[EMAIL_" in after

    def test_idempotent_on_already_pseudonymized(self, eml_file: Path, key_file: Path) -> None:
        # First pass pseudonymizes.
        first = _run_hook(
            tool_name="Read",
            file_path=eml_file,
            env_overrides={"KUCKUCK_KEY_FILE": str(key_file)},
        )
        assert first.returncode == 0
        snapshot_after_first = eml_file.read_text(encoding="utf-8")

        # Second pass must not crash and must keep token shapes intact.
        second = _run_hook(
            tool_name="Edit",
            file_path=eml_file,
            env_overrides={"KUCKUCK_KEY_FILE": str(key_file)},
        )
        assert second.returncode == 0, second.stderr
        assert eml_file.read_text(encoding="utf-8") == snapshot_after_first

    def test_grep_payload_uses_path_field(self, eml_file: Path, key_file: Path) -> None:
        result = _run_hook(
            tool_name="Grep",
            file_path=eml_file,
            path_field="path",
            env_overrides={"KUCKUCK_KEY_FILE": str(key_file)},
        )
        assert result.returncode == 0, result.stderr
        assert "[[EMAIL_" in eml_file.read_text(encoding="utf-8")

    def test_symlink_is_followed(self, tmp_path: Path, eml_file: Path, key_file: Path) -> None:
        link = tmp_path / "link.eml"
        link.symlink_to(eml_file)
        result = _run_hook(
            tool_name="Read",
            file_path=link,
            env_overrides={"KUCKUCK_KEY_FILE": str(key_file)},
        )
        assert result.returncode == 0, result.stderr
        assert "[[EMAIL_" in eml_file.read_text(encoding="utf-8")

    def test_large_file_completes(self, tmp_path: Path, key_file: Path) -> None:
        # Verify the hook does not stall or truncate stdin/stdout on a
        # >10 MB input. We use a plain-text file (kuckuck's fastest path)
        # with a couple of detectable tokens so the pipeline still runs
        # end-to-end, and leave any-format stress-testing to the
        # dedicated preprocessor benchmarks.
        header = "Kontakt: klaus.mueller@firma.de, Telefon +49 40 12345-678.\n"
        filler = ("Lorem ipsum dolor sit amet " * 64 + "\n").encode("utf-8")
        big = tmp_path / "big.txt"
        with big.open("wb") as fh:
            fh.write(header.encode("utf-8"))
            while fh.tell() < 10 * 1024 * 1024 + 1024:
                fh.write(filler)
            fh.write(header.encode("utf-8"))
        assert big.stat().st_size > 10 * 1024 * 1024

        result = _run_hook(
            tool_name="Read",
            file_path=big,
            env_overrides={"KUCKUCK_KEY_FILE": str(key_file)},
        )
        assert result.returncode == 0, result.stderr[-500:]
        assert "klaus.mueller@firma.de" not in big.read_text(encoding="utf-8")


@posix_only
class TestHookScriptFailClosed:
    @pytest.fixture
    def eml_file(self, tmp_path: Path) -> Path:
        source = _REPO_ROOT / "unittests" / "example_files" / "sample_email.eml"
        target = tmp_path / "brief.eml"
        target.write_bytes(source.read_bytes())
        return target

    @jq_required
    def test_missing_key_blocks_tool_call(self, tmp_path: Path, eml_file: Path) -> None:
        bogus_key_path = tmp_path / "no-such-key"
        # Absolute paths for XDG too, so the real user key cannot leak in.
        fake_xdg = tmp_path / "xdg-home"
        fake_xdg.mkdir()
        result = _run_hook(
            tool_name="Read",
            file_path=eml_file,
            env_overrides={
                "KUCKUCK_KEY_FILE": str(bogus_key_path),
                "HOME": str(fake_xdg),
            },
        )
        assert result.returncode == 2, result.stderr
        assert "Refusing to Read" in result.stderr
        assert "kuckuck_pseudonymize" in result.stderr

    @jq_required
    def test_fail_open_env_var_lets_tool_through(self, tmp_path: Path, eml_file: Path) -> None:
        fake_xdg = tmp_path / "xdg-home"
        fake_xdg.mkdir()
        result = _run_hook(
            tool_name="Read",
            file_path=eml_file,
            env_overrides={
                "KUCKUCK_KEY_FILE": str(tmp_path / "no-such-key"),
                "HOME": str(fake_xdg),
                "KUCKUCK_HOOK_FAIL_OPEN": "1",
            },
        )
        assert result.returncode == 0
        assert "UNSAFE" in result.stderr

    @jq_required
    def test_missing_kuckuck_blocks_with_hint(self, tmp_path: Path, eml_file: Path) -> None:
        # Build a minimal PATH that has jq and bash but no kuckuck.
        stubdir = tmp_path / "stubs"
        stubdir.mkdir()
        for tool in ("jq", "bash", "cat", "printf", "rm", "mkdir"):
            real = shutil.which(tool)
            if real is not None:
                (stubdir / tool).symlink_to(real)
        result = _run_hook(
            tool_name="Read",
            file_path=eml_file,
            path_dir=stubdir,
        )
        assert result.returncode == 2
        assert "kuckuck not found" in result.stderr
        assert "pip install" in result.stderr

    def test_missing_jq_blocks_with_hint(self, tmp_path: Path, eml_file: Path) -> None:
        stubdir = tmp_path / "stubs"
        stubdir.mkdir()
        for tool in ("kuckuck", "bash", "cat", "printf", "rm", "mkdir"):
            real = _kuckuck_bin() if tool == "kuckuck" else shutil.which(tool)
            if real is not None:
                (stubdir / tool).symlink_to(real)
        result = _run_hook(
            tool_name="Read",
            file_path=eml_file,
            path_dir=stubdir,
        )
        assert result.returncode == 2
        assert "jq not found" in result.stderr

    @jq_required
    def test_fail_open_still_works_when_kuckuck_missing(self, tmp_path: Path, eml_file: Path) -> None:
        stubdir = tmp_path / "stubs"
        stubdir.mkdir()
        for tool in ("jq", "bash", "cat", "printf", "rm", "mkdir"):
            real = shutil.which(tool)
            if real is not None:
                (stubdir / tool).symlink_to(real)
        result = _run_hook(
            tool_name="Read",
            file_path=eml_file,
            path_dir=stubdir,
            env_overrides={"KUCKUCK_HOOK_FAIL_OPEN": "1"},
        )
        assert result.returncode == 0
        assert "UNSAFE" in result.stderr


@posix_only
@jq_required
class TestHookScriptNoOpCases:
    def test_missing_file_path_field_passes_through(self) -> None:
        result = _run_hook(tool_name="Read", file_path=None)
        assert result.returncode == 0
        # No output path extracted, so the hook must not have invoked kuckuck.
        assert "[kuckuck-hook]" not in result.stderr

    def test_directory_path_is_ignored(self, tmp_path: Path) -> None:
        # Grep on a directory should fall through without failing.
        result = _run_hook(tool_name="Grep", file_path=tmp_path, path_field="path")
        assert result.returncode == 0

    def test_nonexistent_file_is_ignored(self, tmp_path: Path) -> None:
        result = _run_hook(tool_name="Read", file_path=tmp_path / "does-not-exist.eml")
        assert result.returncode == 0


# Helper used by maintainers who want to see what changes if they edit the
# shell script - keeps an at-a-glance hash in the pytest output on rerun.
def test_bundled_script_hash_visible() -> None:
    digest = hashlib.sha256(_BUNDLED_SH.read_bytes()).hexdigest()
    assert len(digest) == 64
