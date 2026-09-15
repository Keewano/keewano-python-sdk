"""Shared file helpers.

:func:`atomic_write` is the single implementation used by every durable write in the SDK: either the target file
is fully replaced or it is left untouched, and a failure is always raised (never silently ignored).
"""

from __future__ import annotations

import os


def atomic_write(path: str, data: bytes) -> None:
    """Writes ``data`` to a sibling ``.tmp`` file, fsyncs it, then renames it over ``path``.

    Raises ``OSError`` if the data could not be put in place — callers decide whether that is worth
    logging or propagating, but nobody gets to mistake a failed write for a successful one. Never
    leaves a partial ``.tmp`` behind.
    """
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
