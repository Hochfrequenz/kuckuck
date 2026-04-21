"""RFC 5322 email (.eml) preprocessor.

Strategy:

1. Parse the message with the stdlib :mod:`email` package.
2. Pick the best body part: prefer text/plain, fall back to text/html
   stripped to text via :mod:`selectolax`. (HTML attribute values
   cannot contain plain-text PII the user wants to keep separate, so
   selectolax's text extraction is the right granularity here.)
3. Split the body into prose / quoted-reply / signature regions:
    * lines beginning with ``>`` (after optional whitespace) are quoted
      replies and are emitted as their own chunks. We do pseudonymize
      them — quoted PII is still PII the LLM should not see — but the
      chunking keeps the per-region ordering intact.
    * the first line whose body matches a known German signature
      trigger (``Mit freundlichen Grüßen``, ``Viele Grüße``, ``Beste
      Grüße``, ``Gesendet von meinem iPhone``, ``Von meinem Samsung
      Galaxy``) starts the signature region. Everything from that
      line onwards is one chunk.
    * if no trigger fires, the **last 8 lines** of the body are treated
      as the signature region (Mailgun's ``talon`` library uses this
      heuristic). Names, phones and emails in the signature still get
      pseudonymized; the chunk split exists so future detectors can
      apply different rules per region (e.g. drop the whole signature
      block instead of pseudonymizing it).

Reassembly emits the original headers verbatim and re-builds the body
from the chunks. The output is a single text/plain MIME message — we
do not try to round-trip multipart structures or HTML, because the
LLM-bound text is what the user cares about.
"""

from __future__ import annotations

import email
import re
from email.message import EmailMessage, Message
from email.policy import default as default_policy

from kuckuck.preprocessors.base import Chunk

#: German signature triggers. Match is case-insensitive and tolerant of
#: a trailing comma. Each trigger marks the START of the signature so
#: everything from the matched line onwards is one chunk. Order from
#: most-specific to least-specific to avoid greedy short matches eating
#: longer phrases (e.g. "Viele Grüße" wins over "Grüße").
_SIGNATURE_TRIGGERS_DE: tuple[str, ...] = (
    "Mit freundlichen Grüßen",
    "Mit freundlichen Gruessen",
    "Mit freundlichen Grußen",
    "Mit freundlichem Gruß",
    "Mit freundlichem Gruss",
    "Mit besten Grüßen",
    "Mit besten Gruessen",
    "Beste Grüße",
    "Beste Gruesse",
    "Viele Grüße",
    "Viele Gruesse",
    "Liebe Grüße",
    "Liebe Gruesse",
    "Herzliche Grüße",
    "Herzliche Gruesse",
    "Gesendet von meinem iPhone",
    "Gesendet von meinem iPad",
    "Von meinem Samsung Galaxy gesendet",
    "Von meinem Samsung Galaxy",
    "Von meinem iPhone gesendet",
)

_SIGNATURE_TRIGGER_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(t) for t in _SIGNATURE_TRIGGERS_DE) + r")\b",
    re.IGNORECASE,
)

#: Heuristic fallback when no trigger is found: treat the last N lines
#: as signature. talon uses 8 in its line-based extractor.
_FALLBACK_SIGNATURE_LINES = 8

#: Lines that begin with ``>`` (after optional whitespace) are replies.
_QUOTED_LINE_RE = re.compile(r"^\s*>")


class EmlPreprocessor:
    """Email (.eml) preprocessor with signature/reply chunking."""

    name = "eml"

    def extract(self, source: str) -> list[Chunk]:
        """Return chunks covering the email body, headers excluded."""
        if not source.strip():
            return []
        message = email.message_from_string(source, policy=default_policy)
        body = _select_body(message)
        if not body:
            return []
        return _split_body_into_chunks(body)

    def reassemble(self, source: str, modified: list[Chunk]) -> str:
        """Rebuild *source*'s headers around the modified body chunks."""
        if not modified:
            return source
        message = email.message_from_string(source, policy=default_policy)
        # Concatenate chunks in document order; locators are sequential
        # ints so we can rebuild the body deterministically.
        modified_sorted = sorted(modified, key=lambda c: int(c.locator))
        new_body = "".join(c.text for c in modified_sorted)
        # Rewriting the body as plain text loses any HTML/multipart
        # structure - that is intentional. The output is for the LLM
        # roundtrip, not for archival re-injection into a mail client.
        new_message = EmailMessage(policy=default_policy)
        for header, value in message.items():
            # Drop Content-Type and Content-Transfer-Encoding so the new
            # plain-text body is correctly described. EmailMessage will
            # set them when we call set_content.
            if header.lower() in {"content-type", "content-transfer-encoding", "mime-version"}:
                continue
            new_message[header] = value
        new_message.set_content(new_body)
        return new_message.as_string()


def _select_body(message: Message) -> str:
    """Return the best body string for *message*: plain > html > empty."""
    plain = _first_part(message, "text/plain")
    if plain:
        return plain
    html = _first_part(message, "text/html")
    if html:
        return _html_to_text(html)
    return ""


def _first_part(message: Message, content_type: str) -> str:
    """Return the payload of the first part with *content_type*."""
    for part in message.walk():
        if part.get_content_type() == content_type and not part.is_multipart():
            payload = part.get_content() if hasattr(part, "get_content") else part.get_payload(decode=True)
            if isinstance(payload, bytes):
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
            if isinstance(payload, str):
                return payload
    return ""


def _html_to_text(html: str) -> str:
    """Strip HTML to a text body using :mod:`selectolax`."""
    # pylint: disable-next=import-outside-toplevel
    from selectolax.parser import HTMLParser

    parser = HTMLParser(html)
    body = parser.body
    if body is None:
        return ""
    # text(separator='\n') keeps line structure so the signature/reply
    # heuristics still see one logical statement per line.
    return body.text(separator="\n", strip=False)


def _split_body_into_chunks(body: str) -> list[Chunk]:
    """Cut *body* into prose / quoted / signature chunks (in that order)."""
    lines = body.splitlines(keepends=True)
    sig_start = _find_signature_start(lines)

    chunks: list[Chunk] = []
    locator = 0

    # Walk the prose region, splitting on quoted-reply boundaries.
    in_quote: bool | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal locator, buffer
        if buffer:
            chunks.append(Chunk(text="".join(buffer), locator=locator))
            locator += 1
            buffer = []

    for idx in range(sig_start):
        line = lines[idx]
        is_quoted = bool(_QUOTED_LINE_RE.match(line))
        if in_quote is None:
            in_quote = is_quoted
        elif is_quoted != in_quote:
            flush()
            in_quote = is_quoted
        buffer.append(line)
    flush()

    # Signature region as one trailing chunk.
    if sig_start < len(lines):
        chunks.append(Chunk(text="".join(lines[sig_start:]), locator=locator))

    return chunks


def _find_signature_start(lines: list[str]) -> int:
    """Return the line index where the signature region begins."""
    for idx, line in enumerate(lines):
        if _SIGNATURE_TRIGGER_RE.match(line):
            return idx
    if len(lines) > _FALLBACK_SIGNATURE_LINES:
        return len(lines) - _FALLBACK_SIGNATURE_LINES
    # Body shorter than the heuristic window: no signature region.
    return len(lines)
