"""Integration tests for the CLI.

Follows the skip-if-typer-missing pattern used elsewhere in the
Hochfrequenz Python stack so the library remains importable when the
optional ``cli`` extra is not installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_skip_cli = False
try:
    from typer.testing import CliRunner

    from kuckuck.__main__ import app, inject_default_run

    runner = CliRunner()
except ImportError:  # pragma: no cover - CI always installs typer
    _skip_cli = True


pytestmark = pytest.mark.skipif(_skip_cli, reason="typer not installed")


def _invoke(argv: list[str]):  # type: ignore[no-untyped-def]
    """CliRunner wrapper that applies the same default-run injection as ``main()``."""
    return runner.invoke(app, inject_default_run(argv))


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "test.kuckuck-key"
    path.write_text("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff", encoding="utf-8")
    return path


class TestInitKey:
    def test_init_key_to_explicit_path(self, tmp_path: Path) -> None:
        target = tmp_path / "newkey"
        result = runner.invoke(app, ["init-key", "--key-file", str(target)])
        assert result.exit_code == 0
        assert target.is_file()
        assert len(target.read_text(encoding="utf-8").strip()) == 64

    def test_init_key_refuses_overwrite(self, tmp_path: Path) -> None:
        target = tmp_path / "k"
        runner.invoke(app, ["init-key", "--key-file", str(target)])
        result = runner.invoke(app, ["init-key", "--key-file", str(target)])
        assert result.exit_code != 0
        assert "exists" in result.output.lower()
        # Must reference the CLI flag, not the Python API kwarg.
        assert "--force" in result.output
        assert "overwrite=true" not in result.output.lower()

    def test_init_key_force(self, tmp_path: Path) -> None:
        target = tmp_path / "k"
        runner.invoke(app, ["init-key", "--key-file", str(target)])
        first = target.read_text(encoding="utf-8")
        result = runner.invoke(app, ["init-key", "--key-file", str(target), "--force"])
        assert result.exit_code == 0
        assert target.read_text(encoding="utf-8") != first


class TestRun:
    def test_run_in_place(self, tmp_path: Path, key_file: Path) -> None:
        source = tmp_path / "doc.txt"
        source.write_text("Kontakt max@firma.de", encoding="utf-8")

        result = _invoke([str(source), "--key-file", str(key_file)])
        assert result.exit_code == 0
        content = source.read_text(encoding="utf-8")
        assert "max@firma.de" not in content
        assert "[[EMAIL_" in content
        assert (tmp_path / "doc.txt.kuckuck-map.enc").is_file()

    def test_run_explicit_subcommand(self, tmp_path: Path, key_file: Path) -> None:
        source = tmp_path / "doc.txt"
        source.write_text("Kontakt max@firma.de", encoding="utf-8")

        result = runner.invoke(app, ["run", str(source), "--key-file", str(key_file)])
        assert result.exit_code == 0
        assert "[[EMAIL_" in source.read_text(encoding="utf-8")

    def test_run_with_output_dir(self, tmp_path: Path, key_file: Path) -> None:
        source = tmp_path / "doc.txt"
        source.write_text("Kontakt max@firma.de", encoding="utf-8")
        outdir = tmp_path / "out"

        result = runner.invoke(
            app,
            ["run", str(source), "--key-file", str(key_file), "--output-dir", str(outdir)],
        )
        assert result.exit_code == 0
        assert source.read_text(encoding="utf-8") == "Kontakt max@firma.de"  # untouched
        assert (outdir / "doc.txt").is_file()
        assert (outdir / "doc.txt.kuckuck-map.enc").is_file()

    def test_run_dry_run_does_not_overwrite(self, tmp_path: Path, key_file: Path) -> None:
        source = tmp_path / "doc.txt"
        original = "Kontakt max@firma.de"
        source.write_text(original, encoding="utf-8")

        result = runner.invoke(app, ["run", str(source), "--key-file", str(key_file), "--dry-run"])
        assert result.exit_code == 0
        assert source.read_text(encoding="utf-8") == original

    def test_run_batch(self, tmp_path: Path, key_file: Path) -> None:
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("cc @eva", encoding="utf-8")
        b.write_text("Kontakt max@firma.de", encoding="utf-8")

        result = runner.invoke(app, ["run", str(a), str(b), "--key-file", str(key_file)])
        assert result.exit_code == 0
        assert "[[HANDLE_" in a.read_text(encoding="utf-8")
        assert "[[EMAIL_" in b.read_text(encoding="utf-8")

    def test_missing_key_returns_named_exit_code(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KUCKUCK_KEY_FILE", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        source = tmp_path / "doc.txt"
        source.write_text("some text", encoding="utf-8")

        result = _invoke([str(source)])
        assert result.exit_code == 3  # EXIT_KEY_NOT_FOUND

    def test_idempotency(self, tmp_path: Path, key_file: Path) -> None:
        source = tmp_path / "doc.txt"
        source.write_text("Kontakt max@firma.de", encoding="utf-8")

        runner.invoke(app, [str(source), "--key-file", str(key_file)])
        after_first = source.read_text(encoding="utf-8")
        runner.invoke(app, [str(source), "--key-file", str(key_file)])
        after_second = source.read_text(encoding="utf-8")
        assert after_first == after_second

    def test_sequential_tokens_flag(self, tmp_path: Path, key_file: Path) -> None:
        source = tmp_path / "doc.txt"
        source.write_text("a@b.de und c@d.de", encoding="utf-8")

        result = runner.invoke(
            app,
            ["run", str(source), "--key-file", str(key_file), "--sequential-tokens"],
        )
        assert result.exit_code == 0
        content = source.read_text(encoding="utf-8")
        assert "[[EMAIL_1]]" in content
        assert "[[EMAIL_2]]" in content

    def test_denylist_file(self, tmp_path: Path, key_file: Path) -> None:
        source = tmp_path / "doc.txt"
        source.write_text("Kunde Alpha GmbH hat gemeldet.", encoding="utf-8")
        denylist = tmp_path / "deny.txt"
        denylist.write_text("# Kommentare werden ignoriert\nAlpha GmbH\n", encoding="utf-8")

        result = runner.invoke(
            app,
            ["run", str(source), "--key-file", str(key_file), "--denylist", str(denylist)],
        )
        assert result.exit_code == 0
        assert "[[TERM_" in source.read_text(encoding="utf-8")


class TestRestore:
    def test_round_trip(self, tmp_path: Path, key_file: Path) -> None:
        source = tmp_path / "doc.txt"
        original = "Kontakt max@firma.de, cc @eva"
        source.write_text(original, encoding="utf-8")

        result = _invoke([str(source), "--key-file", str(key_file)])
        assert result.exit_code == 0
        result = runner.invoke(app, ["restore", str(source), "--key-file", str(key_file)])
        assert result.exit_code == 0
        assert source.read_text(encoding="utf-8") == original

    def test_restore_missing_mapping(self, tmp_path: Path, key_file: Path) -> None:
        source = tmp_path / "nomap.txt"
        source.write_text("pseudonymized output", encoding="utf-8")
        result = runner.invoke(app, ["restore", str(source), "--key-file", str(key_file)])
        assert result.exit_code == 4  # EXIT_MAPPING_MISSING

    def test_restore_missing_key_exit_code(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KUCKUCK_KEY_FILE", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        source = tmp_path / "doc.txt"
        source.write_text("[[EMAIL_xxx]]", encoding="utf-8")
        (tmp_path / "doc.txt.kuckuck-map.enc").write_bytes(b"dummy")

        result = runner.invoke(app, ["restore", str(source)])
        assert result.exit_code == 3  # EXIT_KEY_NOT_FOUND

    def test_restore_corrupt_mapping(self, tmp_path: Path, key_file: Path) -> None:
        source = tmp_path / "doc.txt"
        source.write_text("pseudo", encoding="utf-8")
        map_path = tmp_path / "doc.txt.kuckuck-map.enc"
        map_path.write_bytes(b"garbage that is not a kuckuck map file at all")
        result = runner.invoke(app, ["restore", str(source), "--key-file", str(key_file)])
        assert result.exit_code == 5  # EXIT_MAPPING_CORRUPT
        assert "corrupt" in result.output.lower()

    def test_restore_with_wrong_key(self, tmp_path: Path, key_file: Path) -> None:
        # Build a valid mapping with one key, try to restore with a different key
        source = tmp_path / "doc.txt"
        source.write_text("Kontakt max@firma.de", encoding="utf-8")
        _invoke([str(source), "--key-file", str(key_file)])

        other_key = tmp_path / "other.key"
        other_key.write_text("ff" * 32, encoding="utf-8")
        result = runner.invoke(app, ["restore", str(source), "--key-file", str(other_key)])
        assert result.exit_code == 6  # EXIT_MAPPING_WRONG_KEY
        assert "key does not match" in result.output.lower()


class TestInspectErrorPaths:
    def test_inspect_missing_key_exit_code(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        map_path = tmp_path / "bogus.enc"
        map_path.write_bytes(b"dummy")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KUCKUCK_KEY_FILE", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        result = runner.invoke(app, ["inspect", str(map_path)])
        assert result.exit_code == 3

    def test_inspect_corrupt_mapping(self, tmp_path: Path, key_file: Path) -> None:
        map_path = tmp_path / "bogus.enc"
        map_path.write_bytes(b"not a kuckuck mapping")
        result = runner.invoke(app, ["inspect", str(map_path), "--key-file", str(key_file)])
        assert result.exit_code == 5


class TestInspect:
    def test_shows_entries(self, tmp_path: Path, key_file: Path) -> None:
        source = tmp_path / "doc.txt"
        source.write_text("max@firma.de", encoding="utf-8")
        _invoke([str(source), "--key-file", str(key_file)])

        map_path = tmp_path / "doc.txt.kuckuck-map.enc"
        result = runner.invoke(app, ["inspect", str(map_path), "--key-file", str(key_file)])
        assert result.exit_code == 0
        assert "max@firma.de" in result.output
        assert "EMAIL" in result.output


class TestListDetectors:
    def test_output_contains_builtins(self) -> None:
        result = runner.invoke(app, ["list-detectors"])
        assert result.exit_code == 0
        for name in ("email", "phone", "handle"):
            assert name in result.output


class TestVersion:
    def test_runs(self) -> None:
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
