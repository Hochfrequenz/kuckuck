"""Config loading and key lookup for Kuckuck.

The lookup order for ``.kuckuck-key`` (highest to lowest precedence):

1. Explicit path passed to :func:`load_key` (e.g. via the CLI ``--key-file`` flag)
2. Environment variable ``KUCKUCK_KEY_FILE`` (also resolved from a ``.env`` file)
3. Project-scoped: ``$PWD/.kuckuck-key``
4. User-scoped (XDG-style): ``~/.config/kuckuck/key``
5. :class:`KeyNotFoundError` with a hint to run ``kuckuck init-key``
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Default user-global path for the master secret. Follows XDG Base Directory Spec.
DEFAULT_KEY_PATH = "~/.config/kuckuck/key"

#: Project-local key file name — looked up in the current working directory.
PROJECT_KEY_NAME = ".kuckuck-key"

#: Environment variable that may override the key file location.
KEY_ENV_VAR = "KUCKUCK_KEY_FILE"

#: Length of the master secret in bytes. 32 bytes = 256 bits, matches SHA-256 block.
KEY_BYTES = 32


class KeyNotFoundError(FileNotFoundError):
    """Raised when no key file can be located through the lookup chain."""


class KuckuckSettings(BaseSettings):
    """Runtime settings, populated from environment variables and ``.env`` files.

    Additional user-facing configuration (default denylist path, token prefix
    overrides, etc.) is added here as the feature set grows.
    """

    model_config = SettingsConfigDict(env_prefix="KUCKUCK_", env_file=".env", extra="ignore")

    key_file: str | None = None


def _candidate_paths(explicit: Path | str | None) -> list[Path]:
    """Return the ordered list of paths to check for the key file."""
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit).expanduser())
        return candidates

    load_dotenv()  # best-effort; missing .env is fine
    env_override = os.environ.get(KEY_ENV_VAR)
    if env_override:
        candidates.append(Path(env_override).expanduser())

    candidates.append(Path.cwd() / PROJECT_KEY_NAME)
    candidates.append(Path(DEFAULT_KEY_PATH).expanduser())
    return candidates


def load_key(path: Path | str | None = None) -> SecretStr:
    """Load the master secret from a file.

    If *path* is ``None``, the lookup chain documented in the module docstring
    is traversed. Raises :class:`KeyNotFoundError` if no file is found or
    :class:`ValueError` if the located file is empty.
    """
    for candidate in _candidate_paths(path):
        if candidate.is_file():
            raw = candidate.read_text(encoding="utf-8").strip()
            if not raw:
                raise ValueError(f"key file is empty: {candidate}")
            return SecretStr(raw)
    searched = ", ".join(str(p) for p in _candidate_paths(path))
    raise KeyNotFoundError(
        f"No Kuckuck key file found. Searched: {searched}. Run 'kuckuck init-key' to create one."
    )


def load_default_key() -> SecretStr:
    """Convenience wrapper for :func:`load_key` with no explicit path."""
    return load_key(None)


def generate_key() -> str:
    """Return a freshly generated hex-encoded master secret."""
    return secrets.token_hex(KEY_BYTES)


def init_key(path: Path | str | None = None, *, overwrite: bool = False) -> Path:
    """Write a new key to *path* (or to :data:`DEFAULT_KEY_PATH` when ``None``).

    Creates parent directories as needed. Refuses to overwrite existing files
    unless *overwrite* is ``True``.
    """
    target = Path(path).expanduser() if path is not None else Path(DEFAULT_KEY_PATH).expanduser()
    if target.exists() and not overwrite:
        raise FileExistsError(f"key file already exists: {target} (pass overwrite=True to replace)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(generate_key() + "\n", encoding="utf-8")
    try:
        target.chmod(0o600)
    except (NotImplementedError, PermissionError):
        # Windows / non-POSIX — best effort; the OS handles access control differently.
        pass
    return target
