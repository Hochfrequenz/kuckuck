"""Markdown preprocessor — line-range extraction with inline-code masking.

Strategy:

1. Tokenise the document with :mod:`markdown_it`. Block-level tokens
   carry a ``map = [line_start, line_end)`` range; we use that range
   to decide which lines are processable.
2. Lines that fall inside a fenced/indented code block, an HTML block,
   or YAML frontmatter are skipped — those regions stay byte-identical
   in the output.
3. Inline ``code`` (`` `like_this` ``) is masked with a sentinel before
   the chunk is sent through the detectors and unmasked afterwards, so
   the PII detectors do not match identifiers that happen to live in
   inline code.

Reassembly is byte-faithful for the skipped regions — we only rewrite
the line ranges we actually extracted. That keeps Markdown-specific
syntax (list markers, table pipes, footnote definitions) intact.

Reference-style links and footnote definitions ARE processed: the URL
target ``[label]: https://...`` and the footnote body ``[^id]: text``
are emitted as their own line chunks because they live in normal block
positions in the token stream.
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt

from kuckuck.preprocessors.base import Chunk

_SKIP_BLOCK_TYPES = frozenset(
    {
        "fence",
        "code_block",
        "html_block",
    }
)

_INLINE_CODE_RE = re.compile(r"`+[^`\n]+?`+")
_SENTINEL_PREFIX = "KUCKUCKINLINECODE"
_SENTINEL_SUFFIX = "ENDCODE"
_SENTINEL_RE = re.compile(rf"{_SENTINEL_PREFIX}(\d+){_SENTINEL_SUFFIX}")

_YAML_FRONTMATTER_DELIM = "---"


class MarkdownPreprocessor:
    """Line-range Markdown preprocessor with inline-code masking."""

    name = "markdown"

    def __init__(self) -> None:
        self._md = MarkdownIt("commonmark", {"html": False}).enable("table")

    def extract(self, source: str) -> list[Chunk]:
        """Return one chunk per processable line range in *source*."""
        if not source:
            return []
        lines = source.splitlines(keepends=True)
        skip_lines = _compute_skip_lines(self._md, source, lines)
        chunks: list[Chunk] = []
        run_start: int | None = None
        for idx in range(len(lines)):
            if idx in skip_lines:
                if run_start is not None:
                    chunks.append(_make_chunk(lines, run_start, idx))
                    run_start = None
            elif run_start is None:
                run_start = idx
        if run_start is not None:
            chunks.append(_make_chunk(lines, run_start, len(lines)))
        return chunks

    def reassemble(self, source: str, modified: list[Chunk]) -> str:
        """Splice each chunk's modified text back into *source* by line range."""
        if not modified:
            return source
        lines = source.splitlines(keepends=True)
        by_start: dict[int, tuple[int, str]] = {}
        for chunk in modified:
            if not isinstance(chunk.locator, tuple) or len(chunk.locator) != 3:
                raise ValueError(f"Invalid markdown chunk locator: {chunk.locator!r}")
            start, end, mask_table = chunk.locator
            by_start[start] = (end, _unmask_inline_code(chunk.text, mask_table))

        out: list[str] = []
        idx = 0
        while idx < len(lines):
            replacement = by_start.get(idx)
            if replacement is not None:
                end, new_text = replacement
                out.append(new_text)
                idx = end
            else:
                out.append(lines[idx])
                idx += 1
        return "".join(out)


def _make_chunk(lines: list[str], start: int, end: int) -> Chunk:
    """Build a chunk for ``lines[start:end]`` with inline-code masked."""
    raw = "".join(lines[start:end])
    masked, mask_table = _mask_inline_code(raw)
    return Chunk(text=masked, locator=(start, end, mask_table))


def _mask_inline_code(text: str) -> tuple[str, dict[str, str]]:
    """Replace every inline-code span with a sentinel; return mapping."""
    table: dict[str, str] = {}

    def _sub(match: re.Match[str]) -> str:
        idx = len(table)
        sentinel = f"{_SENTINEL_PREFIX}{idx}{_SENTINEL_SUFFIX}"
        table[sentinel] = match.group(0)
        return sentinel

    return _INLINE_CODE_RE.sub(_sub, text), table


def _unmask_inline_code(text: str, table: dict[str, str]) -> str:
    """Reverse :func:`_mask_inline_code`. Unknown sentinels are left as-is."""

    def _sub(match: re.Match[str]) -> str:
        return table.get(match.group(0), match.group(0))

    return _SENTINEL_RE.sub(_sub, text)


def _compute_skip_lines(md: MarkdownIt, source: str, lines: list[str]) -> set[int]:
    """Return zero-based line indices that must NOT be pseudonymized."""
    skip: set[int] = set()

    if _has_yaml_frontmatter(lines):
        for idx in range(1, len(lines)):
            if lines[idx].strip() == _YAML_FRONTMATTER_DELIM:
                skip.update(range(0, idx + 1))
                break

    tokens = md.parse(source)
    for token in tokens:
        if token.type in _SKIP_BLOCK_TYPES and token.map is not None:
            line_start, line_end = token.map
            skip.update(range(line_start, line_end))
    return skip


def _has_yaml_frontmatter(lines: list[str]) -> bool:
    """Return True when the document opens with ``---`` on its own line."""
    return bool(lines) and lines[0].strip() == _YAML_FRONTMATTER_DELIM
