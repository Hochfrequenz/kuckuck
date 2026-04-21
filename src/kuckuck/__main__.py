"""Command-line entry point for Kuckuck.

Usage highlights — details in ``--help`` and in :mod:`kuckuck.__init__`.

``kuckuck foo.txt``
    Pseudonymize ``foo.txt`` in place and write ``foo.kuckuck-map.enc`` next
    to it. This is the simplest, most-used invocation; the implicit ``run``
    subcommand is inserted when the first argument is a file path.

``kuckuck run foo.txt [options]``
    Explicit form of the above.

``kuckuck restore foo.txt``
    Replace every known token in ``foo.txt`` with its original value by
    reading the sidecar ``foo.kuckuck-map.enc``.

``kuckuck init-key``
    Generate a fresh master secret at ``~/.config/kuckuck/key``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from kuckuck.config import DEFAULT_KEY_PATH, PROJECT_KEY_NAME, KeyNotFoundError, init_key, load_key
from kuckuck.mapping import Mapping, load_mapping, save_mapping
from kuckuck.pseudonymize import build_default_detectors, pseudonymize_text, restore_text

app = typer.Typer(
    help="Lokale Pseudonymisierung personenbezogener Daten vor der Weitergabe an Cloud-LLMs.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

#: Names of explicit subcommands — kept in sync with the ``@app.command`` registrations.
_SUBCOMMANDS = frozenset({"init-key", "restore", "inspect", "list-detectors", "version", "run"})

#: Return codes used across the CLI. Stable so shell scripts can dispatch on them.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_KEY_NOT_FOUND = 3
EXIT_MAPPING_MISSING = 4
EXIT_GENERIC = 1


def _sidecar_path(file_path: Path) -> Path:
    """Return the ``.kuckuck-map.enc`` path that lives next to *file_path*."""
    return file_path.with_suffix(file_path.suffix + ".kuckuck-map.enc")


def _read_denylist(path: Path | None) -> list[str]:
    if path is None:
        return []
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


@app.command("init-key")
def cmd_init_key(
    project: bool = typer.Option(
        False,
        "--project",
        help=f"Write to {PROJECT_KEY_NAME} in CWD instead of {DEFAULT_KEY_PATH}.",
    ),
    key_file: Path | None = typer.Option(None, "--key-file", "-k", help="Explicit path for the new key file."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing key file."),
) -> None:
    """Generate a new master secret."""
    if key_file is not None:
        target = key_file
    elif project:
        target = Path.cwd() / PROJECT_KEY_NAME
    else:
        target = Path(DEFAULT_KEY_PATH).expanduser()
    try:
        written = init_key(target, overwrite=force)
    except FileExistsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_USAGE) from exc
    typer.echo(f"Wrote new key to {written}")


@app.command("run")
def cmd_run(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    paths: list[Path] = typer.Argument(..., exists=True, help="Files to pseudonymize in place."),
    key_file: Path | None = typer.Option(None, "--key-file", "-k", help="Override key lookup."),
    output_dir: Path | None = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Write results to this directory instead of overwriting in place.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show changes without writing anything."),
    sequential_tokens: bool = typer.Option(
        False,
        "--sequential-tokens",
        help="Use [[PERSON_1]]-style counters per document (not cross-document stable).",
    ),
    denylist: Path | None = typer.Option(None, "--denylist", help="Path to a denylist file (one entry per line)."),
    phone_region: str = typer.Option(
        "DE", "--phone-region", help="Default ISO country code for parsing phone numbers."
    ),
) -> None:
    """Pseudonymize one or more text files.

    By default each file is overwritten in place and a matching
    ``<file>.kuckuck-map.enc`` sidecar is written next to it.
    """
    try:
        master = load_key(key_file)
    except KeyNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_KEY_NOT_FOUND) from exc

    denylist_entries = _read_denylist(denylist)
    detectors = build_default_detectors(denylist=denylist_entries, phone_region=phone_region)

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    for path in paths:
        target_text = output_dir / path.name if output_dir is not None else path
        target_map = _sidecar_path(target_text)
        mapping = load_mapping(master, target_map) if target_map.is_file() else Mapping()
        text = path.read_text(encoding="utf-8")
        result = pseudonymize_text(text, master, detectors, mapping=mapping, sequential_tokens=sequential_tokens)
        if dry_run:
            typer.echo(f"--- {path} -> {len(result.replaced)} replacements ---")
            typer.echo(result.text)
            continue
        target_text.write_text(result.text, encoding="utf-8")
        save_mapping(master, result.mapping, target_map)
        typer.echo(f"{path} -> {target_text} ({len(result.replaced)} replacements, map: {target_map})")


@app.command("restore")
def cmd_restore(
    paths: list[Path] = typer.Argument(..., exists=True, help="Pseudonymized files to restore in place."),
    key_file: Path | None = typer.Option(None, "--key-file", "-k"),
    output_dir: Path | None = typer.Option(None, "--output-dir", "-o"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n"),
) -> None:
    """Restore original values into pseudonymized files using the sidecar mapping."""
    try:
        master = load_key(key_file)
    except KeyNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_KEY_NOT_FOUND) from exc

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    for path in paths:
        target_map = _sidecar_path(path)
        if not target_map.is_file():
            typer.echo(f"Missing mapping: {target_map}", err=True)
            raise typer.Exit(EXIT_MAPPING_MISSING)
        mapping = load_mapping(master, target_map)
        text = path.read_text(encoding="utf-8")
        restored = restore_text(text, mapping)
        if dry_run:
            typer.echo(restored)
            continue
        destination = output_dir / path.name if output_dir is not None else path
        destination.write_text(restored, encoding="utf-8")
        typer.echo(f"{path} -> {destination}")


@app.command("inspect")
def cmd_inspect(
    mapping_file: Path = typer.Argument(..., exists=True, help="Encrypted mapping (.kuckuck-map.enc)."),
    key_file: Path | None = typer.Option(None, "--key-file", "-k"),
) -> None:
    """Print a decrypted mapping for debugging. Handle with care — prints cleartext!"""
    try:
        master = load_key(key_file)
    except KeyNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_KEY_NOT_FOUND) from exc
    mapping = load_mapping(master, mapping_file)
    typer.echo(f"key_id: {mapping.key_id or '(none)'}")
    typer.echo(f"entries: {len(mapping)}")
    for token, entry in sorted(mapping.entries.items()):
        typer.echo(f"  [{entry.entity_type}] {token} -> {entry.original}")


@app.command("list-detectors")
def cmd_list_detectors() -> None:
    """Print every built-in detector with its default priority."""
    for det in build_default_detectors(denylist=["__placeholder__"]):
        typer.echo(f"{det.name:12} priority={det.priority:<4} type={det.entity_type.value}")


@app.command("version")
def cmd_version() -> None:
    """Print the installed Kuckuck version."""
    typer.echo(_installed_version())


def _installed_version() -> str:
    """Return the version string from the generated ``_kuckuck_version`` module.

    The module is produced by ``hatch-vcs`` at build time. In an in-tree dev
    checkout it may be missing — in that case we return a stable sentinel so
    ``kuckuck version`` still produces an answer instead of raising.
    """
    try:
        from _kuckuck_version import version  # pylint: disable=import-outside-toplevel

        return str(version)
    except ImportError:
        return "0+unknown"


def inject_default_run(argv: list[str]) -> list[str]:
    """Prepend ``"run"`` to *argv* when the first token looks like a file path.

    This is the glue that makes ``kuckuck foo.txt`` work without requiring
    users to type the explicit ``run`` subcommand. Exposed so test harnesses
    can exercise the same dispatch path without spawning a subprocess.
    """
    if not argv:
        return argv
    first = argv[0]
    if first.startswith("-"):
        return argv
    if first in _SUBCOMMANDS:
        return argv
    return ["run", *argv]


def main() -> None:
    """CLI entry point registered via ``[project.scripts]`` in ``pyproject.toml``."""
    sys.argv[1:] = inject_default_run(sys.argv[1:])
    app()


if __name__ == "__main__":  # pragma: no cover - module is invoked via console_script
    main()
