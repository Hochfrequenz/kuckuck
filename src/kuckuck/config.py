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
import sys
from pathlib import Path

from dotenv import dotenv_values
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

    Currently only exposes the key-file path override. New user-facing
    options (default denylist path, token prefix overrides, …) attach here
    as the feature set grows. Reading ``.env`` via pydantic-settings is
    scoped to this class and does **not** mutate :data:`os.environ` — the
    ``.env`` file stays private to Kuckuck.
    """

    model_config = SettingsConfigDict(env_prefix="KUCKUCK_", env_file=".env", extra="ignore")

    key_file: str | None = None


def _env_override(env_var: str = KEY_ENV_VAR) -> str | None:
    """Look up *env_var* in ``os.environ`` and, if absent, in ``.env``.

    Unlike :func:`dotenv.load_dotenv`, this does **not** mutate
    :data:`os.environ` — so ``.env`` values stay out of subprocesses and
    out of other library code that reads the environment.
    """
    value = os.environ.get(env_var)
    if value:
        return value
    local_env = Path.cwd() / ".env"
    if local_env.is_file():
        return dotenv_values(local_env).get(env_var)
    return None


def _candidate_paths(explicit: Path | str | None) -> list[Path]:
    """Return the ordered list of paths to check for the key file."""
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit).expanduser())
        return candidates

    env_value = _env_override()
    if env_value:
        candidates.append(Path(env_value).expanduser())

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
    raise KeyNotFoundError(f"No Kuckuck key file found. Searched: {searched}. Run 'kuckuck init-key' to create one.")


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

    The secret is written atomically with mode ``0o600`` on POSIX so it is
    never briefly readable by other local users. On Windows the OS access
    model differs — the file inherits the user-profile ACL, which is
    equivalent to user-only access on default installations.
    """
    target = Path(path).expanduser() if path is not None else Path(DEFAULT_KEY_PATH).expanduser()
    if target.exists() and not overwrite:
        raise FileExistsError(f"key file already exists: {target} (pass overwrite=True to replace)")
    target.parent.mkdir(parents=True, exist_ok=True)

    data = (generate_key() + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    mode = 0o600

    if sys.platform == "win32":
        # Windows ignores the `mode` argument for most practical purposes;
        # just use pathlib for simplicity and let the profile ACL protect it.
        target.write_bytes(data)
    else:
        fd = os.open(str(target), flags, mode)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        # Belt-and-braces: if the umask masked out user-only perms on some
        # exotic filesystem, re-assert them now.
        try:
            os.chmod(str(target), mode)
        except (NotImplementedError, PermissionError):
            pass
    return target
