"""FastMCP server exposing Kuckuck as four tools over MCP stdio.

Tools:

* ``kuckuck_pseudonymize`` -- wraps :func:`kuckuck.run_pseudonymize` for one
  file at a time, returns a short status line. No PII flows through this
  tool: cleartext goes in (the client provides a path), tokens come out.
* ``kuckuck_restore`` -- the only tool that can leak cleartext PII into the
  model context. Gated behind a FastMCP elicitation: the user must
  explicitly accept a "yes" / "no" prompt on the client side before the
  restored text is returned.
* ``kuckuck_list_detectors`` -- metadata only, lists active detectors with
  their priority and entity-type.
* ``kuckuck_status`` -- health-check: master key present? GLiNER installed?
  Model snapshot on disk? Useful for the model to introspect why
  ``--ner`` would fail before invoking pseudonymize.

Architectural notes:

* stdio transport only. HTTP / daemon-mode is an explicit non-goal (see
  Issue #10 decision 6); a server-mode binary brings auth / multi-tenant
  / logging concerns that are out of scope for the local-only Kuckuck
  promise.
* No MCP resources are exposed. The mapping itself stays accessible only
  through ``kuckuck inspect`` locally; exposing it as a resource would
  shift the security boundary from "cryptographic" to "user clicks
  permission prompt right" (decision 3).
* Console script ``kuckuck-mcp`` (registered in ``pyproject.toml``) calls
  :func:`main`, which boots the FastMCP stdio server.

Before editing this file, read the relevant FastMCP doc pages
(https://gofastmcp.com/llms.txt). The repo's AGENTS.md elaborates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
)
from pydantic import BaseModel, Field

from kuckuck.config import KeyNotFoundError, load_key
from kuckuck.detectors.ner import (
    NerModelMissingError,
    NerNotInstalledError,
    default_model_path,
    is_gliner_installed,
    is_model_available,
)
from kuckuck.mapping import load_mapping
from kuckuck.options import RunOptions
from kuckuck.pseudonymize import build_default_detectors, restore_text
from kuckuck.runner import run_pseudonymize


class DetectorInfo(BaseModel):
    """One entry in the ``kuckuck_list_detectors`` response.

    Pydantic over TypedDict because FastMCP renders pydantic models
    natively into the MCP tool-result JSON-Schema (clients see typed
    fields with descriptions, not opaque dicts) and validates the
    server's outputs on the way out.
    """

    name: str = Field(description="Internal detector name (email, phone, handle, term, ner).")
    priority: int = Field(description="Higher wins on overlapping spans during resolution.")
    entity_type: str = Field(description="Token prefix that ends up in the pseudonymized output.")


class StatusInfo(BaseModel):
    """Self-diagnostic response for ``kuckuck_status``.

    The ``problems`` list aggregates every misconfiguration the server
    can detect (missing key, missing gliner, missing model) so the
    client/model gets a complete punch-list in one round-trip rather
    than one item at a time. Pattern adopted from
    Hochfrequenz/sap-mcp-config which uses the same aggregation style.
    """

    key_found: bool = Field(description="True when the master-key lookup chain found a key file.")
    key_error: str = Field(default="", description="Empty when key_found is True; otherwise the lookup error.")
    gliner_installed: bool = Field(description="True when the optional gliner package is importable.")
    model_available: bool = Field(description="True when the GLiNER model snapshot is on disk.")
    model_path: str = Field(description="Resolved path where the GLiNER model is expected.")
    problems: list[str] = Field(
        default_factory=list,
        description=(
            "Human-readable list of every detected problem with a remediation hint. "
            "Empty list means the server is fully operational (NER plus regex). "
            "When this is non-empty, the message is safe to surface verbatim to the user."
        ),
    )


def build_server() -> FastMCP:
    """Construct and return the FastMCP server with all tools registered.

    Split out of :func:`main` so tests can spawn an in-process Client
    against the returned server (via ``FastMCPTransport``) and call its
    tools directly without going through stdio.
    """
    mcp: FastMCP = FastMCP(
        name="kuckuck",
        instructions=(
            "Local-only PII pseudonymization. Use kuckuck_pseudonymize on any "
            ".eml / .msg / .md / .xml / text file BEFORE you read its contents. "
            "kuckuck_restore returns cleartext PII to the client and triggers a "
            "user-confirmation elicitation - do not call it for inspection, only "
            "in deliberate restore workflows."
        ),
    )

    @mcp.tool
    async def kuckuck_pseudonymize(
        file_path: str,
        format: Literal["auto", "text", "eml", "msg", "md", "xml"] = "auto",
        ner: bool = False,
        dry_run: bool = False,
    ) -> str:
        """Pseudonymize a file in place; write the encrypted mapping sidecar next to it.

        Returns a short status line with the replacement count and the
        format that was applied. The cleartext content of the file does NOT
        flow back through this tool - only metadata.

        Set ``ner=True`` to enable the GLiNER PERSON detector (requires
        the ``kuckuck[ner]`` extra and a model fetched via the CLI command
        ``kuckuck fetch-model``). Use ``dry_run=True`` to compute the result
        without writing anything to disk.
        """
        path = Path(file_path)
        if not path.is_file():
            raise ToolError(f"{file_path}: not a regular file")
        try:
            results = run_pseudonymize(
                [path],
                RunOptions(format=format, ner=ner, dry_run=dry_run),
            )
        except KeyNotFoundError as exc:
            raise ToolError(
                f"Master key not found ({exc}). "
                "Fix: set KUCKUCK_KEY_FILE in your MCP client config to an absolute key-file path, "
                "or call kuckuck_status to see the full lookup chain."
            ) from exc
        except NerNotInstalledError as exc:
            raise ToolError(
                f"NER requested but the 'gliner' package is not installed ({exc}). "
                "Fix: pip install 'kuckuck[ner]', then restart the MCP client so the server picks up the new dependency."
            ) from exc
        except NerModelMissingError as exc:
            raise ToolError(
                f"NER requested but the model snapshot is not on disk ({exc}). "
                "Fix: run 'kuckuck fetch-model' once in a shell to download it (~ 1 GB)."
            ) from exc
        except (OSError, ValueError) as exc:
            raise ToolError(f"pseudonymize failed: {exc}") from exc
        result = results[0]
        suffix = " (dry-run, nothing written)" if dry_run else ""
        return f"ok: {file_path} -> {len(result.replaced)} replacements{suffix}"

    @mcp.tool
    async def kuckuck_restore(file_path: str, ctx: Context) -> str:
        """Restore the cleartext content of a pseudonymized file.

        WARNING: this tool returns the original PII (names, emails, phone
        numbers) to the MCP client and the AI model behind it. The user
        is asked for explicit consent via FastMCP elicitation before any
        cleartext leaves the server.

        Returns the restored content as a string, or a short cancellation
        message if the user declined.
        """
        path = Path(file_path)
        if not path.is_file():
            raise ToolError(f"{file_path}: not a regular file")
        sidecar = path.with_suffix(path.suffix + ".kuckuck-map.enc")
        if not sidecar.is_file():
            raise ToolError(f"missing mapping sidecar: {sidecar}")

        consent = await ctx.elicit(
            message=(
                f"kuckuck_restore will return the CLEARTEXT contents of {file_path} "
                "to the MCP client. The AI model in your client will see the original "
                "names, emails, phone numbers and other PII that the mapping resolves. "
                "Continue?"
            ),
            response_type=Literal["yes", "no"],
        )
        # The match-statement is the canonical FastMCP elicitation pattern;
        # see https://gofastmcp.com/servers/elicitation.md
        match consent:
            case AcceptedElicitation(data="yes"):
                pass  # fall through to the restore work below
            case AcceptedElicitation(data=other):
                return f"cancelled: user explicitly answered {other!r}"
            case DeclinedElicitation():
                return "cancelled: user declined the cleartext disclosure"
            case CancelledElicitation():
                return "cancelled: elicitation was cancelled"

        try:
            master = load_key(None)
        except KeyNotFoundError as exc:
            raise ToolError(f"master key not found: {exc}") from exc
        mapping = load_mapping(master, sidecar)
        text = path.read_text(encoding="utf-8")
        return restore_text(text, mapping)

    @mcp.tool
    def kuckuck_list_detectors() -> list[DetectorInfo]:
        """List the active built-in detectors with name, priority, and entity type.

        Useful for the model to know which token prefixes (EMAIL, PHONE,
        HANDLE, TERM, PERSON) it can expect in pseudonymized text. NER
        is only listed when both gliner and the model snapshot are available.
        """
        detectors = build_default_detectors(denylist=["__placeholder__"])
        if is_gliner_installed() and is_model_available():
            from kuckuck.detectors.ner import NerDetector  # pylint: disable=import-outside-toplevel

            detectors.append(NerDetector())
        return [
            DetectorInfo(name=d.name, priority=d.priority, entity_type=d.entity_type.value)
            for d in detectors
        ]

    @mcp.prompt(
        name="pseudonymize_before_reading",
        description=(
            "Walks the model through the safe sequence: pseudonymize a sensitive "
            "file via Kuckuck FIRST, then read the result. Use this when the user "
            "drops a .eml / .msg / .md / .xml or any file that may contain PII."
        ),
        tags={"kuckuck", "workflow", "safety"},
    )
    def pseudonymize_before_reading(file_path: str) -> str:
        """Quick-action prompt: pseudonymize then read.

        Surfaced as a template in MCP clients. Picking it sets the model
        up to call ``kuckuck_pseudonymize(file_path=...)`` first and only
        then read the file - the canonical PII-safe sequence.
        """
        return (
            f"The user wants help with the file at {file_path}, which may contain "
            "personally identifiable data (names, emails, phones).\n\n"
            f"Step 1: call kuckuck_pseudonymize(file_path={file_path!r}) to replace "
            "PII with stable [[EMAIL_xxx]] / [[PHONE_xxx]] / [[HANDLE_xxx]] / "
            "[[PERSON_xxx]] tokens.\n"
            "Step 2: read the file with your normal Read tool. You will only see "
            "tokens, not the original PII.\n"
            "Step 3: do whatever the user asked you to do. Use the same tokens "
            "verbatim in your answer.\n"
            "Step 4: when the user wants the cleartext back, they run "
            "'kuckuck restore <file>' locally - do NOT call kuckuck_restore unless "
            "they explicitly ask for it (it triggers a user-confirmation prompt)."
        )

    @mcp.prompt(
        name="diagnose_kuckuck_setup",
        description=(
            "Calls kuckuck_status, surfaces every detected configuration problem "
            "and the matching remediation step. Use this when a kuckuck_* call "
            "errored out or when the user asks 'is kuckuck set up correctly?'"
        ),
        tags={"kuckuck", "troubleshooting", "setup"},
    )
    def diagnose_kuckuck_setup() -> str:
        """Quick-action prompt: run a self-diagnostic and explain results."""
        return (
            "Call kuckuck_status, then format the response as follows:\n\n"
            "1. Print the boolean fields (key_found, gliner_installed, "
            "model_available) as a short status table.\n"
            "2. If the 'problems' list is empty, say 'Kuckuck is fully operational.'\n"
            "3. If 'problems' is non-empty, list each entry as a bullet with a "
            "clear remediation step. Do NOT abbreviate the messages - they "
            "already contain the exact commands the user needs to run."
        )

    @mcp.prompt(
        name="explain_kuckuck_tokens",
        description=(
            "Explains what the [[EMAIL_xxx]] / [[PERSON_xxx]] / [[HANDLE_xxx]] etc. "
            "tokens in a pseudonymized file mean and how the user can restore the "
            "originals. Use when the model encounters tokens and the user asks "
            "what they are."
        ),
        tags={"kuckuck", "explanation"},
    )
    def explain_kuckuck_tokens() -> str:
        """Quick-action prompt: explain Kuckuck tokens."""
        return (
            "Explain to the user that:\n\n"
            "- Tokens like [[EMAIL_a7f3b2c1]] are produced by Kuckuck "
            "(https://github.com/Hochfrequenz/kuckuck) when a file gets "
            "pseudonymized.\n"
            "- The same original value always gets the same token, even across "
            "documents and across team members who share a Master-Key.\n"
            "- Token prefixes: EMAIL, PHONE, HANDLE (Jira/Confluence mention), "
            "TERM (denylist entry like a customer name), PERSON (NER-detected "
            "person name; only when --ner is enabled).\n"
            "- The mapping back to cleartext is in the AES-GCM-encrypted "
            "*.kuckuck-map.enc sidecar next to the file. Without the Master-Key, "
            "the mapping is cryptographically unreadable.\n"
            "- To restore: 'kuckuck restore <file>' in the user's shell, locally. "
            "The model should NOT auto-restore (that bypasses the user's intent)."
        )

    @mcp.tool
    def kuckuck_status() -> StatusInfo:
        """Self-diagnostic: which Kuckuck capabilities are available right now.

        Reports key availability, GLiNER installation, model presence, and
        the resolved cache directory. The model can call this before
        attempting ``ner=True`` to give the user a precise reason if NER
        is unavailable.

        ``problems`` aggregates every missing piece with a remediation
        hint, so the model can present one combined "here is what to fix"
        message instead of asking the user to triage individual booleans.
        """
        try:
            load_key(None)
            key_found = True
            key_error = ""
        except KeyNotFoundError as exc:
            key_found = False
            key_error = str(exc)
        gliner_ok = is_gliner_installed()
        model_ok = is_model_available()

        problems: list[str] = []
        if not key_found:
            problems.append(
                "No master key found. "
                "Set KUCKUCK_KEY_FILE in your MCP client config to an absolute path, "
                "or run 'kuckuck init-key' once to create ~/.config/kuckuck/key. "
                f"Lookup error: {key_error}"
            )
        if not gliner_ok:
            problems.append(
                "Optional NER detector unavailable: the 'gliner' package is not installed. "
                "Run: pip install 'kuckuck[ner]'"
            )
        if gliner_ok and not model_ok:
            problems.append(
                f"NER model snapshot missing at {default_model_path()}. "
                "Run: kuckuck fetch-model"
            )

        return StatusInfo(
            key_found=key_found,
            key_error=key_error,
            gliner_installed=gliner_ok,
            model_available=model_ok,
            model_path=str(default_model_path()),
            problems=problems,
        )

    return mcp


def main() -> None:
    """Console-script entry point: boot the FastMCP stdio server."""
    server = build_server()
    server.run()


if __name__ == "__main__":  # pragma: no cover - module is invoked via console_script
    main()
