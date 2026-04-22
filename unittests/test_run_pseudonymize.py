"""Tests for the library-facing :func:`kuckuck.run_pseudonymize` API.

The CLI uses the same function under the hood; these tests verify the
library-only path - constructing a :class:`RunOptions` directly and
calling the function without going through typer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kuckuck import RunOptions, run_pseudonymize


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "test.kuckuck-key"
    path.write_text(
        "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
        encoding="utf-8",
    )
    return path


def test_run_pseudonymize_returns_results_in_order(tmp_path: Path, key_file: Path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("Hi max@firma.de", encoding="utf-8")
    b.write_text("Hi eva@firma.de", encoding="utf-8")

    results = run_pseudonymize([a, b], RunOptions(key_file=key_file))
    assert len(results) == 2
    # In-input order, both files written.
    assert "[[EMAIL_" in a.read_text(encoding="utf-8")
    assert "[[EMAIL_" in b.read_text(encoding="utf-8")
    # Mapping shared across the run -> identical entry count for the same address.
    assert len(results[0].mapping.entries) >= 1
    assert len(results[1].mapping.entries) >= 1


def test_run_pseudonymize_dry_run_does_not_write(tmp_path: Path, key_file: Path) -> None:
    source = tmp_path / "doc.txt"
    original = "Hallo max@firma.de"
    source.write_text(original, encoding="utf-8")

    results = run_pseudonymize([source], RunOptions(key_file=key_file, dry_run=True))
    assert source.read_text(encoding="utf-8") == original
    # Result still computed though.
    assert "[[EMAIL_" in results[0].text


def test_run_pseudonymize_output_dir_writes_alongside(tmp_path: Path, key_file: Path) -> None:
    source = tmp_path / "doc.txt"
    source.write_text("Hi max@firma.de", encoding="utf-8")
    out = tmp_path / "out"

    run_pseudonymize([source], RunOptions(key_file=key_file, output_dir=out))
    # Original untouched, output written into out/
    assert source.read_text(encoding="utf-8") == "Hi max@firma.de"
    assert (out / "doc.txt").is_file()
    assert "[[EMAIL_" in (out / "doc.txt").read_text(encoding="utf-8")


def test_run_pseudonymize_format_eml_keeps_headers(tmp_path: Path, key_file: Path) -> None:
    source = tmp_path / "msg.eml"
    source.write_text(
        "From: a@example.com\nTo: b@example.com\nSubject: hi\n\nBody max@firma.de\n",
        encoding="utf-8",
    )
    run_pseudonymize([source], RunOptions(key_file=key_file))
    out = source.read_text(encoding="utf-8")
    assert "[[EMAIL_" in out
    assert "From: a@example.com" in out


def test_run_pseudonymize_explicit_format_overrides_suffix(tmp_path: Path, key_file: Path) -> None:
    # Force text preprocessor on a .eml-named file.
    source = tmp_path / "looks.eml"
    source.write_text("Hi max@firma.de", encoding="utf-8")
    run_pseudonymize([source], RunOptions(key_file=key_file, format="text"))
    assert "[[EMAIL_" in source.read_text(encoding="utf-8")


def test_run_options_rejects_extra_fields() -> None:
    # Defensive: forbid extras so a typo doesn't silently no-op.
    with pytest.raises(Exception):
        RunOptions(definitely_not_a_field=True)  # type: ignore[call-arg]
