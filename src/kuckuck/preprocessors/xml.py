"""XML preprocessor — text nodes and attribute values via :mod:`lxml`.

The preprocessor walks the parsed tree and emits one :class:`Chunk` per
processable string slot:

* element ``text`` (the prose between an opening tag and its first child)
* element ``tail`` (the prose after the closing tag, before the next sibling)
* every attribute value

Tag names, attribute names and the structural skeleton of the document
are not touched. ``strip_cdata=False`` keeps CDATA sections intact for
serialisation, so output documents look byte-similar to the input.

Confluence Storage Format note: ``<ri:user ri:account-id="...">`` carries
opaque identifier strings that look like nothing the regex detectors
match. The preprocessor emits the attribute value as a chunk so users
who want to pseudonymize specific account-ids can add them to a
denylist; otherwise account-ids pass through untouched. A dedicated
Confluence-user detector is tracked as a follow-up.
"""

from __future__ import annotations

from typing import Any

from lxml import etree  # type: ignore[import-untyped]

from kuckuck.preprocessors.base import Chunk


class XmlPreprocessor:
    """XML / Confluence Storage Format preprocessor."""

    name = "xml"

    def __init__(self) -> None:
        # remove_blank_text=False keeps the original whitespace between
        # tags so reassembled output matches the input byte-for-byte
        # outside the chunks we deliberately rewrite.
        self._parser = etree.XMLParser(strip_cdata=False, remove_blank_text=False)

    def extract(self, source: str) -> list[Chunk]:
        """Return one chunk per non-empty text/tail/attribute slot."""
        if not source.strip():
            return []
        tree = etree.fromstring(source.encode("utf-8"), self._parser)
        chunks: list[Chunk] = []
        for element in tree.iter():
            if not isinstance(element.tag, str):  # comments, PIs
                continue
            self._collect_text_chunks(element, chunks)
            self._collect_attribute_chunks(element, chunks)
        return chunks

    def reassemble(self, source: str, modified: list[Chunk]) -> str:
        """Re-parse *source* and re-write each chunk's slot in place."""
        if not modified:
            return source
        tree = etree.fromstring(source.encode("utf-8"), self._parser)
        elements = [el for el in tree.iter() if isinstance(el.tag, str)]
        path_index = {self._element_path(el): el for el in elements}
        for chunk in modified:
            if not isinstance(chunk.locator, tuple):
                raise ValueError(f"Invalid xml chunk locator: {chunk.locator!r}")
            path, slot, attr_name = chunk.locator
            element = path_index.get(path)
            if element is None:
                continue
            new_value = chunk.text
            if slot == "text":
                element.text = new_value
            elif slot == "tail":
                element.tail = new_value
            elif slot == "attr":
                element.set(attr_name, new_value)
            else:
                raise ValueError(f"Unknown xml chunk slot: {slot!r}")
        return str(etree.tostring(tree, encoding="unicode"))

    @staticmethod
    def _collect_text_chunks(element: Any, chunks: list[Chunk]) -> None:
        path = XmlPreprocessor._element_path(element)
        if element.text is not None and element.text.strip():
            chunks.append(Chunk(text=element.text, locator=(path, "text", None)))
        if element.tail is not None and element.tail.strip():
            chunks.append(Chunk(text=element.tail, locator=(path, "tail", None)))

    @staticmethod
    def _collect_attribute_chunks(element: Any, chunks: list[Chunk]) -> None:
        path = XmlPreprocessor._element_path(element)
        for attr_name, value in sorted(element.attrib.items()):
            if not value:
                continue
            chunks.append(Chunk(text=value, locator=(path, "attr", attr_name)))

    @staticmethod
    def _element_path(element: Any) -> str:
        """Return a stable string path identifying *element* in the tree.

        Uses :func:`lxml.etree.ElementTree.getpath` which is XPath-style
        and includes positional predicates for repeated siblings
        (``/root/para[2]``). That makes the path total even when the
        document has no ``id`` attributes.
        """
        return str(element.getroottree().getpath(element))
