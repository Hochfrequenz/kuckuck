"""Tests for the config module — key lookup chain and init_key."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from kuckuck.config import (
    DEFAULT_KEY_PATH,
    KEY_ENV_VAR,
    PROJECT_KEY_NAME,
    KeyNotFoundError,
    generate_key,
    init_key,
    load_key,
)


class TestGenerateKey:
    def test_generate_key_returns_hex_of_expected_length(self) -> None:
        key = generate_key()
        assert len(key) == 64  # 32 bytes → 64 hex chars
        assert all(c in "0123456789abcdef" for c in key)

    def test_generate_key_is_random(self) -> None:
        assert generate_key() != generate_key()


class TestInitKey:
    def test_init_key_writes_to_explicit_path(self, tmp_path: Path) -> None:
        target = tmp_path / "key"
        written = init_key(target)
        assert written == target
        content = target.read_text(encoding="utf-8").strip()
        assert len(content) == 64

    def test_init_key_creates_parent_directories(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "key"
        init_key(target)
        assert target.is_file()

    def test_init_key_refuses_overwrite_by_default(self, tmp_path: Path) -> None:
        target = tmp_path / "key"
        init_key(target)
        with pytest.raises(FileExistsError):
            init_key(target)

    def test_init_key_overwrites_when_explicit(self, tmp_path: Path) -> None:
        target = tmp_path / "key"
        init_key(target)
        first = target.read_text(encoding="utf-8").strip()
        init_key(target, overwrite=True)
        second = target.read_text(encoding="utf-8").strip()
        assert first != second


class TestLoadKey:
    def test_explicit_path_wins(self, tmp_path: Path) -> None:
        explicit = tmp_path / "explicit.key"
        explicit.write_text("deadbeef", encoding="utf-8")
        loaded = load_key(explicit)
        assert isinstance(loaded, SecretStr)
        assert loaded.get_secret_value() == "deadbeef"

    def test_env_var_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_key = tmp_path / "env.key"
        env_key.write_text("envbeef", encoding="utf-8")
        monkeypatch.setenv(KEY_ENV_VAR, str(env_key))
        monkeypatch.chdir(tmp_path)  # no project key here
        assert load_key().get_secret_value() == "envbeef"

    def test_project_key_found_in_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project_key = tmp_path / PROJECT_KEY_NAME
        project_key.write_text("projectbeef", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv(KEY_ENV_VAR, raising=False)
        assert load_key().get_secret_value() == "projectbeef"

    def test_user_scoped_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = tmp_path / "home"
        user_key = fake_home / ".config" / "kuckuck" / "key"
        user_key.parent.mkdir(parents=True)
        user_key.write_text("userbeef", encoding="utf-8")

        monkeypatch.delenv(KEY_ENV_VAR, raising=False)
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("USERPROFILE", str(fake_home))  # Windows

        cwd = tmp_path / "workdir"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        assert load_key().get_secret_value() == "userbeef"

    def test_precedence_explicit_over_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        explicit = tmp_path / "explicit.key"
        explicit.write_text("winner", encoding="utf-8")
        env_key = tmp_path / "env.key"
        env_key.write_text("loser", encoding="utf-8")
        monkeypatch.setenv(KEY_ENV_VAR, str(env_key))
        assert load_key(explicit).get_secret_value() == "winner"

    def test_precedence_env_over_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_key = tmp_path / "env.key"
        env_key.write_text("envwins", encoding="utf-8")
        project_key = tmp_path / PROJECT_KEY_NAME
        project_key.write_text("projectloses", encoding="utf-8")
        monkeypatch.setenv(KEY_ENV_VAR, str(env_key))
        monkeypatch.chdir(tmp_path)
        assert load_key().get_secret_value() == "envwins"

    def test_precedence_project_over_user(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = tmp_path / "home"
        user_key = fake_home / ".config" / "kuckuck" / "key"
        user_key.parent.mkdir(parents=True)
        user_key.write_text("userloses", encoding="utf-8")
        cwd = tmp_path / "workdir"
        cwd.mkdir()
        (cwd / PROJECT_KEY_NAME).write_text("projectwins", encoding="utf-8")

        monkeypatch.delenv(KEY_ENV_VAR, raising=False)
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("USERPROFILE", str(fake_home))
        monkeypatch.chdir(cwd)
        assert load_key().get_secret_value() == "projectwins"

    def test_missing_everywhere_raises_key_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_home = tmp_path / "empty-home"
        fake_home.mkdir()
        cwd = tmp_path / "empty-work"
        cwd.mkdir()

        monkeypatch.delenv(KEY_ENV_VAR, raising=False)
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("USERPROFILE", str(fake_home))
        monkeypatch.chdir(cwd)

        with pytest.raises(KeyNotFoundError) as exc:
            load_key()
        assert "kuckuck init-key" in str(exc.value)

    def test_empty_key_file_raises(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.key"
        empty.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="empty"):
            load_key(empty)

    def test_default_key_path_is_xdg_style(self) -> None:
        assert DEFAULT_KEY_PATH.startswith("~/.config/kuckuck/")

    def test_secret_str_is_not_revealed_in_repr(self, tmp_path: Path) -> None:
        k = tmp_path / "k"
        k.write_text("topsecret", encoding="utf-8")
        loaded = load_key(k)
        assert "topsecret" not in repr(loaded)
        assert "topsecret" not in str(loaded)
