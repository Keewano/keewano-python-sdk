"""Resolves the default per-user application-data directory for durable SDK state.

Device SDKs persist across restarts, so - unlike a server relay that may use a temp dir - the
default location must survive reboots. We pick the conventional per-OS user data directory
using only the standard library, and fall back to ``~/.keewano`` when the environment is unusual.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys


def default_data_dir(unique_dir_seed: str) -> str:
    """A stable, per-user directory for Keewano's identifiers, consent and pending batches.

    When ``unique_dir_seed`` is non-blank a hashed subdirectory is appended, so distinct apps on the
    same host (which, unlike sandboxed mobile apps, share one home directory) get isolated state
    rather than colliding on a single install identity and batch queue.
    """
    home = os.path.expanduser("~")

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.join(home, "AppData", "Local")
        base = os.path.join(base, "Keewano")
    elif sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support", "Keewano")
    else:
        # Linux / other POSIX: honour the XDG base-directory spec.
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        base = os.path.join(base, "keewano")

    if unique_dir_seed and unique_dir_seed.strip():
        base = os.path.join(base, _dir_name_from_seed(unique_dir_seed))

    return base


def _dir_name_from_seed(seed: str) -> str:
    """A stable, filesystem- and shell-safe directory name derived from a seed (e.g. the API key).

    The seed is one-way hashed, so the name reveals nothing recoverable about it. BLAKE2b is used
    rather than MD5: it is never FIPS-gated (MD5 can raise on hardened builds) and is not flagged by
    security linters. The digest is URL-safe base64 encoded, whose alphabet (``A-Za-z0-9-_``) is safe
    on every filesystem and shell, so no character sanitization is needed.
    """
    digest = hashlib.blake2b(seed.strip().encode("utf-8"), digest_size=16).digest()  # 128 bits
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")  # 22 chars, no unsafe chars
