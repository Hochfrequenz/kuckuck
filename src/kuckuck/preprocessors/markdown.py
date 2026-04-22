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
import secrets

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
#: Sentinel format. The ``{salt}`` placeholder is replaced with a fresh
#: random hex string per document so a literal "KUCKUCKINLINECODE0..."
#: in user prose cannot collide with the masking step. The salt makes
#: collisions astronomically unlikely (16^16 ~ 1.8e19 possibilities).
_SENTINEL_BASE = "KUCKUCKINLINECODE_{salt}_"
_SENTINEL_END = "_ENDCODE"
_SENTINEL_SALT_BYTES = 8

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
        # Per-document salt prevents the inline-code sentinel from
        # colliding with literal user prose that happens to contain the
        # static prefix.
        salt = secrets.token_hex(_SENTINEL_SALT_BYTES)
        chunks: list[Chunk] = []
        run_start: int | None = None
        for idx in range(len(lines)):
            if idx in skip_lines:
                if run_start is not None:
                    chunks.append(_make_chunk(lines, run_start, idx, salt))
                    run_start = None
            elif run_start is None:
                run_start = idx
        if run_start is not None:
            chunks.append(_make_chunk(lines, run_start, len(lines), salt))
        return chunks

    def reassemble(self, source: str, modified: list[Chunk]) -> str:
        """Splice each chunk's modified text back into *source* by line range."""
        if not modified:
            return source
        lines = source.splitlines(keepends=True)
        by_start: dict[int, tuple[int, str]] = {}
        for chunk in modified:
            if not isinstance(chunk.locator, tuple) or len(chunk.locator) != 4:
                raise ValueError(f"Invalid markdown chunk locator: {chunk.locator!r}")
            start, end, salt, mask_table = chunk.locator
            by_start[start] = (end, _unmask_inline_code(chunk.text, salt, mask_table))

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


def _make_chunk(lines: list[str], start: int, end: int, salt: str) -> Chunk:
    """Build a chunk for ``lines[start:end]`` with inline-code masked."""
    raw = "".join(lines[start:end])
    masked, mask_table = _mask_inline_code(raw, salt)
    return Chunk(text=masked, locator=(start, end, salt, mask_table))


def _mask_inline_code(text: str, salt: str) -> tuple[str, dict[str, str]]:
    """Replace every inline-code span with a salted sentinel; return mapping."""
    table: dict[str, str] = {}
    prefix = _SENTINEL_BASE.format(salt=salt)

    def _sub(match: re.Match[str]) -> str:
        idx = len(table)
        sentinel = f"{prefix}{idx}{_SENTINEL_END}"
        table[sentinel] = match.group(0)
        return sentinel

    return _INLINE_CODE_RE.sub(_sub, text), table


def _unmask_inline_code(text: str, salt: str, table: dict[str, str]) -> str:
    """Reverse :func:`_mask_inline_code`. Unknown sentinels are left as-is."""
    prefix = _SENTINEL_BASE.format(salt=salt)
    sentinel_re = re.compile(rf"{re.escape(prefix)}(\d+){re.escape(_SENTINEL_END)}")

    def _sub(match: re.Match[str]) -> str:
        return table.get(match.group(0), match.group(0))

    return sentinel_re.sub(_sub, text)


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
