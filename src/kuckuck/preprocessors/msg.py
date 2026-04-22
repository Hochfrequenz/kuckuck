"""Outlook .msg preprocessor — wraps :mod:`extract_msg`.

Body preference order: HTML > RTF > plain text. We prefer HTML because
modern Outlook clients render HTML primarily and the plain-text body
is often a poor auto-converted version that drops formatting context.
HTML is stripped to text via :mod:`selectolax` before pseudonymization.

Attachments are intentionally NOT processed:

* They might be huge (gigabytes); we should not silently rewrite them.
* They are often binary (PDF, DOCX) that can't be pseudonymized as
  plain text.

A warning is logged when attachments are present so the user knows the
output is not a complete pseudonymisation of the .msg file.

Reassembly is text-only: we do not try to round-trip the structured
.msg compound document. The output is a plain-text representation of
the body — sufficient for the LLM round-trip use case Kuckuck targets.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

from kuckuck.preprocessors.base import Chunk
from kuckuck.preprocessors.eml import _split_body_into_chunks  # pylint: disable=protected-access

logger = logging.getLogger(__name__)


class MsgPreprocessor:
    """Outlook .msg preprocessor — bytes-in, text-out."""

    name = "msg"

    def extract(self, source: str | bytes | Path) -> list[Chunk]:
        """Return chunks for the .msg body (HTML > RTF > plain)."""
        body = _extract_msg_body(source)
        if not body:
            return []
        return _split_body_into_chunks(body)

    def reassemble(self, source: str | bytes | Path, modified: list[Chunk]) -> str:  # pylint: disable=unused-argument
        """Return the modified body as plain text. *source* is ignored.

        The .msg compound-document round-trip is out of scope — users get
        the pseudonymized body back as a plain-text string ready to feed
        the LLM, not a re-emitted .msg file.
        """
        if not modified:
            return ""
        modified_sorted = sorted(modified, key=lambda c: int(c.locator))
        return "".join(c.text for c in modified_sorted)


def _extract_msg_body(source: str | bytes | Path) -> str:
    """Open the .msg file and return its preferred body as text.

    *source* can be a path-like (preferred), bytes, or a string that
    happens to point at a path. The CLI hands us a path; the library
    API accepts bytes for in-memory documents.
    """
    # pylint: disable-next=import-outside-toplevel
    import extract_msg

    msg = _open_msg(source, extract_msg)
    try:
        if msg.attachments:
            logger.warning(
                "MsgPreprocessor: %d attachment(s) on this message will NOT be pseudonymized.",
                len(msg.attachments),
            )

        html: Any = msg.htmlBody
        if html:
            return _html_to_text(html)
        rtf: Any = msg.rtfBody
        if rtf:
            return _rtf_to_text(rtf)
        body: Any = msg.body
        return body or ""
    finally:
        msg.close()


def _open_msg(source: str | bytes | Path, extract_msg_module: Any) -> Any:
    """Hand *source* to :func:`extract_msg.openMsg` regardless of type."""
    if isinstance(source, bytes):
        return extract_msg_module.openMsg(io.BytesIO(source))
    return extract_msg_module.openMsg(str(source))


def _html_to_text(html: bytes | str) -> str:
    """Strip HTML to a text body using :mod:`selectolax`.

    Drops ``<script>`` / ``<style>`` / ``<noscript>`` nodes so any
    JavaScript or CSS payload does not flow through the detectors.
    """
    # pylint: disable-next=import-outside-toplevel
    from selectolax.parser import HTMLParser

    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    parser = HTMLParser(html)
    for node in parser.css("script, style, noscript"):
        node.decompose()
    body = parser.body
    if body is None:
        return ""
    return body.text(separator="\n", strip=False)


def _rtf_to_text(rtf: bytes | str) -> str:
    """Best-effort RTF-to-text strip without pulling a heavy RTF library.

    extract-msg can decapsulate RTF via :meth:`Message.deencapsulatedRtf`
    when the RTF wraps an HTML or plain body, but that is not always
    populated. As a fallback we strip RTF control words with a regex —
    the result is messy but contains the prose, which is what the
    detectors need.
    """
    if isinstance(rtf, bytes):
        rtf = rtf.decode("latin-1", errors="replace")
    # Drop control groups like {\fonttbl ... } and control words like \b0.
    # This is intentionally simplistic — RTF is a deep format and we are
    # not trying to faithfully render it, only to expose any prose for
    # the regex detectors.
    import re  # pylint: disable=import-outside-toplevel

    cleaned = re.sub(r"\{\\\*?\\[^{}]*\}", " ", rtf)  # discard control groups
    cleaned = re.sub(r"\\[a-zA-Z]+\d* ?", " ", cleaned)  # control words
    cleaned = re.sub(r"[{}]", " ", cleaned)  # leftover braces
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned
