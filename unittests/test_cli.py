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


class TestFormatFlag:
    def test_auto_detects_eml_by_suffix(self, tmp_path: Path, key_file: Path) -> None:
        source = tmp_path / "msg.eml"
        source.write_text(
            "From: a@example.com\nTo: b@example.com\nSubject: hi\n\nKontakt max@firma.de\n",
            encoding="utf-8",
        )
        result = _invoke([str(source), "--key-file", str(key_file)])
        assert result.exit_code == 0
        out = source.read_text(encoding="utf-8")
        # Body got tokenized.
        assert "[[EMAIL_" in out
        # Headers stayed intact (eml preprocessor leaves them alone).
        assert "From:" in out
        assert "a@example.com" in out

    def test_auto_detects_markdown_by_suffix(self, tmp_path: Path, key_file: Path) -> None:
        source = tmp_path / "doc.md"
        source.write_text(
            "# Hi\n\nMail: max@firma.de\n\n```\nprivate code: max@firma.de\n```\n",
            encoding="utf-8",
        )
        result = _invoke([str(source), "--key-file", str(key_file)])
        assert result.exit_code == 0
        out = source.read_text(encoding="utf-8")
        # Code block content stays untouched.
        assert "private code: max@firma.de" in out
        # Prose got tokenized.
        assert "Mail: [[EMAIL_" in out

    def test_explicit_format_overrides_suffix(self, tmp_path: Path, key_file: Path) -> None:
        source = tmp_path / "looks.eml"  # suffix says eml
        source.write_text("Hallo max@firma.de", encoding="utf-8")
        result = runner.invoke(
            app,
            ["run", str(source), "--key-file", str(key_file), "--format", "text"],
        )
        # Text preprocessor treats whole input as one chunk - no header
        # parsing - so the email is found and tokenized.
        assert result.exit_code == 0
        out = source.read_text(encoding="utf-8")
        assert "[[EMAIL_" in out

    def test_unknown_format_returns_error(self, tmp_path: Path, key_file: Path) -> None:
        source = tmp_path / "doc.txt"
        source.write_text("text", encoding="utf-8")
        result = runner.invoke(
            app,
            ["run", str(source), "--key-file", str(key_file), "--format", "weird"],
        )
        assert result.exit_code != 0

    def test_unknown_suffix_falls_back_to_text(self, tmp_path: Path, key_file: Path) -> None:
        source = tmp_path / "doc.weirdsuffix"
        source.write_text("Hallo max@firma.de\n", encoding="utf-8")
        result = _invoke([str(source), "--key-file", str(key_file)])
        assert result.exit_code == 0
        assert "[[EMAIL_" in source.read_text(encoding="utf-8")

    def test_invalid_xml_returns_friendly_error(self, tmp_path: Path, key_file: Path) -> None:
        # Plain text saved as .xml: the parser raises XMLSyntaxError which
        # the CLI must translate into a one-line message + EXIT_USAGE,
        # not a multi-screen Python traceback.
        source = tmp_path / "broken.xml"
        source.write_text("just plain text max@firma.de", encoding="utf-8")
        result = _invoke([str(source), "--key-file", str(key_file)])
        assert result.exit_code == 2  # EXIT_USAGE
        assert "invalid xml document" in result.output.lower()
        assert "Try --format text" in result.output

    def test_binary_input_returns_friendly_error(self, tmp_path: Path, key_file: Path) -> None:
        # A raw binary file passed without --format msg should not crash
        # with a Unicode traceback.
        source = tmp_path / "doc.bin"
        source.write_bytes(b"\xff\xfe\x00\x01\x02binary garbage\xff")
        result = _invoke([str(source), "--key-file", str(key_file)])
        assert result.exit_code == 2  # EXIT_USAGE
        assert "cannot decode" in result.output.lower()
        assert "--format msg" in result.output

    def test_msg_format_dispatches_to_pseudonymize_msg_file(
        self, tmp_path: Path, key_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end CLI run on a stub .msg: the MsgPreprocessor reads
        # the path itself, NOT the decoded text. We swap extract_msg with
        # a fake module so the test does not need a real OLE compound doc.
        import sys
        import types

        class FakeMsg:
            def __init__(self) -> None:
                # extract-msg uses camelCase attribute names. We mirror
                # them here so the duck-typed access in MsgPreprocessor
                # works against the fake.
                self.attachments: list[int] = []
                self.htmlBody = b""  # pylint: disable=invalid-name
                self.rtfBody = b""  # pylint: disable=invalid-name
                self.body = "Hallo max@firma.de"

            def close(self) -> None:
                pass

        fake_module = types.ModuleType("extract_msg")
        fake_module.openMsg = lambda p, **kw: FakeMsg()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "extract_msg", fake_module)

        # The file must exist for path.is_file() to pass; its contents
        # are irrelevant because the fake module does not read them.
        source = tmp_path / "stub.msg"
        source.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")  # OLE magic prefix
        result = runner.invoke(app, ["run", str(source), "--key-file", str(key_file), "--format", "msg"])
        assert result.exit_code == 0, result.output
        out = source.read_text(encoding="utf-8")
        assert "[[EMAIL_" in out
        assert "max@firma.de" not in out


class TestNerFlag:
    def test_run_ner_without_gliner_exits_model_missing(
        self, tmp_path: Path, key_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kuckuck.__main__.is_gliner_installed", lambda: False)
        source = tmp_path / "doc.txt"
        source.write_text("Kontakt max@firma.de", encoding="utf-8")
        result = runner.invoke(app, ["run", str(source), "--key-file", str(key_file), "--ner"])
        assert result.exit_code == 7  # EXIT_MODEL_MISSING
        assert "gliner" in result.output.lower()

    def test_run_ner_without_model_exits_model_missing(
        self, tmp_path: Path, key_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kuckuck.__main__.is_gliner_installed", lambda: True)
        monkeypatch.setattr("kuckuck.__main__.is_model_available", lambda: False)
        source = tmp_path / "doc.txt"
        source.write_text("Kontakt max@firma.de", encoding="utf-8")
        result = runner.invoke(app, ["run", str(source), "--key-file", str(key_file), "--ner"])
        assert result.exit_code == 7
        assert "fetch-model" in result.output

    def test_run_no_ner_works_without_gliner(
        self, tmp_path: Path, key_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Without --ner, the run path must succeed even when gliner is missing
        # AND must NOT consult is_gliner_installed (cheap-startup invariant).
        called: dict[str, bool] = {"checked": False}

        def fake_check() -> bool:
            called["checked"] = True
            return False

        monkeypatch.setattr("kuckuck.__main__.is_gliner_installed", fake_check)
        source = tmp_path / "doc.txt"
        source.write_text("Kontakt max@firma.de", encoding="utf-8")
        result = runner.invoke(app, ["run", str(source), "--key-file", str(key_file)])
        assert result.exit_code == 0
        assert called["checked"] is False, "is_gliner_installed should not be called without --ner"

    def test_run_ner_with_sequential_tokens_warns(
        self, tmp_path: Path, key_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Combining --ner with --sequential-tokens loses cross-doc stability;
        # users should see a warning even though the run still succeeds.
        # We use a fake NerDetector to avoid loading the real model.
        from kuckuck.detectors.ner import NerDetector

        monkeypatch.setattr("kuckuck.__main__.is_gliner_installed", lambda: True)
        monkeypatch.setattr("kuckuck.__main__.is_model_available", lambda: True)
        monkeypatch.setattr(
            NerDetector,
            "_load",
            lambda self: type(
                "FakeModel",
                (),
                {"predict_entities": staticmethod(lambda *a, **kw: [])},
            )(),
        )
        source = tmp_path / "doc.txt"
        source.write_text("Kontakt max@firma.de", encoding="utf-8")
        result = runner.invoke(
            app,
            [
                "run",
                str(source),
                "--key-file",
                str(key_file),
                "--ner",
                "--sequential-tokens",
            ],
        )
        assert result.exit_code == 0
        assert "cross-document stability" in result.output


class TestFetchModel:
    def test_fetch_without_gliner_exits_model_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kuckuck.__main__.is_gliner_installed", lambda: False)
        result = runner.invoke(app, ["fetch-model"])
        assert result.exit_code == 7
        assert "kuckuck[ner]" in result.output

    def test_fetch_skips_when_already_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kuckuck.__main__.is_gliner_installed", lambda: True)
        target_root = tmp_path / "cache"
        # Pre-create a populated model dir for the default model id slug
        # (config + weights so is_model_available passes).
        model_dir = target_root / "gliner_multi-v2.1"
        model_dir.mkdir(parents=True)
        (model_dir / "config.json").write_text("{}", encoding="utf-8")
        (model_dir / "model.safetensors").write_bytes(b"")

        result = runner.invoke(app, ["fetch-model", "--cache-dir", str(target_root)])
        assert result.exit_code == 0
        assert "already present" in result.output

    def test_fetch_invokes_snapshot_download(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kuckuck.__main__.is_gliner_installed", lambda: True)
        calls: list[dict[str, str]] = []

        def fake_snapshot_download(repo_id: str, local_dir: str) -> str:
            calls.append({"repo_id": repo_id, "local_dir": local_dir})
            Path(local_dir).mkdir(parents=True, exist_ok=True)
            (Path(local_dir) / "config.json").write_text("{}", encoding="utf-8")
            (Path(local_dir) / "model.safetensors").write_bytes(b"")
            return local_dir

        # Inject a fake huggingface_hub module so the import inside
        # cmd_fetch_model resolves without a real install.
        import sys as _sys
        import types as _types

        fake_mod = _types.ModuleType("huggingface_hub")
        fake_mod.snapshot_download = fake_snapshot_download  # type: ignore[attr-defined]
        monkeypatch.setitem(_sys.modules, "huggingface_hub", fake_mod)

        result = runner.invoke(app, ["fetch-model", "--cache-dir", str(tmp_path / "cache")])
        assert result.exit_code == 0
        assert calls and calls[0]["repo_id"] == "urchade/gliner_multi-v2.1"
        assert "Done" in result.output

    def test_fetch_rejects_non_default_model_id_without_allow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defence-in-depth against pickle RCE: --model-id is gated behind
        # --allow-untrusted-model. The default model id (urchade/...) must
        # always work without the flag.
        monkeypatch.setattr("kuckuck.__main__.is_gliner_installed", lambda: True)
        result = runner.invoke(
            app, ["fetch-model", "--model-id", "attacker/evil-gliner"]
        )
        assert result.exit_code == 2  # EXIT_USAGE
        assert "--allow-untrusted-model" in result.output

    def test_fetch_rejects_unsafe_slug(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Path traversal attempt via --model-id. The slug regex must reject
        # anything outside [A-Za-z0-9._-].
        monkeypatch.setattr("kuckuck.__main__.is_gliner_installed", lambda: True)
        result = runner.invoke(
            app,
            [
                "fetch-model",
                "--model-id",
                "user/..\\..\\..\\Windows\\Temp\\evil",
                "--allow-untrusted-model",
            ],
        )
        assert result.exit_code == 2
        assert "not safe" in result.output

    def test_fetch_rejects_empty_slug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kuckuck.__main__.is_gliner_installed", lambda: True)
        # Trailing slash makes the slug empty.
        result = runner.invoke(
            app, ["fetch-model", "--model-id", "user/", "--allow-untrusted-model"]
        )
        assert result.exit_code == 2

    def test_fetch_cleans_up_partial_cache_on_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate snapshot_download writing a partial file then crashing.
        # The next is_model_available check must report False so a
        # subsequent --ner call exits 7 cleanly instead of crashing inside
        # GLiNER.from_pretrained.
        monkeypatch.setattr("kuckuck.__main__.is_gliner_installed", lambda: True)

        def fake_snapshot_download(repo_id: str, local_dir: str) -> str:  # noqa: ARG001
            # pylint: disable=unused-argument
            Path(local_dir).mkdir(parents=True, exist_ok=True)
            (Path(local_dir) / "config.json").write_text("{}", encoding="utf-8")
            raise OSError("simulated network failure mid-download")

        import sys as _sys
        import types as _types

        fake_mod = _types.ModuleType("huggingface_hub")
        fake_mod.snapshot_download = fake_snapshot_download  # type: ignore[attr-defined]
        monkeypatch.setitem(_sys.modules, "huggingface_hub", fake_mod)

        target_root = tmp_path / "cache"
        result = runner.invoke(
            app, ["fetch-model", "--cache-dir", str(target_root)]
        )
        assert result.exit_code == 7  # EXIT_MODEL_MISSING
        assert "Failed to download" in result.output
        # Partial directory must be gone after cleanup.
        assert not (target_root / "gliner_multi-v2.1").exists()

    def test_fetch_huggingface_hub_missing_exits_model_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate broken install: gliner present, huggingface_hub absent.
        # find_spec returning a spec object means installed; we patch the
        # CLI helper directly instead.
        monkeypatch.setattr("kuckuck.__main__.is_gliner_installed", lambda: True)
        import sys as _sys

        # Make sure huggingface_hub import fails.
        monkeypatch.setitem(_sys.modules, "huggingface_hub", None)
        result = runner.invoke(
            app, ["fetch-model", "--cache-dir", str(tmp_path / "c")]
        )
        assert result.exit_code == 7
        assert "huggingface_hub is missing" in result.output

    def test_fetch_force_redownloads(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kuckuck.__main__.is_gliner_installed", lambda: True)
        target_root = tmp_path / "cache"
        model_dir = target_root / "gliner_multi-v2.1"
        model_dir.mkdir(parents=True)
        (model_dir / "config.json").write_text("{}", encoding="utf-8")

        called = {"n": 0}

        def fake_snapshot_download(repo_id: str, local_dir: str) -> str:  # pylint: disable=unused-argument
            called["n"] += 1
            return local_dir

        import sys as _sys
        import types as _types

        fake_mod = _types.ModuleType("huggingface_hub")
        fake_mod.snapshot_download = fake_snapshot_download  # type: ignore[attr-defined]
        monkeypatch.setitem(_sys.modules, "huggingface_hub", fake_mod)

        result = runner.invoke(app, ["fetch-model", "--cache-dir", str(target_root), "--force"])
        assert result.exit_code == 0
        assert called["n"] == 1


class TestListDetectorsWithNer:
    def test_listing_includes_ner_when_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kuckuck.__main__.is_gliner_installed", lambda: True)
        monkeypatch.setattr("kuckuck.__main__.is_model_available", lambda: True)
        result = runner.invoke(app, ["list-detectors"])
        assert result.exit_code == 0
        assert "ner" in result.output

    def test_listing_omits_ner_when_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kuckuck.__main__.is_gliner_installed", lambda: False)
        result = runner.invoke(app, ["list-detectors"])
        assert result.exit_code == 0
        assert "\nner " not in result.output and not result.output.startswith("ner ")
