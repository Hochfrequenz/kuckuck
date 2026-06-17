"""Integration tests for the pseudonymizing MCP proxy in src/kuckuck_mcp/.

A dummy in-process FastMCP "backend" stands in for a real customer-data MCP
server (Jira, a REST-API wrapper, ...). We wrap it with
:func:`kuckuck_mcp.proxy.build_proxy` and drive the proxy through an in-process
:class:`fastmcp.Client`, asserting two things programmatically - no manual
testing against any production server:

1. PII a backend tool returns arrives at the client **pseudonymized** (tokens),
   never as cleartext.
2. A token the client sends as a tool argument is **restored** to the real
   value before it reaches a *trusted* backend - and is left untouched for an
   *untrusted* backend.

Skipped when ``fastmcp`` is not importable (the ``[mcp]`` optional extra).
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from pydantic import AnyUrl, BaseModel, EmailStr, SecretStr

_skip_mcp = False
try:
    import mcp.types as mt
    from fastmcp import Client, FastMCP
    from fastmcp.client.transports import FastMCPTransport
    from fastmcp.tools import ToolResult

    from kuckuck.mapping import Mapping
    from kuckuck.pseudonymize import build_default_detectors
    from kuckuck_mcp.middleware import KuckuckMiddleware
    from kuckuck_mcp.proxy import build_proxy
    from kuckuck_mcp.transform import pseudonymize_value, restore_value
except ImportError:  # pragma: no cover - covered by the skip marker
    _skip_mcp = True

pytestmark = pytest.mark.skipif(_skip_mcp, reason="fastmcp not installed (kuckuck[mcp] extra)")

# 32-byte master key as 64 hex chars - same shape as the CLI key file.
_TEST_KEY = SecretStr("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff")

_REAL_EMAIL = "max@firma.de"
_REAL_PHONE = "+49 30 1234567"
_EMAIL_TOKEN_RE = re.compile(r"\[\[EMAIL_[0-9a-f]+\]\]")


class Customer(BaseModel):
    """Typed tool-return model used to check structure/schema preservation."""

    name: str
    email: str
    age: int


class Contact(BaseModel):
    """Tool-return model with a format-constrained field (EmailStr)."""

    email: EmailStr


def _make_backend() -> tuple["FastMCP", list[str]]:
    """Return a dummy backend MCP server plus a list capturing received args.

    ``get_contact`` returns PII as a plain string (TextContent path).
    ``get_record`` returns PII inside a dict (structured_content path).
    ``notify`` records the ``email`` argument it actually received so a test
    can assert what crossed the proxy into the backend.
    """
    backend: FastMCP = FastMCP("dummy-backend")
    received: list[str] = []

    @backend.tool
    def get_contact() -> str:
        return f"Reach the customer at {_REAL_EMAIL} or {_REAL_PHONE}."

    @backend.tool
    def get_record() -> dict[str, str]:
        return {"email": _REAL_EMAIL, "note": f"phone {_REAL_PHONE}"}

    @backend.tool
    def notify(email: str) -> str:
        received.append(email)
        return "queued"

    @backend.resource("resource://customer/profile")
    def profile() -> str:
        return f"Customer email: {_REAL_EMAIL}, phone {_REAL_PHONE}"

    @backend.prompt
    def greeting() -> str:
        return f"Write a greeting to {_REAL_EMAIL}"

    return backend, received


def _text_of(result: object) -> str:
    """Concatenate the text content blocks of a client tool result."""
    return " ".join(block.text for block in result.content if hasattr(block, "text"))  # type: ignore[attr-defined]


async def _proxy_client(backend: "FastMCP", *, trusted: bool) -> "Client[FastMCPTransport]":
    proxy = build_proxy(backend, master=_TEST_KEY, use_ner=False, trusted=trusted)
    return Client(transport=proxy)


async def test_text_response_is_pseudonymized() -> None:
    """PII in a TextContent response is replaced by tokens before the client sees it."""
    backend, _ = _make_backend()
    client = await _proxy_client(backend, trusted=False)
    async with client:
        result = await client.call_tool("get_contact")
    rendered = _text_of(result)
    assert _REAL_EMAIL not in rendered
    assert _REAL_PHONE not in rendered
    assert _EMAIL_TOKEN_RE.search(rendered), f"expected an EMAIL token in {rendered!r}"


async def test_structured_response_is_pseudonymized() -> None:
    """PII inside structured_content (a dict) is pseudonymized leaf-by-leaf."""
    backend, _ = _make_backend()
    client = await _proxy_client(backend, trusted=False)
    async with client:
        result = await client.call_tool("get_record")
    blob = repr(result.data) + " " + _text_of(result)
    assert _REAL_EMAIL not in blob
    assert _EMAIL_TOKEN_RE.search(blob), f"expected an EMAIL token in {blob!r}"


async def test_trusted_backend_restores_token_argument() -> None:
    """A token sent as an argument is restored to real PII for a trusted backend."""
    backend, received = _make_backend()
    client = await _proxy_client(backend, trusted=True)
    async with client:
        # First surface the email so the shared mapping learns its token.
        contact = await client.call_tool("get_contact")
        match = _EMAIL_TOKEN_RE.search(_text_of(contact))
        assert match, "setup: no email token produced"
        token = match.group(0)
        # The model echoes the token back as an argument; the proxy must
        # restore it to the real address before the backend receives it.
        await client.call_tool("notify", arguments={"email": token})
    assert received == [_REAL_EMAIL]


async def test_untrusted_backend_does_not_restore_token_argument() -> None:
    """An untrusted backend receives the token literally - no cleartext leaks out."""
    backend, received = _make_backend()
    client = await _proxy_client(backend, trusted=False)
    async with client:
        contact = await client.call_tool("get_contact")
        token = _EMAIL_TOKEN_RE.search(_text_of(contact)).group(0)  # type: ignore[union-attr]
        await client.call_tool("notify", arguments={"email": token})
    assert received == [token]
    assert _REAL_EMAIL not in received


async def test_resource_response_is_pseudonymized() -> None:
    """PII in a resource read is pseudonymized - the Jira/Confluence content path."""
    backend, _ = _make_backend()
    client = await _proxy_client(backend, trusted=False)
    async with client:
        contents = await client.read_resource("resource://customer/profile")
    rendered = " ".join(block.text for block in contents if hasattr(block, "text"))
    assert _REAL_EMAIL not in rendered
    assert _REAL_PHONE not in rendered
    assert _EMAIL_TOKEN_RE.search(rendered), f"expected an EMAIL token in {rendered!r}"


async def test_prompt_is_passed_through_unchanged() -> None:
    """Prompts are a documented non-goal: a prompt template is forwarded as-is.

    The test prompt embeds an email only to prove that no transformation runs;
    real prompt templates are author-controlled and not expected to carry PII.
    """
    backend, _ = _make_backend()
    client = await _proxy_client(backend, trusted=False)
    async with client:
        result = await client.get_prompt("greeting")
    rendered = " ".join(str(message.content) for message in result.messages)
    assert _REAL_EMAIL in rendered


def _middleware(*, trusted: bool = False) -> "KuckuckMiddleware":
    return KuckuckMiddleware(
        master=_TEST_KEY,
        mapping=Mapping(),
        detectors=build_default_detectors(use_ner=False),
        trusted=trusted,
    )


def test_tool_result_meta_and_embedded_resource_are_pseudonymized() -> None:
    """The meta field and EmbeddedResource text are rewritten, not just TextContent."""
    result = ToolResult(
        content=[
            mt.TextContent(type="text", text=f"plain {_REAL_EMAIL}"),
            mt.EmbeddedResource(
                type="resource",
                resource=mt.TextResourceContents(uri=AnyUrl("resource://x"), text=f"embedded {_REAL_EMAIL}"),
            ),
        ],
        meta={"contact": _REAL_EMAIL},
    )
    _middleware()._rewrite_tool_result(result)  # pylint: disable=protected-access
    assert _REAL_EMAIL not in repr(result.content)
    assert _REAL_EMAIL not in repr(result.meta)
    assert _EMAIL_TOKEN_RE.search(repr(result.content))


def test_fail_open_env_var_enables_escape_hatch(monkeypatch: "pytest.MonkeyPatch") -> None:
    """KUCKUCK_PROXY_FAIL_OPEN=1 turns on fail-open even without the flag."""
    monkeypatch.setenv("KUCKUCK_PROXY_FAIL_OPEN", "1")
    assert _middleware()._fail_open is True  # pylint: disable=protected-access
    monkeypatch.delenv("KUCKUCK_PROXY_FAIL_OPEN")
    assert _middleware()._fail_open is False  # pylint: disable=protected-access


async def test_structured_result_preserves_schema_and_types() -> None:
    """The proxy exposes the backend's output schema and keeps non-string types.

    Only string leaves become tokens; the JSON structure and an ``int`` field
    survive, so the result still satisfies the forwarded output schema.
    """
    backend: FastMCP = FastMCP("typed-backend")

    @backend.tool
    def get_customer() -> "Customer":
        return Customer(name="Anna", email=_REAL_EMAIL, age=42)

    proxy = build_proxy(backend, master=_TEST_KEY, use_ner=False)
    async with Client(transport=proxy) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        schema = tools["get_customer"].outputSchema
        assert schema is not None and schema["properties"]["age"]["type"] == "integer"
        result = await client.call_tool("get_customer")
    structured = result.structured_content
    assert structured is not None
    assert set(structured) == {"name", "email", "age"}  # structure preserved
    assert structured["age"] == 42  # non-string type untouched
    assert structured["email"] != _REAL_EMAIL
    assert _EMAIL_TOKEN_RE.fullmatch(structured["email"])  # string leaf tokenized


async def test_format_constrained_field_is_tokenized_without_leak() -> None:
    """A format-constrained string (EmailStr) is still tokenized - the documented
    caveat is that the token violates ``format: email`` so strict client-side
    typing yields no parsed object, but no cleartext ever leaks.
    """

    backend: FastMCP = FastMCP("constrained-backend")

    @backend.tool
    def get_contact_model() -> "Contact":
        return Contact(email=_REAL_EMAIL)

    proxy = build_proxy(backend, master=_TEST_KEY, use_ner=False)
    async with Client(transport=proxy) as client:
        result = await client.call_tool("get_contact_model")
    # Security guarantee holds regardless of the schema constraint.
    assert _REAL_EMAIL not in repr(result.structured_content)
    assert _EMAIL_TOKEN_RE.fullmatch(result.structured_content["email"])
    # Caveat: the token is not a valid email, so the strict typed view is empty.
    assert result.data is None


def test_transform_round_trip_over_nested_structure() -> None:
    """pseudonymize_value / restore_value recurse through dicts, lists and scalars."""
    mapping = Mapping()
    detectors = build_default_detectors(use_ner=False)
    original: dict[str, Any] = {
        "contacts": [_REAL_EMAIL, {"alt": _REAL_EMAIL}],
        "count": 2,
        "active": True,
        "missing": None,
    }
    pseudo = pseudonymize_value(original, master=_TEST_KEY, mapping=mapping, detectors=detectors)
    assert _REAL_EMAIL not in repr(pseudo)
    assert pseudo["count"] == 2 and pseudo["active"] is True and pseudo["missing"] is None
    # Same email -> same token in both positions (shared mapping).
    assert pseudo["contacts"][0] == pseudo["contacts"][1]["alt"]
    assert restore_value(pseudo, mapping) == original
