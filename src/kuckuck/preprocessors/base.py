"""Format-aware preprocessor protocol.

A preprocessor turns a structured input (an .eml message, a Markdown
document, an XML tree) into a list of :class:`Chunk` objects that the
core pseudonymize pipeline can scan for entities. Once each chunk has
been replaced, :meth:`Preprocessor.reassemble` rebuilds the source so
the structural skeleton is preserved — code blocks stay code blocks,
XML attributes stay attributes, mail headers stay mail headers.

The contract is intentionally minimal:

* :meth:`extract` returns chunks in document order. Each chunk's
  :attr:`Chunk.text` is the raw substring that should be pseudonymized.
  The :attr:`Chunk.locator` is opaque payload that lets the matching
  :meth:`reassemble` find where to insert the modified text. We do not
  use absolute character offsets at the protocol level because some
  formats (XML, Markdown AST) reason about nodes, not bytes.
* :meth:`reassemble` takes the original source plus the chunks (with
  their text fields modified) and returns the new document as a string.
  Callers must pass the *same* chunk instances they got from
  :meth:`extract` so the locators line up.

The text preprocessor (`text.py`) is the trivial identity case and
exists so callers can write ``preprocessor or TextPreprocessor()``
without a None-check.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class Chunk(BaseModel):
    """A piece of source text that should be sent through the detectors.

    The :attr:`locator` field is opaque to the core pipeline — only the
    producing preprocessor reads it during :meth:`Preprocessor.reassemble`.
    Subclassing :class:`BaseModel` instead of using a dataclass keeps the
    chunk type pickleable for snapshot tests.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    text: str = Field(description="The substring that should be pseudonymized.")
    locator: Any = Field(default=None, description="Opaque marker the preprocessor uses to put text back.")


class Preprocessor(Protocol):
    """Minimal interface every format-aware preprocessor implements.

    Implementations live in sibling modules — :mod:`kuckuck.preprocessors.text`,
    :mod:`kuckuck.preprocessors.eml`, etc. The :attr:`name` attribute is used
    in CLI ``--format`` listings and in error messages.
    """

    name: str

    def extract(self, source: str) -> list[Chunk]:  # pragma: no cover - protocol
        """Return chunks of *source* that should be pseudonymized."""

    def reassemble(self, source: str, modified: list[Chunk]) -> str:  # pragma: no cover - protocol
        """Return *source* with each chunk's modified text re-inserted."""
