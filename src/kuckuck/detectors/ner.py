"""GLiNER-based NER detector for person names.

Why GLiNER and not Flair / spaCy / transformers-NER?
The CLI binary needs to ship a usable model. GLiNER's multilingual
``urchade/gliner_multi-v2.1`` is ~ 1.1 GB on disk (safetensors + pickle
duplicate) and uses ONNX-friendly weights. The PyInstaller NER binary
ends up ~ 300 MB with CPU-only torch instead of the 2+ GB you get with
Flair's German LM bundle. Quality on German person names is comparable
for our use case (signatures, salutations, mention lists).

The detector is intentionally **lazy**:

* :mod:`gliner` is a heavy import (pulls in :mod:`torch`), so we only import
  it inside :meth:`NerDetector.detect` on first use.
* The model is loaded from a local on-disk path written by
  ``kuckuck fetch-model``. We do not fall back to an automatic download from
  the network — air-gapped environments and reproducible CI runs require an
  explicit fetch step. If the model is missing the detector raises
  :class:`NerModelMissingError` so the caller can decide between a soft skip
  (library API) and a hard exit (CLI when ``--ner`` is requested).

Security note: ``GLiNER.from_pretrained`` deserialises pickle from the
weights file. The CLI gates non-default ``--model-id`` values behind an
``--allow-untrusted-model`` flag for that reason - a malicious repo
running the bundled ``pytorch_model.bin`` through ``torch.load`` is
effectively arbitrary code execution.
"""

from __future__ import annotations

from importlib import util as importlib_util
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kuckuck.detectors.base import EntityType, Priority, Span

if TYPE_CHECKING:  # pragma: no cover - typing only
    # The gliner package is an optional dependency; treat its types as Any for
    # mypy --strict so the core install does not require the package.
    GLiNERType = Any
else:  # pragma: no cover - runtime fallback when gliner is not installed
    GLiNERType = Any

#: HuggingFace repo id for the bundled multilingual model.
DEFAULT_MODEL_ID = "urchade/gliner_multi-v2.1"

#: Slug used as the directory name under the cache root.
DEFAULT_MODEL_SLUG = "gliner_multi-v2.1"

#: Default confidence threshold passed to :meth:`GLiNER.predict_entities`.
#: Tuned conservatively — false positives on common nouns hurt more than
#: missed names because the regex detectors already cover handles/emails.
DEFAULT_THRESHOLD = 0.5

#: Labels asked of the model. Keep this list short — GLiNER scales linearly
#: with the number of labels and we only consume the PERSON span here.
DEFAULT_LABELS: tuple[str, ...] = ("person",)


class NerModelMissingError(FileNotFoundError):
    """Raised when the GLiNER model directory does not exist on disk."""


class NerNotInstalledError(ImportError):
    """Raised when the optional ``gliner`` package is not importable."""


def default_cache_root() -> Path:
    """Return the on-disk root for downloaded NER models.

    Lives under ``~/.cache/kuckuck/models/`` so it follows the XDG convention
    used elsewhere for user-scoped Kuckuck state. Callers are expected to
    create the directory themselves before writing into it.
    """
    return Path("~/.cache/kuckuck/models").expanduser()


def default_model_path() -> Path:
    """Return the on-disk directory holding the default multilingual model."""
    return default_cache_root() / DEFAULT_MODEL_SLUG


def is_gliner_installed() -> bool:
    """Return ``True`` when :mod:`gliner` can be imported.

    Uses :func:`importlib.util.find_spec` to avoid the cost (and side
    effects) of actually importing :mod:`torch`.
    """
    return importlib_util.find_spec("gliner") is not None


#: Filenames that indicate a populated model snapshot. We require AT LEAST
#: one config marker AND at least one weights file, so a half-downloaded
#: directory containing only ``config.json`` is correctly reported as
#: unavailable. Without this check ``--ner`` would pass the precheck and
#: then crash deep inside ``GLiNER.from_pretrained`` with an opaque error.
_CONFIG_MARKERS = ("gliner_config.json", "config.json")
_WEIGHT_MARKERS = ("model.safetensors", "pytorch_model.bin", "model.onnx")


def is_model_available(path: Path | None = None) -> bool:
    """Return ``True`` when the model directory exists and looks populated.

    A populated snapshot must contain at least one config marker
    (``gliner_config.json`` or ``config.json``) AND at least one weights
    file (``model.safetensors``, ``pytorch_model.bin`` or ``model.onnx``).
    The dual check guards against the common partial-download case where
    only a small file landed before the network dropped.
    """
    target = path or default_model_path()
    if not target.is_dir():
        return False
    has_config = any((target / name).is_file() for name in _CONFIG_MARKERS)
    has_weights = any((target / name).is_file() for name in _WEIGHT_MARKERS)
    return has_config and has_weights


class NerDetector:
    """Wrap a GLiNER model as a Kuckuck :class:`~kuckuck.detectors.base.Detector`.

    The detector yields ``[[PERSON_...]]`` spans only — emails, phones and
    handles are picked up by their dedicated regex detectors and outrank a
    PERSON match through :class:`~kuckuck.detectors.base.Priority`.
    """

    name = "ner"
    entity_type = EntityType.PERSON
    priority = Priority.PERSON

    def __init__(
        self,
        *,
        model_path: Path | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        labels: tuple[str, ...] = DEFAULT_LABELS,
        model: GLiNERType | None = None,
    ) -> None:
        self._model_path = model_path or default_model_path()
        self._threshold = threshold
        self._labels = list(labels)
        self._model: GLiNERType | None = model

    def _load(self) -> GLiNERType:
        if self._model is not None:
            return self._model
        if not is_gliner_installed():
            raise NerNotInstalledError(
                "The optional 'gliner' package is not installed. Install it via: pip install 'kuckuck[ner]'"
            )
        if not is_model_available(self._model_path):
            raise NerModelMissingError(
                f"GLiNER model not found at {self._model_path}. Run 'kuckuck fetch-model' to download it."
            )
        # pylint: disable-next=import-outside-toplevel,import-error
        from gliner import GLiNER  # type: ignore[import-not-found]

        self._model = GLiNER.from_pretrained(str(self._model_path))
        return self._model

    def detect(self, text: str) -> list[Span]:
        """Return PERSON spans found by the GLiNER model in *text*.

        Empty strings short-circuit so the model is not loaded for trivial
        inputs (cheap when called repeatedly on small chunks).
        """
        if not text.strip():
            return []
        model = self._load()
        predictions: list[dict[str, Any]] = model.predict_entities(text, labels=self._labels, threshold=self._threshold)
        spans: list[Span] = []
        for prediction in predictions:
            start = int(prediction["start"])
            end = int(prediction["end"])
            if start >= end or start < 0 or end > len(text):
                continue
            spans.append(
                Span(
                    start=start,
                    end=end,
                    text=text[start:end],
                    entity_type=self.entity_type,
                    detector_name=self.name,
                    priority=self.priority,
                )
            )
        return spans
