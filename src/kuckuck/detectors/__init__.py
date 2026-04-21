"""Detector plugins and span-resolution logic."""

from kuckuck.detectors.base import Detector, EntityType, Span
from kuckuck.detectors.denylist import DenylistDetector
from kuckuck.detectors.email import EmailDetector
from kuckuck.detectors.handle import HandleDetector
from kuckuck.detectors.phone import PhoneDetector
from kuckuck.detectors.resolver import resolve_spans

__all__ = [
    "Detector",
    "DenylistDetector",
    "EmailDetector",
    "EntityType",
    "HandleDetector",
    "PhoneDetector",
    "Span",
    "resolve_spans",
]
