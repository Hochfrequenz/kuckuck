"""Tests for format-aware preprocessors and end-to-end pipeline.

Each preprocessor gets:

* A handful of unit tests covering positive and negative cases for the
  format's structure (e.g. signatures get found, code fences are not
  touched, attribute values flow through XML).
* End-to-end snapshot tests through the synthetic fixtures in
  ``unittests/example_files/`` to lock the output shape of a realistic
  document.

Fixtures contain only synthetic email addresses (``@firma.de``,
``@example.com``) and made-up names (Hans, Eva, Klaus) so we can commit
them without worrying about real PII leakage.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from kuckuck.preprocessors import (
    EmlPreprocessor,
    MarkdownPreprocessor,
    MsgPreprocessor,
    TextPreprocessor,
    XmlPreprocessor,
)
from kuckuck.preprocessors.base import Chunk
from kuckuck.preprocessors.eml import _find_signature_start  # pylint: disable=protected-access
from kuckuck.pseudonymize import pseudonymize_text, restore_text

MASTER = SecretStr("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff")
EXAMPLE_DIR = Path(__file__).parent / "example_files"


class TestTextPreprocessor:
    def test_extract_returns_single_chunk(self) -> None:
        pre = TextPreprocessor()
        chunks = pre.extract("hello world")
        assert len(chunks) == 1
        assert chunks[0].text == "hello world"

    def test_reassemble_returns_modified_chunk(self) -> None:
        pre = TextPreprocessor()
        chunks = pre.extract("hello")
        chunks[0] = Chunk(text="HELLO", locator=chunks[0].locator)
        assert pre.reassemble("hello", chunks) == "HELLO"

    def test_reassemble_empty_returns_empty_string(self) -> None:
        assert TextPreprocessor().reassemble("anything", []) == ""

    def test_pipeline_round_trip(self) -> None:
        result = pseudonymize_text("Kontakt max@firma.de", MASTER, preprocessor=TextPreprocessor())
        assert "[[EMAIL_" in result.text
        assert restore_text(result.text, result.mapping) == "Kontakt max@firma.de"


class TestMarkdownPreprocessor:
    def test_extract_skips_fenced_code_block(self) -> None:
        pre = MarkdownPreprocessor()
        chunks = pre.extract("intro line\n\n```\ncode with max@firma.de\n```\n\nafter\n")
        joined = "".join(c.text for c in chunks)
        assert "max@firma.de" not in joined
        assert "intro line" in joined
        assert "after" in joined

    def test_extract_skips_indented_code_block(self) -> None:
        pre = MarkdownPreprocessor()
        chunks = pre.extract("intro\n\n    indented max@firma.de\n\nafter\n")
        joined = "".join(c.text for c in chunks)
        assert "max@firma.de" not in joined

    def test_extract_skips_yaml_frontmatter(self) -> None:
        pre = MarkdownPreprocessor()
        chunks = pre.extract("---\ntitle: x\nauthor: alice@example.com\n---\n\nbody\n")
        joined = "".join(c.text for c in chunks)
        assert "alice@example.com" not in joined
        assert "body" in joined

    def test_inline_code_masked_during_extract(self) -> None:
        pre = MarkdownPreprocessor()
        chunks = pre.extract("Hello `max@firma.de` ping\n")
        assert "max@firma.de" not in chunks[0].text
        # Sentinel survives in the chunk text.
        assert "KUCKUCKINLINECODE" in chunks[0].text

    def test_inline_code_restored_during_reassemble(self) -> None:
        pre = MarkdownPreprocessor()
        source = "Hello `max@firma.de` ping\n"
        chunks = pre.extract(source)
        rebuilt = pre.reassemble(source, chunks)
        assert "`max@firma.de`" in rebuilt

    def test_pipeline_round_trip_through_markdown(self) -> None:
        source = "# Heading\n\nMail: max@firma.de\n"
        result = pseudonymize_text(source, MASTER, preprocessor=MarkdownPreprocessor())
        assert "[[EMAIL_" in result.text
        assert "max@firma.de" not in result.text
        # Heading structure preserved.
        assert "# Heading" in result.text

    def test_sentinel_lookalike_in_user_prose_survives_round_trip(self) -> None:
        # Per-document salted sentinel: even text containing the static
        # KUCKUCKINLINECODE prefix must not collide with the masking step.
        pre = MarkdownPreprocessor()
        source = "Reference: KUCKUCKINLINECODE_aaaaaaaaaaaaaaaa_42_ENDCODE plus my email `max@firma.de`\n"
        chunks = pre.extract(source)
        rebuilt = pre.reassemble(source, chunks)
        # Reassembly without any chunk modification must reproduce the
        # input exactly, including the lookalike literal.
        assert rebuilt == source


class TestEmlPreprocessor:
    def test_extract_skips_headers(self) -> None:
        pre = EmlPreprocessor()
        source = "From: a@example.com\nTo: b@example.com\nSubject: hi\n\nBody line with max@firma.de\n"
        chunks = pre.extract(source)
        joined = "".join(c.text for c in chunks)
        # Headers contain a@example.com / b@example.com but they're not in chunks.
        assert "a@example.com" not in joined
        assert "max@firma.de" in joined

    @pytest.mark.parametrize(
        "trigger",
        [
            "Mit freundlichen Gruessen",
            "Mit freundlichen Grußen",  # NFC umlaut
            "Viele Gruesse",
            "Beste Gruesse",
            "Gesendet von meinem iPhone",
            "Von meinem Samsung Galaxy gesendet",
        ],
    )
    def test_signature_trigger_found(self, trigger: str) -> None:
        # The signature trigger detection is the heart of the .eml
        # heuristic; parametrise to lock the German trigger list.
        lines = ["body line\n", "more body\n", f"{trigger}\n", "Max Mustermann\n"]
        idx = _find_signature_start(lines)
        assert idx == 2

    def test_signature_fallback_to_last_lines(self) -> None:
        # 12 lines with no trigger -> last 8 are signature.
        lines = [f"line {i}\n" for i in range(12)]
        assert _find_signature_start(lines) == 4

    def test_signature_fallback_short_body(self) -> None:
        # Bodies shorter than the heuristic window: no signature region.
        lines = ["a\n", "b\n", "c\n"]
        assert _find_signature_start(lines) == 3

    def test_quoted_lines_split_into_their_own_chunk(self) -> None:
        pre = EmlPreprocessor()
        source = "From: a@example.com\n\nprose line\n> quoted line one\n> quoted line two\nmore prose\n"
        chunks = pre.extract(source)
        # We expect at least two chunks: prose, quoted, more prose. The
        # signature-fallback may pull "more prose" into its own chunk;
        # the relevant invariant is that the quoted lines live on their
        # own chunk so future per-region rules can target them.
        quoted_chunks = [c for c in chunks if "> quoted line" in c.text]
        assert len(quoted_chunks) == 1
        assert "prose line" not in quoted_chunks[0].text

    def test_html_body_extracted_to_text(self) -> None:
        pre = EmlPreprocessor()
        source = EXAMPLE_DIR.joinpath("sample_html_email.eml").read_text(encoding="utf-8")
        chunks = pre.extract(source)
        joined = "".join(c.text for c in chunks)
        assert "Hans Mueller" in joined
        # HTML tags should NOT show up in the extracted text.
        assert "<p>" not in joined
        assert "<strong>" not in joined

    def test_pipeline_round_trip_with_signature(self) -> None:
        pre = EmlPreprocessor()
        source = EXAMPLE_DIR.joinpath("sample_email.eml").read_text(encoding="utf-8")
        result = pseudonymize_text(source, MASTER, preprocessor=pre)
        # Signature email addresses get pseudonymized.
        assert "eva.schmidt@firma.de" not in result.text
        assert "klaus.mueller@firma.de" not in result.text
        # Header addresses stay (we don't process headers).
        assert "alice@example.com" in result.text
        # Phone got tokenized.
        assert "[[PHONE_" in result.text
        assert "[[EMAIL_" in result.text


class TestMsgPreprocessor:
    # extract-msg requires actual .msg files (compound documents). Fully
    # exercising it without binary fixtures is impractical, so the unit
    # tests here focus on the pure-Python helpers (RTF/HTML strip).
    def test_html_strip_removes_tags(self) -> None:
        # pylint: disable-next=protected-access
        from kuckuck.preprocessors.msg import _html_to_text

        html = "<html><body><p>Hi <b>Hans</b></p><p>by max@firma.de</p></body></html>"
        text = _html_to_text(html)
        assert "Hans" in text
        assert "max@firma.de" in text
        assert "<p>" not in text
        assert "<b>" not in text

    def test_html_strip_drops_script_and_style(self) -> None:
        # pylint: disable-next=protected-access
        from kuckuck.preprocessors.msg import _html_to_text

        html = (
            "<html><body>"
            '<script>var x = "secret@hacker.com"</script>'
            "<style>body { color: red; }</style>"
            "<p>Hi from max@firma.de</p>"
            "</body></html>"
        )
        text = _html_to_text(html)
        # PII inside <script>/<style> must NOT show up in the extracted text.
        assert "secret@hacker.com" not in text
        assert "color: red" not in text
        # Real prose still comes through.
        assert "max@firma.de" in text

    def test_rtf_strip_recovers_prose(self) -> None:
        # pylint: disable-next=protected-access
        from kuckuck.preprocessors.msg import _rtf_to_text

        rtf = r"{\rtf1\ansi\ansicpg1252 Hello \b Hans\b0  by max@firma.de}"
        text = _rtf_to_text(rtf)
        assert "Hans" in text
        assert "max@firma.de" in text

    def test_reassemble_concatenates_chunks_in_locator_order(self) -> None:
        pre = MsgPreprocessor()
        chunks = [
            Chunk(text="second", locator=1),
            Chunk(text="first", locator=0),
        ]
        rebuilt = pre.reassemble(b"unused", chunks)
        assert rebuilt == "firstsecond"

    def test_extract_dispatches_to_extract_msg_with_fake_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Verify the .msg pipeline wiring (path -> openMsg -> body) without
        # needing a real .msg compound-document fixture committed to the repo.
        import sys
        import types

        opened_paths: list[str] = []
        attachments_sig: list[int] = []

        class FakeMsg:
            def __init__(self, path: str, attachments: int = 0) -> None:
                opened_paths.append(path)
                attachments_sig.append(attachments)
                self.attachments = list(range(attachments))
                # extract-msg uses camelCase attribute names; mirror them.
                self.htmlBody = b""  # pylint: disable=invalid-name
                self.rtfBody = b""  # pylint: disable=invalid-name
                self.body = "Hallo Hans, ruf max@firma.de an"

            def close(self) -> None:
                pass

        fake_module = types.ModuleType("extract_msg")
        fake_module.openMsg = lambda p, **kw: FakeMsg(str(p))  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "extract_msg", fake_module)

        pre = MsgPreprocessor()
        chunks = pre.extract("/some/path/foo.msg")
        joined = "".join(c.text for c in chunks)
        assert opened_paths == ["/some/path/foo.msg"]
        assert "max@firma.de" in joined
        assert "Hans" in joined


class TestXmlPreprocessor:
    def test_extract_emits_text_tail_and_attrs(self) -> None:
        pre = XmlPreprocessor()
        chunks = pre.extract('<root><para att="max@firma.de">eva text</para>tail anna</root>')
        texts = sorted(c.text for c in chunks)
        assert "eva text" in texts
        assert "tail anna" in texts
        assert "max@firma.de" in texts

    def test_skips_comments_and_processing_instructions(self) -> None:
        pre = XmlPreprocessor()
        # Comments and PIs have non-string .tag in lxml; collecting them
        # would crash the path lookup. The preprocessor must skip them.
        source = (
            "<?xml version='1.0'?>\n"
            "<root>\n"
            "  <!-- a comment with max@firma.de inside -->\n"
            "  <para>visible max@firma.de</para>\n"
            "</root>"
        )
        chunks = pre.extract(source)
        # Only the para text is in chunks; the comment text is preserved
        # as-is in the source skeleton during reassembly.
        assert any(c.text.strip() == "visible max@firma.de" for c in chunks)

    def test_pipeline_round_trip_preserves_xml_structure(self) -> None:
        source = EXAMPLE_DIR.joinpath("confluence_storage.xml").read_text(encoding="utf-8")
        result = pseudonymize_text(source, MASTER, preprocessor=XmlPreprocessor())
        # Structural elements survive.
        assert "<ac:layout>" in result.text
        assert "<ri:user" in result.text
        # Plain-text PII gets tokenized.
        assert "max@firma.de" not in result.text
        assert "[[EMAIL_" in result.text
        # Attribute values are processed too.
        assert "hans@firma.de" not in result.text

    def test_invalid_xml_raises(self) -> None:
        pre = XmlPreprocessor()
        # lxml rejects unclosed tags; we intentionally do not swallow
        # parse errors so the user sees a real diagnostic. We catch the
        # lxml.etree.XMLSyntaxError via its base class to avoid pulling
        # the symbol that pylint cannot resolve through the lxml stubs.
        with pytest.raises(SyntaxError):
            pre.extract("<root><unclosed>")

    def test_xxe_external_entity_blocked(self, tmp_path: Path) -> None:
        # XML External Entity attack: a hostile .xml file should not be
        # able to leak local file contents through entity expansion.
        # Path.as_uri() produces a platform-correct file:// URI - on
        # Windows that means forward slashes, otherwise lxml rejects
        # the URI itself before our hardening can kick in.
        secret = tmp_path / "secret.txt"
        secret.write_text("LEAKED-SECRET-PAYLOAD", encoding="utf-8")
        hostile = (
            "<?xml version='1.0'?>"
            f'<!DOCTYPE r [<!ENTITY xxe SYSTEM "{secret.as_uri()}">]>'
            "<r>&xxe;</r>"
        )
        pre = XmlPreprocessor()
        # Two acceptable behaviours block the leak: the parser refuses
        # the document outright (XMLSyntaxError, which inherits from
        # SyntaxError) or it silently drops the entity expansion. Only
        # the case where the secret actually appears in the extracted
        # text is a real failure.
        try:
            chunks = pre.extract(hostile)
        except SyntaxError:
            return
        joined = "".join(c.text for c in chunks)
        assert "LEAKED-SECRET-PAYLOAD" not in joined

    def test_cdata_wrapper_preserved_through_round_trip(self) -> None:
        # CDATA-wrapped content must remain CDATA-wrapped after the
        # preprocessor rewrites the element's text - Confluence Storage
        # Format relies on this for ac:plain-text-body macro params.
        pre = XmlPreprocessor()
        source = "<root><body><![CDATA[max@firma.de]]></body></root>"
        result = pseudonymize_text(source, MASTER, preprocessor=pre)
        assert "<![CDATA[" in result.text
        assert "[[EMAIL_" in result.text
        assert "max@firma.de" not in result.text


class TestPipelineSnapshots:
    @pytest.mark.snapshot
    def test_eml_snapshot(self, snapshot):  # type: ignore[no-untyped-def]
        source = EXAMPLE_DIR.joinpath("sample_email.eml").read_text(encoding="utf-8")
        result = pseudonymize_text(source, MASTER, preprocessor=EmlPreprocessor())
        assert result.text == snapshot

    @pytest.mark.snapshot
    def test_markdown_snapshot(self, snapshot):  # type: ignore[no-untyped-def]
        source = EXAMPLE_DIR.joinpath("sample.md").read_text(encoding="utf-8")
        result = pseudonymize_text(source, MASTER, preprocessor=MarkdownPreprocessor())
        assert result.text == snapshot

    @pytest.mark.snapshot
    def test_xml_snapshot(self, snapshot):  # type: ignore[no-untyped-def]
        source = EXAMPLE_DIR.joinpath("confluence_storage.xml").read_text(encoding="utf-8")
        result = pseudonymize_text(source, MASTER, preprocessor=XmlPreprocessor())
        assert result.text == snapshot
