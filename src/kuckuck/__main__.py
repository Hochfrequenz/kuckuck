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

import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from cryptography.exceptions import InvalidTag
from pydantic import SecretStr

from kuckuck.config import DEFAULT_KEY_PATH, PROJECT_KEY_NAME, KeyNotFoundError, init_key, load_key
from kuckuck.detectors.ner import (
    DEFAULT_MODEL_ID,
    default_cache_root,
    default_model_path,
    is_gliner_installed,
    is_model_available,
)
from kuckuck.mapping import Mapping, MappingCorruptError, load_mapping, save_mapping
from kuckuck.options import RunOptions
from kuckuck.preprocessors import (
    EmlPreprocessor,
    MarkdownPreprocessor,
    MsgPreprocessor,
    Preprocessor,
    TextPreprocessor,
    XmlPreprocessor,
)
from kuckuck.pseudonymize import (
    PseudonymizeResult,
    build_default_detectors,
    pseudonymize_msg_file,
    pseudonymize_text,
    restore_text,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

# pseudonymize_msg_file lives in kuckuck.pseudonymize because it shares the
# detector / mapping / counter plumbing with pseudonymize_text. The CLI
# uses it for --format msg where the input is a binary OLE compound doc.

app = typer.Typer(
    help="Lokale Pseudonymisierung personenbezogener Daten vor der Weitergabe an Cloud-LLMs.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

#: Names of explicit subcommands — kept in sync with the ``@app.command`` registrations.
_SUBCOMMANDS = frozenset({"init-key", "restore", "inspect", "list-detectors", "version", "run", "fetch-model"})

#: Return codes used across the CLI. Stable so shell scripts can dispatch on them.
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_KEY_NOT_FOUND = 3
EXIT_MAPPING_MISSING = 4
EXIT_MAPPING_CORRUPT = 5
EXIT_MAPPING_WRONG_KEY = 6
EXIT_MODEL_MISSING = 7


#: Map ``--format`` choices to their preprocessor implementations.
_PREPROCESSORS: dict[str, type[Preprocessor]] = {
    "text": TextPreprocessor,
    "eml": EmlPreprocessor,
    "msg": MsgPreprocessor,
    "md": MarkdownPreprocessor,
    "xml": XmlPreprocessor,
}

#: Auto-detection table. Suffix lookup is case-insensitive.
_FORMAT_BY_SUFFIX: dict[str, str] = {
    ".eml": "eml",
    ".msg": "msg",
    ".md": "md",
    ".markdown": "md",
    ".xml": "xml",
    ".html": "xml",  # parses fine as XML for the structural walk
}


def _select_preprocessor(format_name: str, path: Path) -> Preprocessor:
    """Resolve ``--format`` to a concrete preprocessor instance.

    ``--format auto`` (the default) uses the file suffix to decide;
    everything else picks the named entry from :data:`_PREPROCESSORS`.
    Unknown suffixes fall back to the plain-text preprocessor so the
    default behaviour stays compatible with PR 1 / PR 2.
    """
    if format_name == "auto":
        format_name = _FORMAT_BY_SUFFIX.get(path.suffix.lower(), "text")
    cls = _PREPROCESSORS.get(format_name)
    if cls is None:
        raise typer.BadParameter(f"Unknown --format '{format_name}'")
    return cls()


def _load_mapping_or_exit(master: SecretStr, path: Path) -> Mapping:
    """Wrapper around :func:`load_mapping` that turns crypto errors into friendly CLI output."""
    try:
        return load_mapping(master, path)
    except MappingCorruptError as exc:
        typer.echo(f"Mapping file is corrupt: {path} ({exc})", err=True)
        raise typer.Exit(EXIT_MAPPING_CORRUPT) from exc
    except InvalidTag as exc:
        typer.echo(
            f"Could not decrypt mapping: {path}\n" f"The key does not match the one used to create this mapping.",
            err=True,
        )
        raise typer.Exit(EXIT_MAPPING_WRONG_KEY) from exc


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
        typer.echo(
            f"Key file already exists: {target}\nUse --force to overwrite it.",
            err=True,
        )
        raise typer.Exit(EXIT_USAGE) from exc
    typer.echo(f"Wrote new key to {written}")


@app.command("run")
def cmd_run(  # pylint: disable=too-many-arguments,too-many-positional-arguments
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
    format_: str = typer.Option(
        "auto",
        "--format",
        help=(
            "Input format. 'auto' (default) chooses by file suffix: "
            ".eml -> eml, .msg -> msg, .md / .markdown -> md, "
            ".xml / .html -> xml, everything else -> text. "
            "Note: msg always emits plain text (the .msg compound document "
            "is not round-tripped); attachments are dropped with a warning."
        ),
    ),
    ner: bool = typer.Option(
        False,
        "--ner/--no-ner",
        help="Enable the GLiNER PERSON detector. Requires 'kuckuck[ner]' installed and 'kuckuck fetch-model' run once.",
    ),
) -> None:
    """Pseudonymize one or more text files.

    By default each file is overwritten in place and a matching
    ``<file>.kuckuck-map.enc`` sidecar is written next to it.
    """
    options = RunOptions(
        key_file=key_file,
        output_dir=output_dir,
        dry_run=dry_run,
        sequential_tokens=sequential_tokens,
        denylist=denylist,
        phone_region=phone_region,
        format=format_,
        ner=ner,
    )
    run_pseudonymize(paths, options, _writer=_cli_progress_writer)


def _cli_progress_writer(line: str) -> None:
    """Adapter so the library API can stream per-file status to typer.echo."""
    typer.echo(line)


def run_pseudonymize(
    paths: list[Path],
    options: RunOptions,
    *,
    _writer: Callable[[str], None] | None = None,
) -> list[PseudonymizeResult]:
    """Library entry point for "pseudonymize a list of files end-to-end".

    Mirrors the behaviour of ``kuckuck run`` without the typer plumbing
    so it can be called from notebooks, scripts, or other Python code.
    Returns one :class:`PseudonymizeResult` per input path in input order.

    *_writer*, when set, receives one progress line per processed file.
    The CLI hands typer.echo here; library callers usually leave it
    ``None`` (silent).
    """
    try:
        master = load_key(options.key_file)
    except KeyNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_KEY_NOT_FOUND) from exc

    _check_ner_requirements(options)

    denylist_entries = _read_denylist(options.denylist)
    detectors = build_default_detectors(
        denylist=denylist_entries, phone_region=options.phone_region, use_ner=options.ner
    )

    if options.output_dir is not None:
        options.output_dir.mkdir(parents=True, exist_ok=True)

    results: list[PseudonymizeResult] = []
    for path in paths:
        result = _pseudonymize_path(path, master, detectors, options=options, writer=_writer)
        results.append(result)
    return results


def _check_ner_requirements(options: RunOptions) -> None:
    """Raise typer.Exit when ``--ner`` is set but the model isn't usable.

    Library callers also benefit from this guard: building a Run with
    ``ner=True`` on a system that has neither gliner nor the model
    fails fast with a clear EXIT_MODEL_MISSING instead of a silent
    regex-only run.
    """
    if not options.ner:
        return
    if not is_gliner_installed():
        typer.echo(
            "NER requested via --ner but the optional 'gliner' package is not installed.\n"
            "Install it via: pip install 'kuckuck[ner]'",
            err=True,
        )
        raise typer.Exit(EXIT_MODEL_MISSING)
    if not is_model_available():
        typer.echo(
            f"NER requested via --ner but no model was found at {default_model_path()}.\n"
            "Download it via: kuckuck fetch-model",
            err=True,
        )
        raise typer.Exit(EXIT_MODEL_MISSING)
    if options.sequential_tokens:
        typer.echo(
            "Warning: --ner with --sequential-tokens loses cross-document "
            "stability for PERSON tokens. Drop --sequential-tokens for "
            "stable hashes across files.",
            err=True,
        )


def _pseudonymize_path(
    path: Path,
    master: SecretStr,
    detectors: list,  # type: ignore[type-arg]
    *,
    options: RunOptions,
    writer: Callable[[str], None] | None,
) -> PseudonymizeResult:
    """Process a single file end-to-end: read, pseudonymize, write."""
    preprocessor = _select_preprocessor(options.format, path)
    target_text = options.output_dir / path.name if options.output_dir is not None else path
    target_map = _sidecar_path(target_text)
    mapping = _load_mapping_or_exit(master, target_map) if target_map.is_file() else Mapping()
    result = _pseudonymize_one(
        path=path,
        preprocessor=preprocessor,
        master=master,
        detectors=detectors,
        mapping=mapping,
        sequential_tokens=options.sequential_tokens,
    )
    if options.dry_run:
        if writer is not None:
            writer(f"--- {path} -> {len(result.replaced)} replacements ({preprocessor.name}) ---")
            writer(result.text)
        return result
    target_text.write_text(result.text, encoding="utf-8")
    save_mapping(master, result.mapping, target_map)
    if writer is not None:
        writer(
            f"{path} -> {target_text} ({len(result.replaced)} replacements, "
            f"format: {preprocessor.name}, map: {target_map})"
        )
    return result


def _pseudonymize_one(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    path: Path,
    preprocessor: Preprocessor,
    master: SecretStr,
    detectors: list,  # type: ignore[type-arg]
    mapping: Mapping,
    sequential_tokens: bool,
) -> PseudonymizeResult:
    """Read *path*, run the right pipeline, return the pseudonymize result.

    Branches on the preprocessor type so MsgPreprocessor (which needs
    binary input) takes the dedicated :func:`pseudonymize_msg_file` path
    while text-based preprocessors keep the existing UTF-8 read.
    Friendly errors translate library exceptions into typer.Exit(2).
    """
    if isinstance(preprocessor, MsgPreprocessor):
        if not path.is_file():
            typer.echo(f"{path}: not a regular file (refusing to read)", err=True)
            raise typer.Exit(EXIT_USAGE)
        try:
            return pseudonymize_msg_file(
                path,
                master,
                detectors,
                mapping=mapping,
                sequential_tokens=sequential_tokens,
            )
        except (OSError, ValueError) as exc:
            typer.echo(f"{path}: cannot parse as Outlook .msg: {exc}", err=True)
            raise typer.Exit(EXIT_USAGE) from exc

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        typer.echo(
            f"{path}: cannot decode as UTF-8 ({exc}). " "If this is an Outlook .msg file, pass --format msg.",
            err=True,
        )
        raise typer.Exit(EXIT_USAGE) from exc

    try:
        return pseudonymize_text(
            text,
            master,
            detectors,
            mapping=mapping,
            sequential_tokens=sequential_tokens,
            preprocessor=preprocessor,
        )
    except SyntaxError as exc:
        # lxml.etree.XMLSyntaxError inherits from SyntaxError; using the
        # base type lets us avoid pulling lxml symbols at the CLI level.
        typer.echo(
            f"{path}: invalid {preprocessor.name} document: {exc}. " "Try --format text to bypass structural parsing.",
            err=True,
        )
        raise typer.Exit(EXIT_USAGE) from exc


@app.command("restore")
def cmd_restore(
    paths: list[Path] = typer.Argument(..., exists=True, help="Pseudonymized files to restore in place."),
    key_file: Path | None = typer.Option(None, "--key-file", "-k", help="Override key lookup."),
    output_dir: Path | None = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Write restored output to this directory instead of overwriting in place.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Print the restored text instead of writing it."),
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
        mapping = _load_mapping_or_exit(master, target_map)
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
    key_file: Path | None = typer.Option(None, "--key-file", "-k", help="Override key lookup."),
) -> None:
    """Print a decrypted mapping for debugging. Handle with care — prints cleartext!"""
    try:
        master = load_key(key_file)
    except KeyNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_KEY_NOT_FOUND) from exc
    mapping = _load_mapping_or_exit(master, mapping_file)
    typer.echo(f"key_id: {mapping.key_id or '(none)'}")
    typer.echo(f"entries: {len(mapping)}")
    for token, entry in sorted(mapping.entries.items()):
        typer.echo(f"  [{entry.entity_type}] {token} -> {entry.original}")


@app.command("list-detectors")
def cmd_list_detectors() -> None:
    """Print every built-in detector with its default priority."""
    detectors = build_default_detectors(denylist=["__placeholder__"])
    if is_gliner_installed() and is_model_available():
        # Surface the NER detector in the listing only when actually usable.
        from kuckuck.detectors.ner import NerDetector  # pylint: disable=import-outside-toplevel

        detectors.append(NerDetector())
    for det in detectors:
        typer.echo(f"{det.name:12} priority={det.priority:<4} type={det.entity_type.value}")


#: Slugs derived from --model-id are written under the cache root.
#: Restrict to the safe character set HuggingFace itself uses for repo
#: names, with no path separators of any kind. Anything outside this
#: set is rejected before we touch the filesystem - prevents Windows
#: backslash traversal, empty slugs, and dotfile collisions.
_MODEL_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@app.command("fetch-model")
def cmd_fetch_model(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    model_id: str = typer.Option(
        DEFAULT_MODEL_ID,
        "--model-id",
        help="HuggingFace repo id of the GLiNER model to download.",
    ),
    cache_dir: Path | None = typer.Option(
        None,
        "--cache-dir",
        help=f"Target directory for the model snapshot (default: {default_cache_root()}).",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Re-download even if the model is already present."),
    allow_untrusted_model: bool = typer.Option(
        False,
        "--allow-untrusted-model",
        help=(
            "Permit a non-default --model-id. GLiNER weights are loaded via torch.load (pickle), "
            "which can execute arbitrary code if the repo is malicious. Only use with repos you trust."
        ),
    ),
) -> None:
    """Download the GLiNER model into the local model cache.

    The model is fetched once and reused across invocations. Default location
    is ``~/.cache/kuckuck/models/<slug>``. Network access is required only for
    this step; subsequent ``--ner`` runs work entirely offline.

    Custom ``--model-id`` requires ``--allow-untrusted-model`` because
    ``GLiNER.from_pretrained`` deserialises pickle from the downloaded
    weights file and a malicious repo can execute arbitrary code at the
    privileges of the calling user.
    """
    if not is_gliner_installed():
        typer.echo(
            "Cannot fetch model: the optional 'gliner' package is not installed.\n"
            "Install it via: pip install 'kuckuck[ner]'",
            err=True,
        )
        raise typer.Exit(EXIT_MODEL_MISSING)

    if model_id != DEFAULT_MODEL_ID and not allow_untrusted_model:
        typer.echo(
            f"Refusing to fetch '{model_id}': non-default model ids require --allow-untrusted-model.\n"
            "GLiNER weights are loaded via pickle and a malicious repo can execute arbitrary code.",
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)

    slug = model_id.split("/")[-1]
    if not _MODEL_SLUG_RE.match(slug):
        typer.echo(
            f"Refusing to fetch '{model_id}': slug '{slug}' contains characters that are not safe "
            "for use as a directory name (must match [A-Za-z0-9._-]).",
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)

    root = (cache_dir or default_cache_root()).expanduser().resolve()
    target = (root / slug).resolve()
    # Belt-and-braces: even with the slug regex, refuse anything that would
    # escape the cache root after resolve() (handles symlinks pointing out).
    if not _is_within(root, target):
        typer.echo(
            f"Refusing to fetch '{model_id}': resolved target {target} escapes cache root {root}.",
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)

    if target.is_dir() and is_model_available(target) and not force:
        typer.echo(f"Model already present: {target}")
        typer.echo("Pass --force to re-download.")
        return

    try:
        # Imported lazily so the CLI starts up without huggingface_hub on the
        # critical path (the dependency is only present in the [ner] extra).
        # pylint: disable-next=import-outside-toplevel,import-error
        from huggingface_hub import snapshot_download  # type: ignore[import-not-found]
    except ImportError as exc:
        typer.echo(
            "Cannot fetch model: huggingface_hub is missing. Reinstall 'kuckuck[ner]' to repair the install.",
            err=True,
        )
        raise typer.Exit(EXIT_MODEL_MISSING) from exc

    target.mkdir(parents=True, exist_ok=True)
    typer.echo(f"Downloading GLiNER model '{model_id}' to {target} ...")
    try:
        snapshot_download(repo_id=model_id, local_dir=str(target))
    except (OSError, ValueError, RuntimeError) as exc:
        # Wide net: huggingface_hub raises a mix of HfHubHTTPError /
        # RepositoryNotFoundError / RevisionNotFoundError / OSError. We do
        # not import them - the bare exception classes here would couple us
        # to private hub internals. Instead we catch the common base types,
        # delete the partial cache, and surface a friendly message + exit 7
        # so users do not see a raw stack trace.
        shutil.rmtree(target, ignore_errors=True)
        typer.echo(f"Failed to download '{model_id}': {exc}", err=True)
        raise typer.Exit(EXIT_MODEL_MISSING) from exc
    typer.echo(f"Done: {target}")


def _is_within(root: Path, candidate: Path) -> bool:
    """Return True when *candidate* lives under *root* after resolution.

    Implemented via ``relative_to`` (3.11+) with a try/except for the
    not-relative case. Avoids relying on ``str.startswith`` which is
    fragile under trailing-slash differences across platforms.
    """
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


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
