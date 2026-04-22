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
from pathlib import Path, PurePosixPath, PureWindowsPath
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


class TestSettingsExampleSync:
    """The documented example must track the block that install-claude-hook emits."""

    @pytest.fixture
    def example(self) -> dict[str, Any]:
        raw = (_REPO_ROOT / "integrations" / "claude-code" / "settings.example.json").read_text(encoding="utf-8")
        parsed: dict[str, Any] = json.loads(raw)
        return parsed

    def test_example_matcher_matches_emitted_block(self, example: dict[str, Any]) -> None:
        pre_tool_use = example["hooks"]["PreToolUse"]
        assert len(pre_tool_use) == 1
        matcher_group = pre_tool_use[0]
        assert matcher_group["matcher"] == install_hook.MATCHER
        assert len(matcher_group["hooks"]) == 1
        inner = matcher_group["hooks"][0]
        assert inner["type"] == "command"
        assert inner["if"] == install_hook.IF_FILTER
        assert install_hook.POSIX_SCRIPT in inner["command"]
        assert "$CLAUDE_PROJECT_DIR" in inner["command"]


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

    def test_does_not_mutate_settings_when_returning_false(self) -> None:
        # Idempotent re-install: the hook is already present. The input
        # must not gain empty scaffolding as a side effect.
        seed = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Read|Edit|Grep",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/custom/path/to/kuckuck-pseudo.sh",
                            }
                        ],
                    }
                ]
            },
            "other_key": "preserved",
        }
        snapshot = json.dumps(seed, sort_keys=True)
        changed = install_hook.merge_hook_into_settings(seed, command="/tmp/kuckuck-pseudo.sh")
        assert changed is False
        assert json.dumps(seed, sort_keys=True) == snapshot

    def test_does_not_inject_empty_hooks_on_no_op(self) -> None:
        # A plain "already-installed" dict check ruled above; verify also
        # that a FRESH settings dict whose user-hooks-list-is-absent
        # stays empty when merge decides nothing is needed. This happens
        # when the dict has an unrelated kuckuck-like command inline.
        seed: dict[str, Any] = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/usr/local/bin/kuckuck-pseudo.sh",
                            }
                        ],
                    }
                ]
            }
        }
        snapshot = json.dumps(seed, sort_keys=True)
        changed = install_hook.merge_hook_into_settings(seed, command="/tmp/kuckuck-pseudo.sh")
        assert changed is False
        assert json.dumps(seed, sort_keys=True) == snapshot


class TestCommandStringRendering:
    """Lock in the exact ``command`` shape emitted for each (platform, scope) combination.

    The Windows cases are especially important to exercise on Linux CI:
    ``script_path.as_posix()`` is a no-op on Linux (POSIX paths already
    use ``/``) so a regression that reverts the call would silently pass
    without these monkeypatched cases. We parametrize on ``sys.platform``
    instead of skipping so a Linux-only CI matrix still covers both.
    """

    def test_posix_project_local_uses_claude_project_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Use PurePosixPath literals so the stringification does not
        # depend on the host platform. On Windows, a plain Path("/a/b")
        # stringifies with backslashes regardless of the monkeypatched
        # sys.platform, which would break this cross-platform test.
        monkeypatch.setattr("kuckuck.install_hook.sys.platform", "linux")
        rendered = install_hook.command_string(
            PurePosixPath("/irrelevant"),  # type: ignore[arg-type]
            global_scope=False,
        )
        assert rendered == '"$CLAUDE_PROJECT_DIR"/.claude/hooks/kuckuck-pseudo.sh'

    def test_posix_global_uses_absolute_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kuckuck.install_hook.sys.platform", "linux")
        rendered = install_hook.command_string(
            PurePosixPath("/home/u/.claude/hooks/kuckuck-pseudo.sh"),  # type: ignore[arg-type]
            global_scope=True,
        )
        assert rendered == '"/home/u/.claude/hooks/kuckuck-pseudo.sh"'

    def test_windows_project_local_uses_forward_slashes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kuckuck.install_hook.sys.platform", "win32")
        rendered = install_hook.command_string(
            PureWindowsPath("ignored"),  # type: ignore[arg-type]
            global_scope=False,
        )
        assert "\\" not in rendered, f"expected forward slashes only, got {rendered!r}"
        assert rendered == (
            "powershell -NoProfile -ExecutionPolicy Bypass -File "
            '"$CLAUDE_PROJECT_DIR"/.claude/hooks/kuckuck-pseudo.ps1'
        )

    def test_windows_global_converts_backslashes_to_forward_slashes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # PureWindowsPath.as_posix() converts backslashes to forward
        # slashes regardless of the host platform. Reverting the
        # .as_posix() call in install_hook would leak backslashes into
        # the rendered command and break bash, which is what this locks in.
        monkeypatch.setattr("kuckuck.install_hook.sys.platform", "win32")
        win_path = PureWindowsPath("C:\\Users\\u\\.claude\\hooks\\kuckuck-pseudo.ps1")
        rendered = install_hook.command_string(win_path, global_scope=True)  # type: ignore[arg-type]
        assert "\\" not in rendered, f"expected forward slashes only, got {rendered!r}"
        assert rendered == (
            "powershell -NoProfile -ExecutionPolicy Bypass -File " '"C:/Users/u/.claude/hooks/kuckuck-pseudo.ps1"'
        )


class TestKuckuckEntryDetection:
    """The uninstaller must distinguish our own hook from unrelated user hooks."""

    # Protected access is deliberate here: _is_kuckuck_entry is an internal
    # helper whose exact detection semantics matter enough to exercise
    # them directly instead of only through the merge/remove wrappers.
    # pylint: disable=protected-access

    @pytest.mark.parametrize(
        "command",
        [
            '"$CLAUDE_PROJECT_DIR"/.claude/hooks/kuckuck-pseudo.sh',
            "/home/u/.claude/hooks/kuckuck-pseudo.sh",
            'powershell -NoProfile -ExecutionPolicy Bypass -File "C:/Users/u/.claude/hooks/kuckuck-pseudo.ps1"',
            "/custom/path/to/kuckuck-pseudo.sh --verbose",
        ],
    )
    def test_real_kuckuck_commands_match(self, command: str) -> None:
        assert install_hook._is_kuckuck_entry({"command": command}) is True

    @pytest.mark.parametrize(
        "command",
        [
            "echo 'will install kuckuck-pseudo.sh later'",
            "cp legacy-hook.sh /tmp/kuckuck-pseudo.sh-backup",
            "echo kuckuck-pseudo.ps1 is a cool name",
        ],
    )
    def test_free_form_mentions_do_not_match(self, command: str) -> None:
        # Preceding separator missing or not a path char - must not match.
        assert install_hook._is_kuckuck_entry({"command": command}) is False


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

    def test_write_is_atomic_no_tmp_leftover_on_success(self, tmp_path: Path) -> None:
        target = tmp_path / "settings.json"
        install_hook._write_settings(target, {"hooks": {"PreToolUse": []}})  # pylint: disable=protected-access
        # No .tmp sibling must linger after a successful write.
        assert list(tmp_path.glob("settings.json*")) == [target]
        assert json.loads(target.read_text(encoding="utf-8")) == {"hooks": {"PreToolUse": []}}

    def test_write_leaves_original_intact_on_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Simulate a crash between open() and os.replace() by having
        # os.replace raise. The pre-existing file must be unchanged; the
        # .tmp sibling must be cleaned up.
        target = tmp_path / "settings.json"
        original = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"command": "x"}]}]}}
        target.write_text(json.dumps(original), encoding="utf-8")

        def fail_replace(src: str, dst: str) -> None:
            _ = src, dst
            raise OSError("simulated crash mid-rename")

        monkeypatch.setattr("kuckuck.install_hook.os.replace", fail_replace)
        with pytest.raises(OSError, match="simulated crash"):
            install_hook._write_settings(target, {"hooks": {"PreToolUse": []}})  # pylint: disable=protected-access

        # Original content preserved and no .tmp file left behind.
        assert json.loads(target.read_text(encoding="utf-8")) == original
        assert list(tmp_path.glob("settings.json.tmp")) == []

    def test_preserves_user_hook_that_merely_mentions_the_filename(self) -> None:
        # Regression for H4 review finding: a user hook whose command
        # happens to contain "kuckuck-pseudo.sh" in free text (without a
        # path separator in front) must survive --uninstall.
        seed = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "echo 'will install kuckuck-pseudo.sh later'",
                            }
                        ],
                    }
                ]
            }
        }
        snapshot = json.dumps(seed, sort_keys=True)
        changed = install_hook.remove_hook_from_settings(seed)
        assert changed is False
        assert json.dumps(seed, sort_keys=True) == snapshot


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

    def test_uninstall_global_round_trip(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The install / uninstall code branches on --global to choose
        # between ~/.claude and $PWD/.claude. We need both paths exercised
        # with a fake Path.home() so no real user state is touched.
        fake_home = project / "fake-home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        install_result = _invoke(["install-claude-hook", "--global"])
        assert install_result.exit_code == 0
        settings_path = fake_home / ".claude" / "settings.json"
        hook_path = fake_home / ".claude" / "hooks" / install_hook.hook_script_name()
        assert settings_path.is_file()
        assert hook_path.is_file()

        uninstall_result = _invoke(["install-claude-hook", "--uninstall", "--global"])
        assert uninstall_result.exit_code == 0
        assert not hook_path.exists(), "global hook script must be removed"
        # Settings file may still exist but must be empty of our entry.
        if settings_path.is_file():
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            assert "hooks" not in settings or not settings["hooks"]


# ---------------------------------------------------------------------------
# Subprocess-level tests for the shell script itself.
# ---------------------------------------------------------------------------


_ON_WINDOWS = sys.platform == "win32"
_JQ_AVAILABLE = shutil.which("jq") is not None
_PWSH_AVAILABLE = shutil.which("pwsh") is not None

posix_only = pytest.mark.skipif(_ON_WINDOWS, reason="POSIX shell hook only runs on POSIX")
jq_required = pytest.mark.skipif(
    not _JQ_AVAILABLE,
    reason="shell hook requires jq on PATH (apt install jq / brew install jq)",
)
pwsh_required = pytest.mark.skipif(
    not _PWSH_AVAILABLE,
    reason="PowerShell script tests need pwsh on PATH (install: https://aka.ms/install-powershell)",
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
        # Regression for H3 (rc=$? after `fi` captured 0 instead of the
        # real exit code): the block message must surface kuckuck's
        # actual key-not-found exit status (3), otherwise the
        # troubleshooting anchor "(kuckuck exit 3)" is unreachable.
        assert "exit 3" in result.stderr

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
    def test_garbled_stdin_json_blocks(self, tmp_path: Path, eml_file: Path) -> None:
        # Regression for H2 review finding: the shell script used to
        # swallow jq parse failures via $(...) command substitution,
        # leaving TOOL/FILE empty and silently exit-0ing. The result was
        # a fail-open on malformed payloads. We build a PATH that has
        # the real tools and send garbage on stdin.
        _ = eml_file  # fixture only used to keep setup shape consistent
        env = os.environ.copy()
        env["PATH"] = f"{Path(_kuckuck_bin()).parent}{os.pathsep}{env.get('PATH', '')}"
        # Route kuckuck past the key-found check even if the global key
        # disappears: point HOME and KUCKUCK_KEY_FILE somewhere bogus so
        # the fail-closed path is the jq one, not the kuckuck one.
        env["HOME"] = str(tmp_path)
        env["KUCKUCK_KEY_FILE"] = str(tmp_path / "no-such-key")
        result = subprocess.run(
            ["bash", str(_BUNDLED_SH)],
            input="not a JSON payload at all",
            text=True,
            capture_output=True,
            env=env,
            check=False,
            timeout=30,
        )
        assert result.returncode == 2
        assert "failed to parse stdin as JSON" in result.stderr

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


# ---------------------------------------------------------------------------
# PowerShell-script tests. These run pwsh against the real .ps1 payload
# so we cover the Windows codepath without needing a Windows CI runner.
# Install pwsh (cross-platform): https://aka.ms/install-powershell
# ---------------------------------------------------------------------------


def _run_pwsh_hook(
    *,
    tool_name: str,
    file_path: Path | None,
    path_field: str = "file_path",
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Spawn pwsh with the bundled .ps1 script and a crafted stdin payload."""
    payload: dict[str, object] = {
        "session_id": "test-session",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {path_field: str(file_path)} if file_path is not None else {},
    }
    env = os.environ.copy()
    kuckuck_dir = str(Path(_kuckuck_bin()).parent)
    env["PATH"] = f"{kuckuck_dir}{os.pathsep}{env.get('PATH', '')}"
    if env_overrides:
        for key, value in env_overrides.items():
            env[key] = value

    return subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(_BUNDLED_PS1)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=120,
    )


@pwsh_required
class TestPowerShellHookScript:
    """End-to-end pwsh coverage mirroring the bash subprocess tests."""

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

        result = _run_pwsh_hook(
            tool_name="Read",
            file_path=eml_file,
            env_overrides={"KUCKUCK_KEY_FILE": str(key_file)},
        )
        assert result.returncode == 0, result.stderr

        after = eml_file.read_text(encoding="utf-8")
        assert "klaus.mueller@firma.de" not in after
        assert "[[EMAIL_" in after

    def test_grep_payload_uses_path_field(self, eml_file: Path, key_file: Path) -> None:
        result = _run_pwsh_hook(
            tool_name="Grep",
            file_path=eml_file,
            path_field="path",
            env_overrides={"KUCKUCK_KEY_FILE": str(key_file)},
        )
        assert result.returncode == 0, result.stderr
        assert "[[EMAIL_" in eml_file.read_text(encoding="utf-8")

    def test_missing_file_path_field_passes_through(self) -> None:
        result = _run_pwsh_hook(tool_name="Read", file_path=None)
        assert result.returncode == 0
        assert "[kuckuck-hook]" not in result.stderr

    def test_nonexistent_file_is_ignored(self, tmp_path: Path) -> None:
        result = _run_pwsh_hook(
            tool_name="Read",
            file_path=tmp_path / "does-not-exist.eml",
        )
        assert result.returncode == 0

    def test_missing_key_blocks_tool_call(self, tmp_path: Path, eml_file: Path) -> None:
        bogus_key_path = tmp_path / "no-such-key"
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        result = _run_pwsh_hook(
            tool_name="Read",
            file_path=eml_file,
            env_overrides={
                "KUCKUCK_KEY_FILE": str(bogus_key_path),
                "HOME": str(fake_home),
            },
        )
        assert result.returncode == 2, result.stderr
        assert "Refusing to Read" in result.stderr
        assert "kuckuck_pseudonymize" in result.stderr
        assert "exit 3" in result.stderr

    def test_fail_open_env_var_lets_tool_through(self, tmp_path: Path, eml_file: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        result = _run_pwsh_hook(
            tool_name="Read",
            file_path=eml_file,
            env_overrides={
                "KUCKUCK_KEY_FILE": str(tmp_path / "no-such-key"),
                "HOME": str(fake_home),
                "KUCKUCK_HOOK_FAIL_OPEN": "1",
            },
        )
        assert result.returncode == 0
        assert "UNSAFE" in result.stderr

    def test_garbled_stdin_json_blocks(self, tmp_path: Path) -> None:
        env = os.environ.copy()
        env["PATH"] = f"{Path(_kuckuck_bin()).parent}{os.pathsep}{env.get('PATH', '')}"
        env["HOME"] = str(tmp_path)
        env["KUCKUCK_KEY_FILE"] = str(tmp_path / "no-such-key")
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(_BUNDLED_PS1)],
            input="this is not JSON",
            text=True,
            capture_output=True,
            env=env,
            check=False,
            timeout=30,
        )
        assert result.returncode == 2
        assert "failed to parse stdin as JSON" in result.stderr
