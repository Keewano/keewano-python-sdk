"""Leases on per-process work directories under a shared server data directory.

A server process rarely runs alone: gunicorn and Celery fork workers, and a deployment runs several
replicas. If two processes write batches into the same directory they collide on file names and both
upload what they find, so the backend receives batches twice. Instead of asking the integrator to give
every worker its own ``data_dir``, each process leases the first free ``worker-<N>`` subdirectory: the
lease is an exclusive, non-blocking OS file lock on ``worker-<N>/.lock``, held for as long as the
process keeps the descriptor open.

 * The kernel releases the lock when the process dies, however it dies — so a crashed worker's lease
   simply expires: its directory can be acquired again (and its leftover batches recovered) without
   any stale-lock heuristics.
 * :func:`take_over_expired` lets a running process take over the batches of a directory whose lease
   nobody holds (e.g. after scaling from 8 workers down to 4), so leftovers are not stranded until the
   next restart.

``flock`` locks belong to the *open file description*, which a forked child shares. A child must
therefore never release an inherited lease (that would release it for the parent too); it only closes
its copy of the descriptor (:meth:`WorkDirLease.abandon_inherited`) and acquires a lease of its own.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, List, Optional, Tuple

_log = logging.getLogger("keewano_sdk")

try:  # POSIX
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass

    LOCKING_SUPPORTED = True
except ImportError:  # pragma: no cover - exercised on Windows only
    try:
        import msvcrt

        def _try_lock(fd: int) -> bool:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False

        def _unlock(fd: int) -> None:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass

        LOCKING_SUPPORTED = True
    except ImportError:
        LOCKING_SUPPORTED = False

        def _try_lock(fd: int) -> bool:
            return True

        def _unlock(fd: int) -> None:
            pass


#: Upper bound on work directories probed; far beyond any realistic worker count on one host/volume.
MAX_WORKER_DIRS = 1024

_DIR_PREFIX = "worker-"
_LOCK_NAME = ".lock"


class WorkDirLease:
    """An acquired work directory; the lease (its lock) is held for as long as ``fd`` stays open."""

    __slots__ = ("path", "fd")

    def __init__(self, path: str, fd: int) -> None:
        self.path = path
        self.fd = fd

    def release(self) -> None:
        """Unlocks and closes. Only for the process that acquired the lease — never after a fork."""
        if self.fd >= 0:
            _unlock(self.fd)
            _close(self.fd)
            self.fd = -1

    def abandon_inherited(self) -> None:
        """Closes a descriptor inherited across ``fork`` without unlocking it (the parent keeps it)."""
        if self.fd >= 0:
            _close(self.fd)
            self.fd = -1


def _close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _open_and_lock(work_dir: str) -> int:
    """Returns a locked descriptor for ``work_dir``, or -1 if another process holds its lease."""
    os.makedirs(work_dir, exist_ok=True)
    fd = os.open(os.path.join(work_dir, _LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    if _try_lock(fd):
        return fd
    _close(fd)
    return -1


def acquire(root: str) -> WorkDirLease:
    """Leases the lowest-numbered free work directory under ``root``. Raises ``OSError`` if none can
    be had."""
    if not LOCKING_SUPPORTED:
        _log.warning("No OS file locking on this platform; processes sharing a data_dir may collide.")
    for n in range(MAX_WORKER_DIRS):
        work_dir = os.path.join(root, f"{_DIR_PREFIX}{n}")
        fd = _open_and_lock(work_dir)
        if fd >= 0:
            return WorkDirLease(work_dir, fd)
    raise OSError(f"No free Keewano work directory under {root} (all {MAX_WORKER_DIRS} are leased)")


def _worker_dirs(root: str) -> List[str]:
    try:
        return [
            e.path
            for e in os.scandir(root)
            if e.is_dir() and e.name.startswith(_DIR_PREFIX) and e.name[len(_DIR_PREFIX) :].isdigit()
        ]
    except OSError:
        return []


def take_over_expired(root: str, own: WorkDirLease, take: Callable[[str], None]) -> int:
    """Calls ``take(work_dir)`` for every work directory under ``root`` whose lease no live process
    holds, while holding that lease (so its owner cannot reappear mid-transfer). ``take`` moves the
    directory's files into ``own``. Returns how many directories were taken over."""
    taken = 0
    own_path = os.path.normpath(own.path)
    for work_dir in _worker_dirs(root):
        if os.path.normpath(work_dir) == own_path:
            continue
        try:
            fd = _open_and_lock(work_dir)
        except OSError:
            continue
        if fd < 0:
            continue  # leased by a live process, which uploads its own batches
        try:
            take(work_dir)
            taken += 1
        except Exception:
            _log.exception("Failed to take over leftover batches from %s.", work_dir)
        finally:
            _unlock(fd)
            _close(fd)
    return taken


def list_files(directory: str, suffix: str) -> List[Tuple[str, str]]:
    """``(name, full_path)`` of the regular files in ``directory`` ending in ``suffix``."""
    try:
        return [(e.name, e.path) for e in os.scandir(directory) if e.name.endswith(suffix) and e.is_file()]
    except OSError:
        return []


def remove_quietly(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass
