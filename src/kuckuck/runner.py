"""High-level pseudonymize-a-list-of-files runner.

This module is the implementation behind both the ``kuckuck run`` CLI
subcommand and the public :func:`run_pseudonymize` library entry
point. It deliberately does NOT depend on typer so the library API
stays usable without the ``[cli]`` extra installed.

Exceptions raised here (e.g. :class:`~kuckuck.config.KeyNotFoundError`,
:class:`~kuckuck.detectors.ner.NerNotInstalledError`,
:class:`~kuckuck.detectors.ner.NerModelMissingError`) propagate to the
caller. The CLI catches them in ``kuckuck.__main__`` and translates
them to ``typer.Exit`` with the appropriate exit code; library
callers can catch them as plain exceptions.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import SecretStr

from kuckuck.config import load_key
from kuckuck.detectors.base import Detector
from kuckuck.detectors.ner import (
    NerModelMissingError,
    NerNotInstalledError,
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
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

logger = logging.getLogger(__name__)


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


def select_preprocessor(format_name: str, path: Path) -> Preprocessor:
    """Resolve a format name to a concrete preprocessor instance.

    ``format_name == "auto"`` uses the file suffix to decide; everything
    else picks the named entry from :data:`_PREPROCESSORS`. Unknown
    suffixes fall back to the plain-text preprocessor.

    Raises :class:`ValueError` for unknown format names.
    """
    if format_name == "auto":
        format_name = _FORMAT_BY_SUFFIX.get(path.suffix.lower(), "text")
    cls = _PREPROCESSORS.get(format_name)
    if cls is None:
        raise ValueError(f"Unknown format '{format_name}'")
    return cls()


def _sidecar_path(file_path: Path) -> Path:
    """Return the ``.kuckuck-map.enc`` path that lives next to *file_path*."""
    return file_path.with_suffix(file_path.suffix + ".kuckuck-map.enc")


def _read_denylist(path: Path | None) -> list[str]:
    if path is None:
        return []
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def run_pseudonymize(
    paths: list[Path],
    options: RunOptions,
    *,
    progress_writer: Callable[[str], None] | None = None,
) -> list[PseudonymizeResult]:
    """Pseudonymize a batch of files end-to-end.

    Mirrors the behaviour of ``kuckuck run`` so notebooks, scripts and
    other Python code can drive the same pipeline without the typer
    plumbing. Returns one :class:`PseudonymizeResult` per input path in
    input order.

    :param paths: Files to process.
    :param options: Run-time configuration; see :class:`RunOptions`.
    :param progress_writer: Optional ``str -> None`` callback that
        receives one line per processed file. The CLI hands
        ``typer.echo`` here; library callers usually leave it ``None``.

    Raises:
        :class:`~kuckuck.config.KeyNotFoundError`: master key not found.
        :class:`~kuckuck.detectors.ner.NerNotInstalledError`:
            ``options.ner`` is True but ``gliner`` is not importable.
        :class:`~kuckuck.detectors.ner.NerModelMissingError`:
            ``options.ner`` is True but the model is not on disk.
        :class:`ValueError`: invalid ``options.format`` value.
    """
    master = load_key(options.key_file)
    _check_ner_requirements(options, writer=progress_writer)

    denylist_entries = _read_denylist(options.denylist)
    detectors = build_default_detectors(
        denylist=denylist_entries,
        phone_region=options.phone_region,
        use_ner=options.ner,
    )

    if options.output_dir is not None:
        options.output_dir.mkdir(parents=True, exist_ok=True)

    results: list[PseudonymizeResult] = []
    for path in paths:
        result = _pseudonymize_path(path, master, detectors, options=options, writer=progress_writer)
        results.append(result)
    return results


def _check_ner_requirements(
    options: RunOptions,
    *,
    writer: Callable[[str], None] | None,
) -> None:
    """Raise when ``options.ner`` is set but NER is not usable.

    Logs a warning when ``--ner`` and ``--sequential-tokens`` combine,
    because sequential per-document counters defeat the cross-doc
    stability that makes NER useful in the first place.
    """
    if not options.ner:
        return
    if not is_gliner_installed():
        raise NerNotInstalledError(
            "NER requested but the optional 'gliner' package is not installed. "
            "Install it via: pip install 'kuckuck[ner]'"
        )
    if not is_model_available():
        raise NerModelMissingError(
            f"NER requested but no model was found at {default_model_path()}. " "Download it via: kuckuck fetch-model"
        )
    if options.sequential_tokens:
        msg = (
            "Warning: --ner with --sequential-tokens loses cross-document "
            "stability for PERSON tokens. Drop --sequential-tokens for "
            "stable hashes across files."
        )
        if writer is not None:
            writer(msg)
        else:
            logger.warning(msg)


def _pseudonymize_path(
    path: Path,
    master: SecretStr,
    detectors: list[Detector],
    *,
    options: RunOptions,
    writer: Callable[[str], None] | None,
) -> PseudonymizeResult:
    """Process a single file end-to-end: read, pseudonymize, write."""
    preprocessor = select_preprocessor(options.format, path)
    target_text = options.output_dir / path.name if options.output_dir is not None else path
    target_map = _sidecar_path(target_text)
    mapping = _load_mapping_or_raise(master, target_map) if target_map.is_file() else Mapping()
    result = _run_one(
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


def _load_mapping_or_raise(master: SecretStr, path: Path) -> Mapping:
    """Wrap :func:`load_mapping` so the caller can rely on a Mapping or exception.

    Raises :class:`MappingCorruptError` or
    :class:`cryptography.exceptions.InvalidTag` exactly as
    :func:`load_mapping` does; callers that want friendly translation
    catch these and re-raise.
    """
    return load_mapping(master, path)


def _run_one(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    path: Path,
    preprocessor: Preprocessor,
    master: SecretStr,
    detectors: list[Detector],
    mapping: Mapping,
    sequential_tokens: bool,
) -> PseudonymizeResult:
    """Read *path* and route through the right pipeline (text vs. binary)."""
    if isinstance(preprocessor, MsgPreprocessor):
        if not path.is_file():
            raise FileNotFoundError(f"{path}: not a regular file (refusing to read)")
        return pseudonymize_msg_file(
            path,
            master,
            detectors,
            mapping=mapping,
            sequential_tokens=sequential_tokens,
        )

    text = path.read_text(encoding="utf-8")
    return pseudonymize_text(
        text,
        master,
        detectors,
        mapping=mapping,
        sequential_tokens=sequential_tokens,
        preprocessor=preprocessor,
    )


# Re-export for backwards compatibility with callers that imported these
# helpers from ``kuckuck.runner`` before the public API was nailed down.
__all__ = [
    "MappingCorruptError",
    "_FORMAT_BY_SUFFIX",
    "_PREPROCESSORS",
    "run_pseudonymize",
    "select_preprocessor",
]
