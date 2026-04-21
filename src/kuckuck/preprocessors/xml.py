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
        # XXE / billion-laughs hardening: refuse to load DTDs, do not
        # resolve entity references, never touch the network. lxml 5.x
        # leaves these on by default which would let an attacker leak
        # local files through `<!ENTITY xxe SYSTEM "file:///etc/passwd">`.
        # remove_blank_text=False keeps the original whitespace between
        # tags so reassembled output matches the input byte-for-byte
        # outside the chunks we deliberately rewrite. huge_tree=False is
        # the default but stated explicitly so a future maintenance diff
        # can't silently raise the resource limit.
        self._parser = etree.XMLParser(
            strip_cdata=False,
            remove_blank_text=False,
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            huge_tree=False,
        )

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
        # Track elements whose text was originally a CDATA section so we
        # can re-wrap the new text on assignment. lxml drops the CDATA
        # marker on .text = "..." otherwise, which silently breaks
        # Confluence Storage Format macros that depend on it.
        cdata_text_elements = _detect_cdata_text_elements(tree)
        for chunk in modified:
            if not isinstance(chunk.locator, tuple):
                raise ValueError(f"Invalid xml chunk locator: {chunk.locator!r}")
            path, slot, attr_name = chunk.locator
            element = path_index.get(path)
            if element is None:
                continue
            new_value = chunk.text
            if slot == "text":
                if path in cdata_text_elements:
                    element.text = etree.CDATA(new_value)
                else:
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


def _detect_cdata_text_elements(tree: Any) -> set[str]:
    """Return paths of elements whose `.text` is a CDATA section in *tree*.

    lxml exposes CDATA content as a regular `.text` string; the only way
    to spot it is to re-serialise the element and look for the
    ``<![CDATA[`` marker. Done once per reassemble call so the cost is
    bounded by tree size, not chunk count.
    """
    cdata_paths: set[str] = set()
    for element in tree.iter():
        if not isinstance(element.tag, str):
            continue
        if element.text is None:
            continue
        # Only check elements with no children where text could be CDATA.
        # Re-serialising a single element is cheap.
        serialised = etree.tostring(element, encoding="unicode")
        if "<![CDATA[" in serialised:
            cdata_paths.add(str(element.getroottree().getpath(element)))
    return cdata_paths
