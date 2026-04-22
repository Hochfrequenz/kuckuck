"""Kuckuck — lokale Pseudonymisierung personenbezogener Daten.

The package exposes a small, stable surface:

* :func:`pseudonymize_text` and :func:`restore_text` for programmatic use.
* :func:`run_pseudonymize` plus :class:`RunOptions` for batch file runs
  (the same code path the CLI uses).
* :class:`Mapping` / :func:`load_mapping` / :func:`save_mapping` for the
  encrypted sidecar file.
* :func:`load_key` / :func:`load_default_key` for the master-secret lookup.
* :class:`KuckuckSettings` for ``pydantic-settings``-style configuration.

See the module-level docstrings for implementation notes.
"""

from kuckuck.config import DEFAULT_KEY_PATH, KuckuckSettings, load_default_key, load_key
from kuckuck.mapping import Mapping, load_mapping, save_mapping
from kuckuck.options import RunOptions
from kuckuck.pseudonymize import (
    PseudonymizeResult,
    build_default_detectors,
    pseudonymize_msg_file,
    pseudonymize_text,
    restore_text,
)

# run_pseudonymize lives in kuckuck.__main__ alongside the typer plumbing
# because the CLI and library API share the same per-file orchestration.
# Re-exporting here keeps "from kuckuck import run_pseudonymize" working
# without requiring callers to know the internal layout.
from kuckuck.__main__ import run_pseudonymize  # noqa: E402  pylint: disable=wrong-import-position

__all__ = [
    "DEFAULT_KEY_PATH",
    "KuckuckSettings",
    "Mapping",
    "PseudonymizeResult",
    "RunOptions",
    "build_default_detectors",
    "load_default_key",
    "load_key",
    "load_mapping",
    "pseudonymize_msg_file",
    "pseudonymize_text",
    "restore_text",
    "run_pseudonymize",
    "save_mapping",
]
