"""Kuckuck — lokale Pseudonymisierung personenbezogener Daten.

The public API is built up module-by-module; the final re-exports live here.
Missing imports during in-tree development are tolerated so partial check-ins
still pass ``pytest --collect-only``.
"""

from kuckuck.config import DEFAULT_KEY_PATH, KuckuckSettings, load_default_key, load_key

__all__ = [
    "DEFAULT_KEY_PATH",
    "KuckuckSettings",
    "load_default_key",
    "load_key",
]
