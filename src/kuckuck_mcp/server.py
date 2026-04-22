"""FastMCP server exposing Kuckuck as five tools and four prompts over MCP stdio.

Tools:

* ``kuckuck_pseudonymize`` -- wraps :func:`kuckuck.run_pseudonymize` for one
  file at a time, returns a short status line. No PII flows through this
  tool: cleartext goes in (the client provides a path), tokens come out.
  Default ``ner=auto`` uses GLiNER PERSON detection when both the
  ``[ner]`` extra and the model snapshot are available, falls back to
  regex-only otherwise.
* ``kuckuck_restore`` -- the only tool that can leak cleartext PII into the
  model context. Gated behind a FastMCP elicitation: the user must
  explicitly accept a "yes" / "no" prompt on the client side before the
  restored text is returned.
* ``kuckuck_fetch_model`` -- one-time downloader for the ~ 1.1 GB GLiNER
  snapshot. Gated behind an elicitation so a multi-GB transfer never
  starts silently. Required for PERSON detection in ``ner=auto``.
* ``kuckuck_list_detectors`` -- metadata only, lists active detectors with
  their priority and entity-type.
* ``kuckuck_status`` -- health-check: master key present? GLiNER installed?
  Model snapshot on disk? Returns an aggregated ``problems`` list with
  remediation hints (pattern from Hochfrequenz/sap-mcp-config).

Prompts (quick-actions surfaced in the MCP-client slash menu):

* ``setup_kuckuck`` -- first-time setup walkthrough: key, [ner] extra,
  model download. Use when a fresh user asks "how do I start?".
* ``pseudonymize_before_reading`` -- safe sequence "pseudonymize first,
  then read" for a given file_path.
* ``diagnose_kuckuck_setup`` -- runs kuckuck_status and formats the
  remediation steps for the user.
* ``explain_kuckuck_tokens`` -- explains the [[EMAIL_xxx]] / [[PERSON_xxx]]
  tokens and how to restore them locally.

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

import os
from pathlib import Path
from typing import Literal, TypeAlias

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
    DEFAULT_MODEL_ID,
    NerModelMissingError,
    NerNotInstalledError,
    default_cache_root,
    default_model_path,
    is_gliner_installed,
    is_model_available,
)
from kuckuck.mapping import load_mapping
from kuckuck.options import RunOptions
from kuckuck.pseudonymize import build_default_detectors, restore_text
from kuckuck.runner import run_pseudonymize

#: Format selector accepted by ``kuckuck_pseudonymize``. Mirrors the
#: ``--format`` choices of the ``kuckuck run`` CLI. Pulled out of the tool
#: signature so the literal lives in one place and FastMCP renders the
#: same enum into the JSON-Schema regardless of where it gets reused.
FormatChoice: TypeAlias = Literal["auto", "text", "eml", "msg", "md", "xml"]

#: Env var that lists colon-separated allowed roots for file_path arguments.
#: Default (when unset): only $PWD at server-start time is allowed. Setting
#: this lets the operator widen the workspace explicitly, e.g.
#: ``KUCKUCK_MCP_ALLOWED_ROOTS=/home/me/work:/home/me/inbox``. Set to ``*``
#: to disable confinement entirely (NOT recommended for shared MCP clients).
_ALLOWED_ROOTS_ENV = "KUCKUCK_MCP_ALLOWED_ROOTS"


def _allowed_roots() -> list[Path] | None:
    """Return the resolved allowed-root paths, or None to disable confinement.

    A return value of ``None`` only happens when the operator explicitly sets
    the env var to ``*`` - the default uses ``$PWD`` as the single root.
    """
    raw = os.environ.get(_ALLOWED_ROOTS_ENV, "").strip()
    if raw == "*":
        return None
    if not raw:
        return [Path.cwd().resolve()]
    return [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p]


def _ensure_path_in_workspace(file_path: str) -> Path:
    """Reject path arguments that escape the allowed workspace roots.

    Without this check, a model could call
    ``kuckuck_pseudonymize(file_path="/etc/passwd")`` and Kuckuck would
    happily overwrite the file in place. By default the workspace is
    just ``$PWD`` at server-start; operators widen it via
    KUCKUCK_MCP_ALLOWED_ROOTS.
    """
    candidate = Path(file_path).expanduser().resolve()
    roots = _allowed_roots()
    if roots is None:
        return candidate
    for root in roots:
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue
    raise ToolError(
        f"Refusing to operate on {file_path}: path is outside the allowed "
        f"workspace roots ({', '.join(str(r) for r in roots)}). "
        f"To widen the workspace, set the {_ALLOWED_ROOTS_ENV} environment "
        "variable in your MCP client config (colon-separated absolute paths, "
        "or '*' to disable confinement entirely)."
    )


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
            "By default it auto-enables NER (PERSON-name detection) when "
            "available, falling back to regex-only otherwise. "
            "If a user just installed this server and asks how to start, run "
            "the setup_kuckuck prompt - it walks them through key creation, "
            "the [ner] extra and (optionally) calling kuckuck_fetch_model to "
            "download the GLiNER model for best-effort PERSON detection. "
            "kuckuck_restore returns cleartext PII to the client and triggers a "
            "user-confirmation elicitation - do not call it for inspection, only "
            "in deliberate restore workflows."
        ),
    )

    @mcp.tool(
        annotations={
            "title": "Pseudonymize a file (destructive, in-place)",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def kuckuck_pseudonymize(
        file_path: str,
        format: FormatChoice = "auto",
        ner: bool | None = None,
        dry_run: bool = False,
    ) -> str:
        """Pseudonymize a file in place; write the encrypted mapping sidecar next to it.

        WARNING: this tool MODIFIES the file at *file_path* unless
        ``dry_run=True``. It writes a *.kuckuck-map.enc sidecar next to
        the file. Path arguments are confined to the workspace roots
        listed in KUCKUCK_MCP_ALLOWED_ROOTS (default: $PWD at server
        start) so the model cannot accidentally rewrite arbitrary
        files like /etc/hosts.

        Returns a short status line with the replacement count and the
        format that was applied. The cleartext content of the file does NOT
        flow back through this tool - only metadata.

        ``ner`` controls the GLiNER PERSON detector:

        * ``None`` (default) -- auto: enable NER when both the gliner
          package and the model snapshot are available, fall back to
          regex-only otherwise. This gives best-effort results out of
          the box without crashing on systems that didn't install the
          [ner] extra.
        * ``True`` -- explicit opt-in. Raises a ToolError when gliner or
          the model is missing (use this when downstream behaviour
          depends on PERSON tokens).
        * ``False`` -- explicit opt-out, regex detectors only.

        ``dry_run=True`` computes the result without writing anything.
        """
        path = _ensure_path_in_workspace(file_path)
        if not path.is_file():
            raise ToolError(f"{file_path}: not a regular file")
        # Resolve auto-mode now so the run_pseudonymize call sees a
        # concrete bool: explicit True/False stays as-is, None becomes
        # True iff NER is fully usable on this system.
        effective_ner = ner if ner is not None else (is_gliner_installed() and is_model_available())
        try:
            results = run_pseudonymize(
                [path],
                RunOptions(format=format, ner=effective_ner, dry_run=dry_run),
            )
        except KeyNotFoundError as exc:
            # Deliberately do NOT echo {exc}: KeyNotFoundError lists the
            # absolute lookup paths (~/.config/..., /home/USER/...) which
            # leak username and filesystem layout into the model context.
            # Same discipline as kuckuck_status.key_error.
            raise ToolError(
                "Master key not found in the configured lookup chain. "
                "Fix: set KUCKUCK_KEY_FILE in your MCP client config to an absolute key-file path, "
                "or call kuckuck_status for a structured diagnosis."
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

    @mcp.tool(
        annotations={
            "title": "Restore cleartext PII (gated by user elicitation)",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    async def kuckuck_restore(file_path: str, ctx: Context) -> str:
        """Restore the cleartext content of a pseudonymized file.

        WARNING: this tool returns the original PII (names, emails, phone
        numbers) to the MCP client and the AI model behind it. The user
        is asked for explicit consent via FastMCP elicitation before any
        cleartext leaves the server.

        Returns the restored content as a string, or a short cancellation
        message if the user declined. Path arguments are confined to the
        workspace roots listed in KUCKUCK_MCP_ALLOWED_ROOTS (default:
        $PWD at server start).
        """
        path = _ensure_path_in_workspace(file_path)
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
            # Same path-leak discipline as kuckuck_pseudonymize and
            # kuckuck_status: don't echo the absolute lookup paths.
            raise ToolError(
                "Master key not found in the configured lookup chain. "
                "Fix: set KUCKUCK_KEY_FILE in your MCP client config to an absolute key-file path, "
                "or call kuckuck_status for a structured diagnosis."
            ) from exc
        except ValueError as exc:
            raise ToolError(f"master key file is malformed: {exc}") from exc
        try:
            mapping = load_mapping(master, sidecar)
        except (OSError, ValueError, RuntimeError) as exc:
            # MappingCorruptError inherits ValueError; wrong key, truncated
            # file, magic mismatch and version mismatch all surface here.
            # Without this catch they bubble as a stack trace into the model.
            raise ToolError(
                f"Mapping sidecar could not be loaded ({exc}). "
                "This usually means the master key does not match the one used to pseudonymize the file, "
                "or the .kuckuck-map.enc file is corrupt."
            ) from exc
        text = path.read_text(encoding="utf-8")
        return restore_text(text, mapping)

    @mcp.tool(
        annotations={
            "title": "List active detectors (read-only metadata)",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
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
        return [DetectorInfo(name=d.name, priority=d.priority, entity_type=d.entity_type.value) for d in detectors]

    @mcp.tool(
        annotations={
            "title": "Download GLiNER model (~1.1 GB, gated by elicitation)",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    async def kuckuck_fetch_model(ctx: Context) -> str:
        """Download the GLiNER model (~ 1.1 GB) into the local cache.

        After this call, future ``kuckuck_pseudonymize`` invocations
        with default ``ner=auto`` will detect personal names too,
        not just emails / phones / handles. Without it the server
        falls back to regex-only pseudonymization.

        The download is gated behind a FastMCP elicitation: the user
        must explicitly accept the disk-space and bandwidth cost
        before anything is fetched. Cancelled or declined elicitations
        return a short message; the server itself never starts a
        download silently.

        The download takes 5-15 minutes on a typical connection. The
        server stays responsive on other tools while it runs, but the
        client may show a long-running tool indicator.
        """
        if not is_gliner_installed():
            raise ToolError(
                "Cannot fetch the model because the optional 'gliner' package is not "
                "installed. Fix: in a shell, run `pip install 'kuckuck[ner]'` and "
                "restart the MCP client so this server picks up the new dependency."
            )
        if is_model_available():
            return (
                f"Model already present at {default_model_path()}. "
                "kuckuck_pseudonymize with ner=auto or ner=true will use it."
            )
        consent = await ctx.elicit(
            message=(
                "kuckuck_fetch_model will download the GLiNER NER model "
                f"(~ 1.1 GB) from the HuggingFace Hub into {default_model_path()}. "
                "This is a one-time setup; afterwards Kuckuck can recognise "
                "personal names locally without sending any data to a cloud LLM. "
                "The download takes 5-15 minutes. Continue?"
            ),
            response_type=Literal["yes", "no"],
        )
        match consent:
            case AcceptedElicitation(data="yes"):
                pass
            case AcceptedElicitation(data=other):
                return f"cancelled: user explicitly answered {other!r}"
            case DeclinedElicitation():
                return "cancelled: user declined the download"
            case CancelledElicitation():
                return "cancelled: elicitation was cancelled"

        try:
            # Lazy: huggingface_hub is part of the [ner] extra, not the
            # core install. By the time we get here is_gliner_installed()
            # already returned True, so the import is safe.
            # pylint: disable-next=import-outside-toplevel
            from huggingface_hub import snapshot_download  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ToolError(
                "huggingface_hub is missing. Reinstall with `pip install 'kuckuck[ner]'` "
                "to repair the install, then restart the MCP client."
            ) from exc

        target = default_cache_root() / DEFAULT_MODEL_ID.split("/")[-1]
        target.mkdir(parents=True, exist_ok=True)
        try:
            snapshot_download(repo_id=DEFAULT_MODEL_ID, local_dir=str(target))
        except (OSError, ValueError, RuntimeError) as exc:
            # Same wide-net catch as the CLI (cmd_fetch_model): every HF
            # error class inherits from one of these. Surface a short
            # message rather than a stack trace.
            raise ToolError(f"Failed to download '{DEFAULT_MODEL_ID}': {exc}") from exc
        return (
            f"ok: model downloaded to {target}. "
            "kuckuck_pseudonymize with ner=auto will now include PERSON detection."
        )

    @mcp.prompt(
        name="setup_kuckuck",
        description=(
            "First-time setup walkthrough. Calls kuckuck_status, then explains "
            "in order what the user needs to do to reach 'best results' state "
            "(key file present, gliner extra installed, model downloaded). "
            "Use when the user just installed kuckuck-mcp and asks 'how do I "
            "start?' or when kuckuck_status reports any problem."
        ),
        tags={"kuckuck", "setup", "guide"},
    )
    def setup_kuckuck() -> str:
        """Quick-action prompt: walk a non-technical user through setup."""
        return (
            "Walk the user through getting Kuckuck to its 'best results' state. "
            "Steps:\n\n"
            "1. Call kuckuck_status. Read the 'problems' list - it tells you "
            "exactly what is missing.\n"
            "2. If 'master key' is in the problems: tell the user to run "
            "`kuckuck init-key` once in any shell, then restart the MCP client. "
            "Without a key, no pseudonymization can happen.\n"
            "3. If 'gliner' is in the problems: tell the user to run "
            "`pip install 'kuckuck[ner]'` and restart the MCP client. This unlocks "
            "PERSON-name detection in addition to emails/phones/handles.\n"
            "4. If 'model' is in the problems but gliner is installed: offer to "
            "call kuckuck_fetch_model. Explain that this will download about "
            "1.1 GB of model weights and take 5-15 minutes. The user must "
            "accept a confirmation prompt before the download starts.\n"
            "5. After every fix, call kuckuck_status again to confirm 'problems' "
            "is empty. Stop when it is.\n\n"
            "Do not run kuckuck_fetch_model without first explaining the cost "
            "to the user - the elicitation prompt requires their explicit yes."
        )

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

    @mcp.tool(
        annotations={
            "title": "Self-diagnostic (read-only setup check)",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
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
        except KeyNotFoundError:
            key_found = False
            # Deliberately NOT echo the full KeyNotFoundError text here:
            # it lists the absolute lookup paths (~/.config/..., /home/USER/...)
            # which leak the operator's username and filesystem layout to the
            # model context. The remediation hint in 'problems' below tells
            # the user what to do without exposing those internals.
            key_error = "no key file in the configured lookup chain"
        except ValueError:
            # Empty / unreadable key file: load_key found a candidate but
            # rejected its content. Same path-leak discipline.
            key_found = False
            key_error = "located key file is empty or malformed"
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
                "Either run `kuckuck fetch-model` in a shell, or call the "
                "kuckuck_fetch_model MCP tool from this server (it will ask "
                "for confirmation before the ~ 1.1 GB download)."
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
