"""Integration tests for the FastMCP server in src/kuckuck_mcp/.

Uses FastMCP's in-process Client + FastMCPTransport pattern documented
at https://gofastmcp.com/servers/testing.md so we exercise the actual
tool-dispatch path (decorator wrapping, JSON-Schema validation,
elicitation routing) instead of calling the underlying functions
directly. Without this the elicitation logic is essentially untested.

Skipped when ``fastmcp`` is not importable (the package lives in the
optional ``[mcp]`` extra and the core install must remain MCP-free).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, TypeAlias, Union

import pytest

from kuckuck import RunOptions, run_pseudonymize

_skip_mcp = False
try:
    from fastmcp import Client
    from fastmcp.client.elicitation import ElicitResult
    from fastmcp.client.transports import FastMCPTransport
    from fastmcp.exceptions import ToolError
    from mcp.shared.context import RequestContext
    from mcp.types import ElicitRequestFormParams, ElicitRequestURLParams

    from kuckuck_mcp.server import build_server

    # PEP 613 type aliases. mypy --strict needs the TypeAlias hint to
    # treat these as types instead of variable assignments.
    # Client is parameterised on the transport class; in-process tests
    # always use FastMCPTransport. RequestContext is generic over
    # (SessionT, LifespanContextT, RequestT); Any is fine because the
    # elicitation handlers do not inspect these fields.
    KuckuckClient: TypeAlias = Client[FastMCPTransport]
    ElicitParams: TypeAlias = Union[ElicitRequestURLParams, ElicitRequestFormParams]
    ElicitContext: TypeAlias = RequestContext[Any, Any, Any]
    ConsentResponse: TypeAlias = type
except ImportError:  # pragma: no cover - covered by the skip marker
    _skip_mcp = True

pytestmark = pytest.mark.skipif(_skip_mcp, reason="fastmcp not installed (kuckuck[mcp] extra)")


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    """Create a Kuckuck master-key file and point the lookup at it."""
    path = tmp_path / "test.kuckuck-key"
    path.write_text(
        "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _isolate_key_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key_file: Path,
) -> None:
    """Force load_key to find our test key, ignoring whatever the dev box has.

    The MCP server calls :func:`kuckuck.config.load_key(None)` which traverses
    ``KUCKUCK_KEY_FILE`` env var, $PWD/.kuckuck-key, ~/.config/kuckuck/key.
    Pointing the env var at the fixture key keeps every test deterministic.
    """
    monkeypatch.setenv("KUCKUCK_KEY_FILE", str(key_file))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
async def mcp_client() -> AsyncIterator[KuckuckClient]:
    """Spawn an in-process Client against the kuckuck MCP server."""
    server = build_server()
    async with Client(transport=server) as client:
        yield client


async def _accept_yes_handler(  # pylint: disable=unused-argument
    message: str,
    response_type: "ConsentResponse",
    params: "ElicitParams",
    context: "ElicitContext",
) -> object:
    """Elicitation handler that always accepts with the literal 'yes'.

    response_type is a generated dataclass with a single 'value' field
    because the server passed Literal["yes","no"] (per FastMCP's
    primitive-wrapping rule).
    """
    return response_type(value="yes")


async def _accept_no_handler(  # pylint: disable=unused-argument
    message: str,
    response_type: "ConsentResponse",
    params: "ElicitParams",
    context: "ElicitContext",
) -> object:
    """Elicitation handler that 'accepts' but answers 'no' instead of 'yes'."""
    return response_type(value="no")


async def _decline_handler(  # pylint: disable=unused-argument
    message: str,
    response_type: "ConsentResponse",
    params: "ElicitParams",
    context: "ElicitContext",
) -> ElicitResult:
    """Elicitation handler that returns the explicit Declined action."""
    return ElicitResult(action="decline")


async def _cancel_handler(  # pylint: disable=unused-argument
    message: str,
    response_type: "ConsentResponse",
    params: "ElicitParams",
    context: "ElicitContext",
) -> ElicitResult:
    """Elicitation handler that returns the explicit Cancelled action."""
    return ElicitResult(action="cancel")


class TestServerSetup:
    async def test_all_four_tools_are_registered(self, mcp_client: KuckuckClient) -> None:
        # Sanity check via the in-process Client: list_tools() returns the
        # registered MCP tools so we know the decorator wiring took.
        tools = await mcp_client.list_tools()
        names = {tool.name for tool in tools}
        expected = {
            "kuckuck_pseudonymize",
            "kuckuck_restore",
            "kuckuck_list_detectors",
            "kuckuck_status",
        }
        assert expected.issubset(names)

    async def test_server_carries_kuckuck_instructions(self) -> None:
        server = build_server()
        assert server.instructions is not None
        assert "kuckuck_pseudonymize" in server.instructions
        assert "kuckuck_restore" in server.instructions


class TestPromptDiscoverability:
    async def test_three_prompts_are_registered(self, mcp_client: KuckuckClient) -> None:
        # Prompts surface as quick-actions in MCP clients (Claude Desktop /
        # Code show them in the slash-command picker). They are the
        # discoverability layer on top of the tools.
        prompts = await mcp_client.list_prompts()
        names = {p.name for p in prompts}
        expected = {
            "pseudonymize_before_reading",
            "diagnose_kuckuck_setup",
            "explain_kuckuck_tokens",
        }
        assert expected.issubset(names)

    async def test_pseudonymize_prompt_renders_with_file_path(
        self, mcp_client: KuckuckClient, tmp_path: Path
    ) -> None:
        result = await mcp_client.get_prompt(
            "pseudonymize_before_reading",
            arguments={"file_path": str(tmp_path / "foo.eml")},
        )
        # The prompt result holds a list of MCP messages; the rendered
        # text mentions the file path AND the safe sequence (pseudonymize
        # first, read second).
        rendered = " ".join(str(m.content) for m in result.messages)
        assert "foo.eml" in rendered
        assert "kuckuck_pseudonymize" in rendered
        assert "Step" in rendered

    async def test_diagnose_prompt_references_status_tool(
        self, mcp_client: KuckuckClient
    ) -> None:
        result = await mcp_client.get_prompt("diagnose_kuckuck_setup")
        rendered = " ".join(str(m.content) for m in result.messages)
        assert "kuckuck_status" in rendered
        assert "problems" in rendered

    async def test_explain_prompt_covers_token_types(
        self, mcp_client: KuckuckClient
    ) -> None:
        result = await mcp_client.get_prompt("explain_kuckuck_tokens")
        rendered = " ".join(str(m.content) for m in result.messages)
        # Must explain at least the four user-visible token prefixes.
        for prefix in ("EMAIL", "PHONE", "HANDLE", "PERSON"):
            assert prefix in rendered
        # Must point at the local restore path (we never auto-restore).
        assert "kuckuck restore" in rendered


class TestPseudonymizeTool:
    async def test_pseudonymize_returns_status_line(
        self, mcp_client: KuckuckClient, tmp_path: Path
    ) -> None:
        source = tmp_path / "doc.txt"
        source.write_text("Kontakt max@firma.de", encoding="utf-8")
        result = await mcp_client.call_tool(
            "kuckuck_pseudonymize", arguments={"file_path": str(source)}
        )
        assert "ok" in result.data
        assert "1 replacements" in result.data
        # File got rewritten with tokens.
        assert "[[EMAIL_" in source.read_text(encoding="utf-8")
        # Sidecar was created.
        assert (tmp_path / "doc.txt.kuckuck-map.enc").is_file()

    async def test_pseudonymize_dry_run_does_not_write(
        self, mcp_client: KuckuckClient, tmp_path: Path
    ) -> None:
        source = tmp_path / "doc.txt"
        original = "Kontakt max@firma.de"
        source.write_text(original, encoding="utf-8")
        result = await mcp_client.call_tool(
            "kuckuck_pseudonymize",
            arguments={"file_path": str(source), "dry_run": True},
        )
        assert "dry-run" in result.data
        assert source.read_text(encoding="utf-8") == original

    async def test_pseudonymize_format_eml_keeps_headers(
        self, mcp_client: KuckuckClient, tmp_path: Path
    ) -> None:
        source = tmp_path / "msg.eml"
        source.write_text(
            "From: a@example.com\nSubject: hi\n\nBody max@firma.de\n",
            encoding="utf-8",
        )
        await mcp_client.call_tool(
            "kuckuck_pseudonymize",
            arguments={"file_path": str(source), "format": "eml"},
        )
        out = source.read_text(encoding="utf-8")
        # Body is tokenized.
        assert "[[EMAIL_" in out
        # Headers stay intact (a@example.com is in the From header, not the body).
        assert "a@example.com" in out

    async def test_pseudonymize_missing_file_raises_tool_error(
        self, mcp_client: KuckuckClient, tmp_path: Path
    ) -> None:
        with pytest.raises(ToolError, match="not a regular file"):
            await mcp_client.call_tool(
                "kuckuck_pseudonymize",
                arguments={"file_path": str(tmp_path / "nonexistent.txt")},
            )

    async def test_pseudonymize_invalid_format_raises_tool_error(
        self, mcp_client: KuckuckClient, tmp_path: Path
    ) -> None:
        # The Literal type annotation makes FastMCP reject the call before
        # it reaches our code; the error surfaces as a ToolError. We catch
        # the precise exception class instead of plain Exception so an
        # unrelated AssertionError would not silently make this pass.
        source = tmp_path / "doc.txt"
        source.write_text("hi", encoding="utf-8")
        with pytest.raises(ToolError):
            await mcp_client.call_tool(
                "kuckuck_pseudonymize",
                arguments={"file_path": str(source), "format": "weirdo"},
            )

    async def test_pseudonymize_refuses_path_outside_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Path-confinement: the model could try to mutate any file on the
        # filesystem; the server must hard-fail when file_path escapes
        # KUCKUCK_MCP_ALLOWED_ROOTS (default $PWD at server start).
        target = tmp_path / "safe-zone"
        target.mkdir()
        monkeypatch.setenv("KUCKUCK_MCP_ALLOWED_ROOTS", str(target))
        outside = tmp_path / "out_of_bounds.txt"
        outside.write_text("Hi max@firma.de", encoding="utf-8")
        # Need a fresh server so the env var is observed.
        from kuckuck_mcp.server import build_server as _build

        server = _build()
        async with Client(transport=server) as client:
            with pytest.raises(ToolError, match="outside the allowed workspace"):
                await client.call_tool(
                    "kuckuck_pseudonymize",
                    arguments={"file_path": str(outside)},
                )
        # Original was NOT modified.
        assert outside.read_text(encoding="utf-8") == "Hi max@firma.de"


class TestRestoreToolElicitation:
    @staticmethod
    def _setup_pseudonymized(tmp_path: Path) -> Path:
        """Helper: create a file, pseudonymize it (so a sidecar exists)."""
        source = tmp_path / "doc.txt"
        source.write_text("Hi max@firma.de und cc @eva", encoding="utf-8")
        run_pseudonymize([source], RunOptions())
        return source

    async def test_restore_with_yes_returns_cleartext(self, tmp_path: Path) -> None:
        source = self._setup_pseudonymized(tmp_path)
        server = build_server()
        async with Client(transport=server, elicitation_handler=_accept_yes_handler) as client:
            result = await client.call_tool("kuckuck_restore", arguments={"file_path": str(source)})
        # We get back the original cleartext.
        assert "max@firma.de" in result.data
        assert "@eva" in result.data
        assert "[[EMAIL_" not in result.data

    async def test_restore_with_no_returns_cancellation(self, tmp_path: Path) -> None:
        source = self._setup_pseudonymized(tmp_path)
        server = build_server()
        async with Client(transport=server, elicitation_handler=_accept_no_handler) as client:
            result = await client.call_tool("kuckuck_restore", arguments={"file_path": str(source)})
        assert "cancelled" in result.data
        # No cleartext leaked.
        assert "max@firma.de" not in result.data

    async def test_restore_with_decline_returns_cancellation(self, tmp_path: Path) -> None:
        source = self._setup_pseudonymized(tmp_path)
        server = build_server()
        async with Client(transport=server, elicitation_handler=_decline_handler) as client:
            result = await client.call_tool("kuckuck_restore", arguments={"file_path": str(source)})
        assert "cancelled" in result.data
        assert "declined" in result.data
        assert "max@firma.de" not in result.data

    async def test_restore_with_cancel_returns_cancellation(self, tmp_path: Path) -> None:
        source = self._setup_pseudonymized(tmp_path)
        server = build_server()
        async with Client(transport=server, elicitation_handler=_cancel_handler) as client:
            result = await client.call_tool("kuckuck_restore", arguments={"file_path": str(source)})
        assert "cancelled" in result.data
        assert "max@firma.de" not in result.data

    async def test_restore_missing_sidecar_raises(
        self, mcp_client: KuckuckClient, tmp_path: Path
    ) -> None:
        source = tmp_path / "no-sidecar.txt"
        source.write_text("plain text", encoding="utf-8")
        with pytest.raises(ToolError, match="missing mapping sidecar"):
            await mcp_client.call_tool("kuckuck_restore", arguments={"file_path": str(source)})


class TestListDetectorsTool:
    async def test_returns_known_detectors(self, mcp_client: KuckuckClient) -> None:
        result = await mcp_client.call_tool("kuckuck_list_detectors")
        # FastMCP deserialises the list of pydantic models back into
        # objects with attribute access on the client side.
        names = {d.name for d in result.data}
        assert {"email", "phone", "handle"}.issubset(names)
        for entry in result.data:
            assert isinstance(entry.priority, int)
            assert entry.entity_type


class TestStatusTool:
    async def test_status_reports_key_found(self, mcp_client: KuckuckClient) -> None:
        result = await mcp_client.call_tool("kuckuck_status")
        assert result.data.key_found is True
        assert result.data.key_error == ""
        assert result.data.model_path
        # gliner is optional; just check the field type.
        assert isinstance(result.data.gliner_installed, bool)
        assert isinstance(result.data.model_available, bool)

    async def test_status_reports_missing_key_with_remediation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Need a fresh client (not the mcp_client fixture) because the env
        # mutation has to happen before the server reads its config.
        monkeypatch.setenv("KUCKUCK_KEY_FILE", str(tmp_path / "nope"))
        server = build_server()
        async with Client(transport=server) as client:
            result = await client.call_tool("kuckuck_status")
        assert result.data.key_found is False
        assert result.data.key_error != ""
        # The aggregated 'problems' field must mention the missing key
        # AND the remediation hint, so a model can surface it verbatim.
        problems_text = " ".join(result.data.problems)
        assert "master key" in problems_text.lower()
        assert "KUCKUCK_KEY_FILE" in problems_text or "kuckuck init-key" in problems_text
        # key_error is intentionally generic - we do not echo the absolute
        # paths from KeyNotFoundError into the model context.
        assert "/" not in result.data.key_error

    async def test_status_problems_empty_when_fully_operational(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force gliner+model to look present so the problems list collapses
        # to []. This locks the contract "problems == [] iff fully usable".
        monkeypatch.setattr("kuckuck_mcp.server.is_gliner_installed", lambda: True)
        monkeypatch.setattr("kuckuck_mcp.server.is_model_available", lambda: True)
        # Rebuild the server so the patched lookups are seen.
        server = build_server()
        async with Client(transport=server) as client:
            result = await client.call_tool("kuckuck_status")
        assert result.data.problems == []
