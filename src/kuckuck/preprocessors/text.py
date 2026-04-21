"""Identity preprocessor for plain-text inputs.

Exists so the public ``pseudonymize_text(..., preprocessor=...)`` API
takes a uniform :class:`Preprocessor` argument without a None-check
fallback. For text we have no structure to preserve — the whole input
is one chunk and reassembly is just returning the modified chunk.
"""

from __future__ import annotations

from kuckuck.preprocessors.base import Chunk


class TextPreprocessor:
    """Pass-through preprocessor: one chunk per input, no structure."""

    name = "text"

    def extract(self, source: str) -> list[Chunk]:
        """Return a single chunk holding the entire *source*."""
        return [Chunk(text=source, locator=None)]

    def reassemble(self, source: str, modified: list[Chunk]) -> str:  # pylint: disable=unused-argument
        """Return the modified chunk text. *source* is ignored.

        With a single chunk covering the whole input the modified chunk
        text IS the new source — the original is dropped on purpose so
        any in-chunk replacements (token substitutions) win.
        """
        if not modified:
            return ""
        return modified[0].text
