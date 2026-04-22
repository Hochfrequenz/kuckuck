"""Run-options model for the library and CLI.

The CLI hydrates a :class:`RunOptions` from typer-bound flags and hands
it to :func:`kuckuck.run_pseudonymize`, which is the single library
entry point for "process a list of files end-to-end". Library callers
can construct ``RunOptions`` directly without going through typer.

Centralising the option set in one model keeps the CLI signature short
(no more 8-argument typer functions, no more ``too-many-arguments``
pylint disables) and makes it cheap to add new flags in future PRs:
add the field on :class:`RunOptions`, expose a CLI option in
``cmd_run``, done.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class RunOptions(BaseModel):
    """Parameters for :func:`kuckuck.run_pseudonymize`.

    Mirrors the flags exposed by ``kuckuck run``. All fields have
    sensible defaults so library callers can construct
    ``RunOptions(key_file=Path("..."))`` without spelling out the rest.
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    key_file: Path | None = Field(
        default=None,
        description="Override the master-key lookup chain. None uses the configured precedence.",
    )
    output_dir: Path | None = Field(
        default=None,
        description="Write results here instead of overwriting in place.",
    )
    dry_run: bool = Field(
        default=False,
        description="Compute results without writing anything to disk.",
    )
    sequential_tokens: bool = Field(
        default=False,
        description="Use [[PERSON_1]]-style counters per document instead of HMAC fingerprints.",
    )
    denylist: Path | None = Field(
        default=None,
        description="Path to a denylist file (one entry per line).",
    )
    phone_region: str = Field(
        default="DE",
        description="Default ISO country code for parsing phone numbers without an international prefix.",
    )
    format: str = Field(
        default="auto",
        description=("Input format selector: 'auto' (decide by file suffix), 'text', 'eml', " "'msg', 'md', or 'xml'."),
    )
    ner: bool = Field(
        default=False,
        description="Enable the GLiNER PERSON detector. Requires the 'ner' extra and a fetched model.",
    )
