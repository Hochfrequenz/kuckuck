"""Format-aware preprocessors.

Each preprocessor turns a structured input format into the chunk-list
the core pseudonymize pipeline expects, and reassembles the document
after the chunks come back rewritten.
"""

from kuckuck.preprocessors.base import Chunk, Preprocessor
from kuckuck.preprocessors.eml import EmlPreprocessor
from kuckuck.preprocessors.markdown import MarkdownPreprocessor
from kuckuck.preprocessors.msg import MsgPreprocessor
from kuckuck.preprocessors.text import TextPreprocessor
from kuckuck.preprocessors.xml import XmlPreprocessor

__all__ = [
    "Chunk",
    "EmlPreprocessor",
    "MarkdownPreprocessor",
    "MsgPreprocessor",
    "Preprocessor",
    "TextPreprocessor",
    "XmlPreprocessor",
]
