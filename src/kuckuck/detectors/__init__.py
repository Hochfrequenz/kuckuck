"""Detector plugins and span-resolution logic."""

from kuckuck.detectors.base import Detector, EntityType, Span
from kuckuck.detectors.denylist import DenylistDetector
from kuckuck.detectors.email import EmailDetector
from kuckuck.detectors.handle import HandleDetector
from kuckuck.detectors.ner import (
    NerDetector,
    NerModelMissingError,
    NerNotInstalledError,
    default_model_path,
    is_gliner_installed,
    is_model_available,
)
from kuckuck.detectors.phone import PhoneDetector
from kuckuck.detectors.resolver import resolve_spans

__all__ = [
    "Detector",
    "DenylistDetector",
    "EmailDetector",
    "EntityType",
    "HandleDetector",
    "NerDetector",
    "NerModelMissingError",
    "NerNotInstalledError",
    "PhoneDetector",
    "Span",
    "default_model_path",
    "is_gliner_installed",
    "is_model_available",
    "resolve_spans",
]
