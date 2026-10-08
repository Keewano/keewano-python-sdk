"""Event engine for the server-side SDK: one process reporting on behalf of many end users.

How it differs from the client :class:`~keewano_sdk.internal.dispatcher.KEventDispatcher`, which owns
one install's single collecting batch:

 * **Per-user aggregation.** Every event names its user. Each user tracked by the process has its own
   collecting batch, so a batch never mixes users (the user id is a batch-level field). Events are
   aggregated per user and the batch is *sealed* only when it is big enough (``_SEAL_BYTES``), old
   enough (``flush_interval``), under memory pressure, or on flush/shutdown — so a user who does a
   handful of things a minute produces one batch, not one batch per event.
 * **Per-user identity.** A batch's install id *is* its user id. Each user gets a fresh data-session id
   the first time the process sees them, and batch numbers count from 0 within that data session. The
   state is forgotten when the user goes idle (``user_idle_timeout``) or is pushed out of the LRU
   (``max_users``); a returning user simply starts a new data session.
 * **Bounded memory.** The collecting batches are capped in total (``max_buffered_bytes``): past the
   cap the oldest buffers are sealed early. Sealed batches waiting for upload are capped too
   (``max_pending_bytes``): past it the oldest are collapsed into BATCH_DROPPED markers, and markers
   themselves are discarded oldest-first if even they do not fit.
 * **Persistent or ephemeral storage.** With a persistent ``work_root`` sealed batches go to disk in a
   per-process work directory (see :mod:`.work_dir_lease`), survive restarts, and leftovers of dead
   processes are taken over.
   Without one (a Kubernetes pod, a read-only filesystem) everything stays in RAM and shutdown makes a
   bounded best-effort attempt to upload it all.
 * **Per-user-ordered upload.** One upload thread sends over a single kept-alive connection (see
   :mod:`.network`), never a user's later batch before an earlier one. A refused batch only holds
   back its own user — the others keep uploading; an unreachable ingress backs everyone off.
 * **Fork-aware.** Threads start lazily, on the first event or flush, so a process that forks before
   reporting (gunicorn ``--preload``) forks thread-free. :meth:`after_fork_in_child` (wired to
   ``os.register_at_fork`` by the facade) gives a forked child (gunicorn/Celery workers) an empty
   engine with fresh locks, again started lazily. The parent keeps — and alone uploads — whatever it
   had collected.

The report API (``report_*``/``add_event_*``) mirrors :class:`KEventDispatcher` method for method, with
the end user's id first, as in the other SDKs; the facade only validates and calls these methods.

Locks: ``_persist_lock`` -> ``_lock`` (``_cond`` wraps ``_lock``); ``_ce_lock`` is taken alone.
Disk and network I/O never happen under ``_lock``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Sequence

from ..item import Item
from . import encoding, guid, serializer, work_dir_lease
from .batch import KBatch
from .custom_event_set import CustomEventSet
from .dispatcher import MAX_BATCH_ATTEMPTS
from .events import KBatchDropReason, KEvents
from .guid import KGuid
from .network import KNetwork, SendResult
from .storage import KStorage

_log = logging.getLogger("keewano_sdk")

#: A user's collecting batch is sealed once it reaches this size (the client's slicing threshold).
_SEAL_BYTES = 50 * 1024
#: Estimated RAM cost of tracking one pending batch, counted against ``max_pending_bytes`` so a queue
#: of tiny drop markers is bounded too.
_RECORD_OVERHEAD = 256
#: How long every uploader backs off after the ingress could not be reached at all.
_UNREACHABLE_BACKOFF_S = 30.0
#: How long a refused batch waits before it is retried (other users keep uploading meanwhile).
_REJECTED_RETRY_S = 30.0
#: How often a persistent engine looks for work directories whose leases expired (dead processes).
_TAKE_OVER_INTERVAL_S = 300.0
#: Upper bound on the maintenance tick; the tick also scales down with a short flush interval.
_MAX_TICK_S = 1.0
#: Most batches sealed per hold of ``_lock`` by a loop that seals many users (age, idle, flush of
#: everyone, memory pressure). A seal costs ~3 us, so this bounds how long a reporting thread can wait
#: behind such a loop to well under a millisecond. Between chunks the loop releases the lock *and
#: yields the GIL* (:func:`_yield_to_waiters`): releasing alone is not enough under CPython, since the
#: sealing thread still holds the GIL and simply re-takes the lock before a waiter can run. Without
#: both, 20k users aging out in the same tick stalled every reporting thread for 20-55 ms.
_SEAL_CHUNK = 256

_BATCH_SUFFIX = ".kwub"
_MAP_SUFFIX = ".map.gz"

#: Outcome of an upload attempt whose batch could not even be loaded (file gone or corrupt).
_LOAD_FAILED = object()


def _yield_to_waiters() -> None:
    """Gives up the GIL so a thread blocked on ``_lock`` (just released) can take it before the
    caller's next chunk re-acquires it. ``sleep(0)`` is the standard CPython yield."""
    time.sleep(0)


class _UserState:
    """What the engine remembers about one end user while they are tracked. Created on the user's
    first event in this process and dropped on eviction (idle timeout or LRU); a returning user gets a
    fresh one, i.e. a new data session. Only touched under ``KServerDispatcher._lock``."""

    __slots__ = ("user_id", "data_session_id", "next_batch_num", "batch", "first_event", "last_active")

    def __init__(self, user_id: KGuid, now: float) -> None:
        #: The end user; also the install id of every batch built for them.
        self.user_id = user_id
        #: This user's data session in this process, minted once and stamped on all of their batches.
        self.data_session_id = guid.new_guid()
        #: Batch number the next sealed batch gets; counts 0, 1, 2, … within the data session.
        self.next_batch_num = 0
        #: The collecting batch the user's events are appended to until it is sealed. None while the user
        #: has nothing collecting: it is created by the first event after a seal, not by the seal
        #: itself, so a burst of seals allocates nothing for users who may never report again (fewer
        #: objects for Python's cycle collector, which is what turns a mass seal into a GC pause).
        self.batch: Optional[KBatch] = None
        #: Monotonic time of the oldest event in ``batch``; drives the ``flush_interval`` seal.
        self.first_event = now
        #: Monotonic time of the user's latest event; drives the LRU order and idle eviction.
        self.last_active = now


class _Pending:
    """A sealed batch waiting for upload: held in RAM (``batch``) or on disk (``filename``), never both.
    Lives in ``KServerDispatcher._pending`` until accepted, unreadable, or discarded by the cap. Only
    touched under ``_lock``, except by the thread that reserved it via ``in_flight``."""

    __slots__ = (
        "seq",
        "user_id",
        "size",
        "batch",
        "filename",
        "attempts",
        "not_before",
        "in_flight",
        "is_marker",
        "drain_tried",
    )

    def __init__(self, seq: int, user_id: KGuid, size: int, batch: Optional[KBatch], filename: Optional[str]) -> None:
        #: Queue position (and the file-name prefix on disk); increasing in seal order.
        self.seq = seq
        #: Whose batch this is; uploads keep each user's batches in ``seq`` order.
        self.user_id = user_id
        #: Bytes on disk (or of event data in RAM), counted against ``max_pending_bytes``.
        self.size = size
        #: The batch itself when kept in RAM (ephemeral mode, or a failed disk write); else None.
        self.batch = batch
        #: The ``.kwub`` file holding the batch when persisted; else None.
        self.filename = filename
        #: Consecutive refusals; at ``MAX_BATCH_ATTEMPTS`` the events are replaced by a drop marker.
        self.attempts = 0
        #: Monotonic time before which a refused batch is not retried.
        self.not_before = 0.0
        #: Reserved by a thread (being uploaded or rewritten); nobody else may touch it meanwhile.
        self.in_flight = False
        #: Already collapsed into a BATCH_DROPPED marker; never collapsed again, discarded first.
        self.is_marker = False
        #: Already attempted during the shutdown drain, which gives every batch a single attempt.
        self.drain_tried = False


def _new_batch(user_id: KGuid, data_session_id: KGuid, ce_version: int) -> KBatch:
    """An empty collecting batch for one user's data session, stamped with the custom-event version.
    The server SDK has no device install, so the batch's install id is its user id."""
    b = KBatch(user_id, user_id, data_session_id)
    b.custom_events_version = ce_version
    return b


def _batch_file_name(seq: int, user_id: KGuid) -> str:
    """``<seq>_<user id hex>.kwub``. The user id rides in the name so the queue can be rebuilt (with
    per-user ordering) without opening every file at startup."""
    return f"{seq}_{user_id.to_bytes().hex()}{_BATCH_SUFFIX}"


def _parse_batch_file_name(name: str):
    """``(seq, user_id)`` from a name written by :func:`_batch_file_name`, or None for any other file
    (so stray or foreign files in a work directory are ignored rather than uploaded)."""
    if not name.endswith(_BATCH_SUFFIX):
        return None
    parts = name[: -len(_BATCH_SUFFIX)].split("_")
    if len(parts) != 2 or len(parts[1]) != 32:
        return None
    try:
        return int(parts[0]), guid.from_bytes(bytes.fromhex(parts[1]))
    except ValueError:
        return None


class KServerDispatcher:
    """The server-side event engine: collects events per end user, seals them into per-user batches,
    keeps those in RAM or on disk, and uploads them in the background (see the module docstring for the
    design). One instance per process, owned by :mod:`keewano_sdk.server_sdk`.

    Usage: construct it (nothing starts), call the ``report_*``/``add_event_*`` methods from any thread
    with already-validated arguments, optionally :meth:`flush`, and finally :meth:`shutdown`. The first
    report or flush starts the background threads. After ``fork()`` call :meth:`after_fork_in_child` in
    the child before anything else.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        app_secret: str,
        sdk_version: str,
        work_root: Optional[str],
        flush_interval: float,
        user_idle_timeout: float,
        max_users: int,
        max_buffered_bytes: int,
        max_pending_bytes: int,
        proxy_auth_bearer: Optional[str] = None,
        custom_event_set: Optional[CustomEventSet] = None,
        test_user_name: Optional[str] = None,
        network=None,
    ) -> None:
        """Builds an idle engine; no thread is started and no file is touched until the first event or
        :meth:`flush`.

        :param endpoint: Ingress base URL (``/in`` and ``/custom`` are appended by the transport).
        :param app_secret: The project API key, sent as ``K-Token``.
        :param sdk_version: Reported as ``K-SDK: Python/<version>``.
        :param work_root: Directory under which this process leases a ``worker-N`` work directory for
            persisted batches; None for ephemeral mode (everything in RAM).
        :param flush_interval: Seconds a user's oldest buffered event may wait before their batch is sealed.
        :param user_idle_timeout: Seconds without events after which a user (and data session) is forgotten.
        :param max_users: Most users tracked at once; beyond it the least recently active is evicted.
        :param max_buffered_bytes: Budget for collecting batches; beyond it the oldest are sealed early.
        :param max_pending_bytes: Budget for sealed, unsent batches; beyond it the oldest become drop markers.
        :param proxy_auth_bearer: Optional ``Authorization: Bearer`` token for a test ingress behind a proxy.
        :param custom_event_set: The codegen's custom-event definitions (None for none).
        :param test_user_name: Sent as ``K-Tester`` on every upload, marking all of it as test data.
        :param network: A transport to use instead of :class:`KNetwork` (tests).
        """
        # --- Configuration (fixed for the engine's lifetime) ---
        # Where work directories are leased; None => ephemeral (RAM only). Reset to None if the
        # directory turns out unusable, which switches the engine to RAM for the rest of its life.
        self._work_root = work_root
        self._flush_interval = flush_interval
        self._user_idle_timeout = user_idle_timeout
        self._max_users = max_users
        self._max_buffered_bytes = max_buffered_bytes
        self._max_pending_bytes = max_pending_bytes
        # Definitions whose version is stamped on every new batch and registered before the first
        # upload that needs it; version 0 (the empty set) means "no custom events".
        self._ce_set = custom_event_set if custom_event_set is not None else CustomEventSet()
        self._test_user_name = test_user_name
        # The kept-alive HTTP transport; used only by the upload thread.
        self._network = (
            network if network is not None else KNetwork(endpoint, app_secret, proxy_auth_bearer, sdk_version)
        )
        # Maintenance-thread period: at most 1 s, shorter for short flush intervals so seals are timely.
        self._tick = max(0.01, min(_MAX_TICK_S, flush_interval / 4.0))
        # The custom-event version the backend is confirmed to have. Guarded by _ce_lock. Survives a
        # fork: it describes the backend, not this process.
        self._ce_map_version = 0
        # Set by shutdown(): new events are refused and the engine never (re)starts. Survives a fork.
        self._closed = False
        self._reset_runtime_state()

    def _reset_runtime_state(self) -> None:
        """(Re)creates everything that belongs to one process: locks, users, queues, the work-directory
        lease and the thread bookkeeping. Called at construction and again in a forked child, where the
        copies inherited from the parent must not be used."""
        # --- Synchronization ---
        # Guards all mutable state below except where noted. Held only for in-memory work.
        self._lock = threading.Lock()
        # Wraps _lock; the upload thread and shutdown() wait on it for new or finished batches.
        self._cond = threading.Condition(self._lock)
        # Serializes disk work (persisting sealed batches, leasing, taking over expired directories)
        # so batches enter the queue in seal order. Taken before _lock, never inside it.
        self._persist_lock = threading.Lock()
        # Serializes custom-event registration (and guards _ce_map_version).
        self._ce_lock = threading.Lock()
        # Wakes the maintenance thread early (something was sealed, flushed or shut down).
        self._wake_maintenance = threading.Event()

        # --- Aggregation ---
        # Tracked users, least recently active first (LRU eviction and idle timeout read the front).
        self._users: "OrderedDict[KGuid, _UserState]" = OrderedDict()
        # Users whose collecting batch is non-empty, oldest first event first (age and memory seals
        # read the front).
        self._aging: "OrderedDict[KGuid, _UserState]" = OrderedDict()
        # Bytes in all collecting batches; capped by max_buffered_bytes.
        self._buffered_bytes = 0
        # Something was sealed since the threads were last woken (see _wake_after_seal_locked).
        self._sealed_unannounced = False

        # --- Sealed batches ---
        # Sealed batches awaiting their disk write (persistent mode only), in seal order.
        self._to_persist: List[KBatch] = []
        self._to_persist_bytes = 0
        # Batches awaiting upload, keyed by seq; dicts keep insertion (= seal) order.
        self._pending: Dict[int, _Pending] = {}
        # Size of _pending including per-record overhead; capped by max_pending_bytes.
        self._pending_bytes = 0
        # Next queue position / file-name prefix. Seeded past any leftovers found on disk.
        self._next_seq = 0
        # Monotonic time until which nothing is uploaded, after the ingress was unreachable.
        self._backoff_until = 0.0

        # --- Lifecycle ---
        # This process's leased work directory (persistent mode, once leased); None otherwise.
        self._lease: Optional[work_dir_lease.WorkDirLease] = None
        # The maintenance and upload threads, once started.
        self._threads: List[threading.Thread] = []
        # Threads started (by the first event or flush).
        self._started = False
        # Threads must exit (set at the end of shutdown()).
        self._cancelled = False
        # shutdown() is draining: back-off and retry delays are ignored, one attempt per batch.
        self._draining = False
        # Monotonic time of the last look for expired work directories to take over.
        self._last_take_over = 0.0

    # --- Lifecycle ------------------------------------------------------------------------------

    @property
    def persistent(self) -> bool:
        """True while sealed batches are written to disk; False in ephemeral mode, or once the data
        directory proved unusable and the engine fell back to RAM.
        """
        return self._work_root is not None

    def start(self) -> None:
        """Starts the background threads (which lease a work directory and recover leftovers when
        persistent). Idempotent, and a no-op after :meth:`shutdown`. Not needed before reporting: the
        first event or flush starts the engine, which keeps a process that forks workers before
        reporting anything (gunicorn ``--preload``) free of threads at fork time — forking while a
        thread is inside TLS or DNS code can deadlock the child."""
        with self._lock:
            self._ensure_started_locked()

    def _ensure_started_locked(self) -> None:
        """Starts the maintenance and upload threads once. Caller holds ``_lock``."""
        if self._started or self._closed:
            return
        self._started = True
        self._last_take_over = time.monotonic()
        for name, target in (("Keewano-Server", self._maintenance_loop), ("Keewano-Upload", self._upload_loop)):
            t = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(t)
            t.start()

    def _ensure_lease(self) -> None:
        """Leases this process's work directory, queues the batches a previous owner left in it, saves
        the custom-event set next to them, and takes over expired directories (persistent mode only).
        Runs on the maintenance thread (or the shutdown caller) under ``_persist_lock``; idempotent. If
        the directory cannot be used the engine switches to RAM instead of failing."""
        root = self._work_root
        if root is None or self._lease is not None or self._cancelled:
            return
        try:
            os.makedirs(root, exist_ok=True)
            lease = work_dir_lease.acquire(root)
        except Exception:
            # A disk we cannot use must not take the SDK down with it: keep batches in RAM instead.
            _log.exception("Cannot use the Keewano data directory %s; keeping batches in memory.", root)
            self._work_root = None
            return
        self._lease = lease
        for _, path in work_dir_lease.list_files(lease.path, ".tmp"):
            work_dir_lease.remove_quietly(path)  # a write torn by a crash; its batch never made the queue
        found = []
        for name, path in work_dir_lease.list_files(lease.path, _BATCH_SUFFIX):
            parsed = _parse_batch_file_name(name)
            if parsed is None:
                continue
            try:
                found.append((parsed[0], parsed[1], path, os.path.getsize(path)))
            except OSError:
                continue
        found.sort(key=lambda f: f[0])
        with self._lock:
            for seq, uid, path, size in found:
                self._add_pending_locked(_Pending(seq, uid, size, None, path))
            if found:
                self._next_seq = max(self._next_seq, found[-1][0] + 1)
            self._cond.notify_all()
        if found:
            _log.info("Recovered %d unsent Keewano batches from %s.", len(found), lease.path)
        if self._ce_set.version != 0:
            KStorage.save_custom_event_set_to_file(self._map_file(self._ce_set.version), self._ce_set)
        self._take_over_expired()

    def shutdown(self, timeout: float) -> int:
        """Seals every user's events (persisting them in persistent mode), spends up to ``timeout``
        seconds uploading what is queued — one attempt per batch — then stops the threads and releases
        the work-directory lease. Returns the number of batches still unsent (on disk when persistent,
        lost when ephemeral). New events are refused from the moment this is called; a second call
        returns 0. Called by ``server_sdk.shutdown()`` (and so at ``atexit``)."""
        with self._lock:
            if self._closed:
                return 0
            self._closed = True
            started = self._started
            # One hold for everyone is fine here: no event can be reported any more (closed).
            for st in list(self._aging.values()):
                self._seal_locked(st)
            self._sealed_unannounced = False  # the drain below wakes the upload thread itself
        if not started:
            return 0
        self._persist_sealed()

        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            # Draining: ignore back-off and retry delays, but give every batch only one attempt so an
            # unreachable ingress cannot hold the exit until the deadline.
            self._draining = True
            self._cond.notify_all()
            while True:
                now = time.monotonic()
                if now >= deadline:
                    break
                busy = any(rec.in_flight for rec in self._pending.values())
                if not busy and self._pick_locked(now) is None:
                    break
                self._cond.wait(min(0.05, deadline - now))
            self._cancelled = True
            remaining = len(self._pending)
            self._cond.notify_all()
        self._wake_maintenance.set()
        join_deadline = time.monotonic() + 0.5
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(max(0.0, join_deadline - time.monotonic()))
        with self._persist_lock:  # not while the maintenance thread is writing into the directory
            if self._lease is not None:
                self._lease.release()
                self._lease = None
        if remaining and not self.persistent:
            _log.warning("Keewano: %d batches could not be uploaded before shutdown and were lost.", remaining)
        return remaining

    def after_fork_in_child(self) -> None:
        """Resets the engine in a freshly forked child; call it before anything else there (the facade
        does, from ``os.register_at_fork``). Locks may have been copied mid-hold by a parent thread
        that does not exist here, and every buffered event belongs to the parent (which will upload
        it), so the child keeps none of it: it closes — without releasing — the inherited lease and
        starts over empty, starting its own threads lazily. The inherited network connection is
        dropped by :mod:`.network`'s own fork hook."""
        if self._lease is not None:
            self._lease.abandon_inherited()  # never release: the lease is the parent's
        self._reset_runtime_state()

    # --- Reporting (app threads) ----------------------------------------------------------------

    def _write(self, user_id: KGuid, encoder, *args) -> None:
        """Appends one event for ``user_id``, encoded by ``encoder(buf, ts, *args)`` (see
        :mod:`.encoding`). Only the report methods below call it, each with its own encoder. Starts the
        engine if needed; silently drops the event after :meth:`shutdown`."""
        with self._lock:
            st = self._user_for_event_locked(user_id)
            if st is not None:
                self._append_locked(st, encoder, *args)
            self._wake_after_seal_locked()

    # The report API mirrors KEventDispatcher (and the other SDKs' dispatchers) method for method, with
    # the end user's id first. Arguments arrive validated by the facade (server_sdk / codegen bridge):
    # these methods do not check them, they only append the event to the user's collecting batch.
    # All are thread-safe, never block on I/O, and are no-ops after shutdown().

    def add_event(self, user_id: KGuid, event_type: int) -> None:
        """An event with no payload (custom events of type "none")."""
        self._write(user_id, encoding.event, event_type)

    def add_event_str(self, user_id: KGuid, event_type: int, s: str) -> None:
        """An event carrying one string (capped at the wire limit by the encoder)."""
        self._write(user_id, encoding.event_str, event_type, s)

    def add_event_uint(self, user_id: KGuid, event_type: int, value: int) -> None:
        """An event carrying an unsigned 32-bit int."""
        self._write(user_id, encoding.event_uint, event_type, value)

    def add_event_int(self, user_id: KGuid, event_type: int, value: int) -> None:
        """An event carrying a signed 32-bit int."""
        self._write(user_id, encoding.event_int, event_type, value)

    def add_event_float(self, user_id: KGuid, event_type: int, value: float) -> None:
        """An event carrying a 32-bit float."""
        self._write(user_id, encoding.event_float, event_type, value)

    def add_event_ushort_pair(self, user_id: KGuid, event_type: int, x: int, y: int) -> None:
        """An event carrying two unsigned 16-bit ints."""
        self._write(user_id, encoding.event_ushort_pair, event_type, x, y)

    def add_event_bool(self, user_id: KGuid, event_type: int, flag: bool) -> None:
        """An event carrying a bool."""
        self._write(user_id, encoding.event_bool, event_type, flag)

    def assign_to_ab_test_group(self, user_id: KGuid, test_name: str, group: str) -> None:
        """The user was assigned to ``group`` (one character, code point 0..255) of A/B test ``test_name``."""
        self._write(user_id, encoding.ab_test_assignment, test_name, group)

    def report_in_app_purchase_usd(self, user_id: KGuid, product_name: str, price_usd_cents: int) -> None:
        """A validated purchase priced in US cents (timestamp, product and price events)."""
        self._write(user_id, encoding.purchase_usd, product_name, price_usd_cents)

    def report_in_app_purchase_local(
        self, user_id: KGuid, product_name: str, localized_price: float, currency_code: str
    ) -> None:
        """A validated purchase priced in a local currency (ISO 4217 ``currency_code``)."""
        self._write(user_id, encoding.purchase_local, product_name, localized_price, currency_code)

    def report_ad_offered(self, user_id: KGuid, placement: str, ad_type: int) -> None:
        """An ad opportunity of type ``ad_type`` (an ``AdType`` value) was offered at ``placement``."""
        self._write(user_id, encoding.ad_offered, placement, ad_type)

    def report_ad_revenue_usd(self, user_id: KGuid, placement: str, revenue_usd_cents: int) -> None:
        """Ad revenue in US cents earned at ``placement``."""
        self._write(user_id, encoding.ad_revenue_usd, placement, revenue_usd_cents)

    def report_ad_revenue_local(
        self, user_id: KGuid, placement: str, localized_revenue: float, currency_code: str
    ) -> None:
        """Ad revenue in a local currency earned at ``placement``."""
        self._write(user_id, encoding.ad_revenue_local, placement, localized_revenue, currency_code)

    def report_subscription_revenue_usd(self, user_id: KGuid, package_name: str, revenue_usd_cents: int) -> None:
        """A subscription billing event in US cents."""
        self._write(user_id, encoding.subscription_revenue_usd, package_name, revenue_usd_cents)

    def report_subscription_revenue_local(
        self, user_id: KGuid, package_name: str, localized_revenue: float, currency_code: str
    ) -> None:
        """A subscription billing event in a local currency."""
        self._write(user_id, encoding.subscription_revenue_local, package_name, localized_revenue, currency_code)

    def report_item_exchange(
        self, user_id: KGuid, exchange_point: str, from_items: Sequence[Item], to_items: Sequence[Item]
    ) -> None:
        """Items exchanged at ``exchange_point``: ``from_items`` deducted, ``to_items`` added."""
        self._write(user_id, encoding.items_exchange, exchange_point, from_items, to_items)

    def report_items_reset(self, user_id: KGuid, location: str, items: Sequence[Item]) -> None:
        """The user's item balances at ``location`` were reset to ``items``."""
        self._write(user_id, encoding.event_str_items, KEvents.ITEMS_RESET, location, items)

    def report_in_app_purchase_items_granted(self, user_id: KGuid, product_id: str, items: Sequence[Item]) -> None:
        """Items granted for purchasing ``product_id``."""
        self._write(user_id, encoding.event_str_items, KEvents.ITEMS_PURCHASED_GRANT, product_id, items)

    def report_ad_items_granted(self, user_id: KGuid, placement: str, items: Sequence[Item]) -> None:
        """Items granted for watching the ad at ``placement``."""
        self._write(user_id, encoding.event_str_items, KEvents.ITEMS_AD_GRANTED, placement, items)

    def report_subscription_items_granted(self, user_id: KGuid, package_name: str, items: Sequence[Item]) -> None:
        """Items granted by the subscription ``package_name``."""
        self._write(user_id, encoding.event_str_items, KEvents.ITEMS_SUBSCRIPTION_GRANTED, package_name, items)

    def report_install_campaign(self, user_id: KGuid, campaign_name: str) -> None:
        """The marketing campaign that acquired the user."""
        self.add_event_str(user_id, KEvents.INSTALL_CAMPAIGN, campaign_name)

    def report_game_language(self, user_id: KGuid, language: str) -> None:
        """The in-app language the user uses."""
        self.add_event_str(user_id, KEvents.GAME_LANG, language)

    def log_error(self, user_id: KGuid, msg: str) -> None:
        """A technical error in the context of the user (message capped at the wire limit)."""
        self.add_event_str(user_id, KEvents.ERROR_MSG, msg)

    def report_onboarding_milestone(self, user_id: KGuid, milestone: str) -> None:
        """An onboarding milestone, sent exactly as given. No occurrence counter is appended to repeats:
        a per-process count would be wrong across restarts and processes, and the backend no longer needs it.
        """
        self.add_event_str(user_id, KEvents.ONBOARDING_MILESTONE, milestone)

    def flush(self, user_id: Optional[KGuid] = None) -> None:
        """Seals the collecting batch of ``user_id`` (or of every user when None) and wakes the upload
        and maintenance threads, so it ships now rather than after ``flush_interval``. Asynchronous.
        Also starts the engine, so it is how a process uploads leftovers before reporting anything. A
        user the engine does not track is ignored."""
        with self._lock:
            self._ensure_started_locked()
            if user_id is not None:
                st = self._users.get(user_id)
                if st is not None:
                    self._seal_locked(st)
                self._wake_after_seal_locked()
                return
            # Everyone: only the batches collecting now, so a busy app cannot keep this loop going.
            remaining = len(self._aging)
        while remaining > 0:
            sealed = self._seal_oldest_in_chunk(lambda st: True, remaining)
            if sealed == 0:
                break
            remaining -= sealed
            _yield_to_waiters()
        self._wake_maintenance.set()

    def _user_for_event_locked(self, user_id: KGuid) -> Optional[_UserState]:
        """The user's state for a new event: refreshed to most recently active, or created (a new data
        session) — evicting, after sealing, the least recently active users beyond ``max_users``. Starts
        the engine if needed. None after shutdown, meaning "drop the event". Caller holds ``_lock``.
        """
        if self._closed:
            return None
        self._ensure_started_locked()
        now = time.monotonic()
        st = self._users.get(user_id)
        if st is None:
            st = _UserState(user_id, now)
            self._users[user_id] = st
            while len(self._users) > self._max_users:
                _, oldest = self._users.popitem(last=False)
                self._seal_locked(oldest)
        else:
            self._users.move_to_end(user_id)
        st.last_active = now
        return st

    def _append_locked(self, st: _UserState, encoder, *args) -> None:
        """Encodes one event into the user's collecting batch with the current timestamp, then applies
        the seal rules that depend on size: the batch reached ``_SEAL_BYTES``, or all collecting batches
        together exceed ``max_buffered_bytes`` (oldest sealed first). Caller holds ``_lock``.
        """
        b = st.batch
        if b is None:  # first event since the last seal (or ever): allocate only now
            b = st.batch = _new_batch(st.user_id, st.data_session_id, self._ce_set.version)
        before = b.data.length
        ts = int(time.time())
        if before == 0:
            b.batch_start_time = ts
            st.first_event = st.last_active
            self._aging[st.user_id] = st
        encoder(b.data, ts, *args)
        self._buffered_bytes += b.data.length - before
        if b.data.length >= _SEAL_BYTES:
            self._seal_locked(st)
        # Memory pressure: ship the oldest aggregations early rather than drop anything. At most one
        # chunk here, so a reporting call stays short; the maintenance thread (woken by
        # _wake_after_seal_locked) seals whatever excess is left.
        sealed = 0
        while self._buffered_bytes > self._max_buffered_bytes and self._aging and sealed < _SEAL_CHUNK:
            self._seal_locked(next(iter(self._aging.values())))
            sealed += 1

    def _seal_locked(self, st: _UserState) -> None:
        """Closes the user's collecting batch — assigning its batch number and end time — and queues it:
        straight into ``_pending`` in ephemeral mode, into ``_to_persist`` for the maintenance thread
        in persistent mode. The user is left with no collecting batch; their next event creates one in
        the same data session. No-op when nothing is collecting. Caller holds ``_lock``."""
        b = st.batch
        if b is None or b.data.length == 0:
            return
        self._aging.pop(st.user_id, None)
        self._buffered_bytes -= b.data.length
        b.batch_num = st.next_batch_num
        st.next_batch_num += 1
        b.batch_end_time = int(time.time())
        st.batch = None
        if self.persistent:
            self._to_persist.append(b)
            self._to_persist_bytes += b.data.length
        else:
            self._add_pending_locked(_Pending(self._next_seq, b.user_id, b.data.length, b, None))
            self._next_seq += 1
        # Waking the threads is left to the caller, once per locked section (_wake_after_seal_locked)
        # rather than once per seal: a loop sealing thousands of users would otherwise pay for it each time.
        self._sealed_unannounced = True

    def _wake_after_seal_locked(self) -> None:
        """Wakes the threads that act on sealed batches, if anything was sealed since the last wake:
        the upload thread (a batch may now be due) and — when it has work to do — the maintenance
        thread: persisting (persistent mode), or finishing a memory-pressure seal or enforcing the
        pending cap. Call once at the end of every locked section that may have sealed. Caller holds
        ``_lock``."""
        if not self._sealed_unannounced:
            return
        self._sealed_unannounced = False
        self._cond.notify_all()
        if (
            self.persistent
            or self._buffered_bytes > self._max_buffered_bytes
            or self._pending_bytes > self._max_pending_bytes
        ):
            self._wake_maintenance.set()

    def _seal_oldest_in_chunk(self, due, limit: int = _SEAL_CHUNK) -> int:
        """Seals, oldest first event first, up to ``min(limit, _SEAL_CHUNK)`` collecting batches for
        which ``due(user_state)`` holds, stopping at the first that is not due, all in one short hold
        of ``_lock``. Returns how many were sealed; loops call it until it returns 0, calling
        :func:`_yield_to_waiters` between chunks, so reporting threads are never held up behind a mass
        seal."""
        sealed = 0
        with self._lock:
            while self._aging and sealed < min(limit, _SEAL_CHUNK):
                st = next(iter(self._aging.values()))
                if not due(st):
                    break
                self._seal_locked(st)
                sealed += 1
            self._wake_after_seal_locked()
        return sealed

    def _forget_idle_in_chunk(self, now: float) -> int:
        """Forgets (after sealing) up to ``_SEAL_CHUNK`` users idle for ``user_idle_timeout``, least
        recently active first, in one short hold of ``_lock``. A later event from a forgotten user
        starts a new data session. Returns how many were forgotten; call until it returns 0."""
        forgotten = 0
        with self._lock:
            while self._users and forgotten < _SEAL_CHUNK:
                st = next(iter(self._users.values()))
                if now - st.last_active < self._user_idle_timeout:
                    break
                del self._users[st.user_id]
                self._seal_locked(st)
                forgotten += 1
            self._wake_after_seal_locked()
        return forgotten

    # --- Maintenance thread ---------------------------------------------------------------------

    def _maintenance_loop(self) -> None:
        """Body of the ``Keewano-Server`` thread: leases the work directory, then runs :meth:`_maintain`
        every ``_tick`` (or sooner when woken) until shutdown cancels it.
        """
        with self._persist_lock:
            self._ensure_lease()
        while True:
            self._wake_maintenance.wait(self._tick)
            self._wake_maintenance.clear()
            with self._lock:
                if self._cancelled:
                    return
            try:
                self._maintain()
            except Exception:
                _log.exception("Keewano maintenance pass failed.")

    def _maintain(self) -> None:
        """One maintenance pass: seals batches older than ``flush_interval``, forgets users idle longer
        than ``user_idle_timeout`` (sealing their batch), seals any excess over ``max_buffered_bytes``,
        persists what was sealed, enforces ``max_pending_bytes``, and periodically takes over expired
        work directories. Sealing goes in chunks of ``_SEAL_CHUNK``, releasing ``_lock`` in between.
        """
        now = time.monotonic()
        # Every sealing loop goes chunk by chunk, releasing _lock and yielding in between (see _SEAL_CHUNK).
        while self._seal_oldest_in_chunk(lambda st: now - st.first_event >= self._flush_interval):
            _yield_to_waiters()
        while self._forget_idle_in_chunk(now):
            _yield_to_waiters()
        # Whatever memory-pressure excess a reporting call left (it seals at most one chunk itself).
        # Bounded by the batches collecting now, so an app reporting above the cap cannot keep this
        # pass from reaching its other duties.
        with self._lock:
            remaining = len(self._aging)
        while remaining > 0:
            sealed = self._seal_oldest_in_chunk(lambda st: self._buffered_bytes > self._max_buffered_bytes, remaining)
            if sealed == 0:
                break
            remaining -= sealed
            _yield_to_waiters()
        with self._lock:
            take_over = self.persistent and now - self._last_take_over >= _TAKE_OVER_INTERVAL_S
        self._persist_sealed()
        self._reduce_pending()
        if take_over:
            with self._persist_lock:
                self._last_take_over = now
                self._take_over_expired()

    def _persist_sealed(self) -> None:
        """Writes sealed batches to disk and queues them for upload (persistent mode; a no-op when
        nothing is waiting). A batch whose write fails is queued in RAM instead of being lost.
        Serialized by ``_persist_lock`` so batches enter the queue in seal order; disk I/O happens
        outside ``_lock``. Called by the maintenance thread and by :meth:`shutdown`."""
        with self._persist_lock:
            self._ensure_lease()
            with self._lock:
                batches, self._to_persist = self._to_persist, []
                self._to_persist_bytes = 0
                lease = self._lease
            if not batches:
                return
            written = []
            for b in batches:
                with self._lock:
                    seq = self._next_seq
                    self._next_seq += 1
                path = os.path.join(lease.path, _batch_file_name(seq, b.user_id)) if lease is not None else None
                size = serializer.save_to_file(b, path) if path is not None else 0
                if size > 0:
                    written.append(_Pending(seq, b.user_id, size, None, path))
                else:
                    # Disk full / gone: keep it in RAM rather than lose it.
                    written.append(_Pending(seq, b.user_id, b.data.length, b, None))
            with self._lock:
                for rec in written:
                    self._add_pending_locked(rec)
                self._cond.notify_all()

    def _add_pending_locked(self, rec: _Pending) -> None:
        """Queues ``rec`` for upload and counts it against ``max_pending_bytes``. Caller holds ``_lock``."""
        self._pending[rec.seq] = rec
        self._pending_bytes += rec.size + _RECORD_OVERHEAD

    def _remove_pending_locked(self, rec: _Pending) -> None:
        """Removes ``rec`` from the upload queue and its budget (the caller deletes any file). Caller holds
        ``_lock``.
        """
        if self._pending.pop(rec.seq, None) is not None:
            self._pending_bytes -= rec.size + _RECORD_OVERHEAD

    def _reduce_pending(self) -> None:
        """Enforces ``max_pending_bytes``: collapses the oldest batches into drop markers (so the backend
        learns of the gap), and discards the oldest markers outright if the markers alone still do not
        fit. Batches being uploaded are skipped. Victims are reserved under ``_lock`` and rewritten
        outside it. Called by the maintenance thread."""
        with self._lock:
            excess = self._pending_bytes - self._max_pending_bytes
            if excess <= 0:
                return
            victims = []
            for rec in self._pending.values():
                if excess <= 0:
                    break
                if rec.in_flight or rec.is_marker:
                    continue
                rec.in_flight = True  # reserve it: no upload may start while it is being rewritten
                victims.append(rec)
                excess -= rec.size
        converted = 0
        for rec in victims:
            new_size = self._make_marker(rec)
            with self._lock:
                converted += self._apply_marker_result_locked(rec, new_size)
        with self._lock:
            if converted:
                _log.error("Keewano pending-batch cap reached; dropped the events of %d batches.", converted)
            # Markers are tiny, but a long enough outage can still produce more than fit.
            for rec in list(self._pending.values()):
                if self._pending_bytes <= self._max_pending_bytes:
                    break
                if rec.is_marker and not rec.in_flight:
                    self._remove_pending_locked(rec)
                    work_dir_lease.remove_quietly(rec.filename)
            self._cond.notify_all()

    def _make_marker(self, rec: _Pending) -> Optional[int]:
        """Replaces ``rec``'s events with a BATCH_DROPPED marker, keeping its identity (user, data
        session, batch number, time span). ``rec`` must be reserved (``in_flight``) by the caller, who
        then applies the result with :meth:`_apply_marker_result_locked`.

        Returns the record's new size once it is a marker; -1 if the batch is unreadable (the caller
        drops the record and its file); or None if nothing changed. None happens only when a batch on
        disk can be neither rewritten nor deleted: the record is then left exactly as it was (the full
        batch, still on disk), because a marker in RAM next to the untouched old file would leave two
        versions of one batch — the file orphaned in this process, and uploaded alongside the marker
        by whichever process next finds it.
        """
        b = rec.batch
        if b is None:
            b = KBatch(guid.EMPTY, guid.EMPTY, guid.EMPTY)
            if serializer.load_from_file(rec.filename, b) < 0:
                return -1
        b.data.set_length(0)
        b.cut_positions.clear()
        encoding.batch_dropped(b.data, b.batch_start_time, KBatchDropReason.TOO_MANY_UNSENT_EVENTS)
        # A marker carries no custom events, so it must not wait on a custom-event registration.
        b.custom_events_version = 0
        if rec.batch is not None:
            # In memory batch (working without persistent storage so not writing to disk)
            self._mark_as_marker(rec)
            return b.data.length
        size = serializer.save_to_file(b, rec.filename)
        if size > 0:
            self._mark_as_marker(rec)
            return size
        # The rewrite failed, and atomic_write leaves the original file - the full batch - in place.
        # Remove it so the marker kept in RAM is the only version of this batch. A disk too full for
        # a 10-byte marker can usually still delete a file, which also frees the space.
        try:
            os.remove(rec.filename)
        except FileNotFoundError:
            pass  # already gone: the RAM marker is the only version either way
        except OSError:
            _log.error("Could not rewrite or remove %s; keeping the batch unchanged for now.", rec.filename)
            return None
        rec.batch, rec.filename = b, None
        self._mark_as_marker(rec)
        return b.data.length

    @staticmethod
    def _mark_as_marker(rec: _Pending) -> None:
        """Records that ``rec`` now holds a drop marker. Only once the conversion has really happened."""
        rec.is_marker = True
        rec.attempts = 0

    def _apply_marker_result_locked(self, rec: _Pending, new_size: Optional[int]) -> bool:
        """Applies what :meth:`_make_marker` returned for a reserved ``rec`` and releases the
        reservation: resizes a converted record, drops an unreadable one (with its file), leaves an
        unchanged one as it was. Returns True if the record was converted. Caller holds ``_lock``."""
        rec.in_flight = False
        if new_size is None:
            return False
        if new_size < 0:
            self._remove_pending_locked(rec)
            work_dir_lease.remove_quietly(rec.filename)
            return False
        self._pending_bytes += new_size - rec.size
        rec.size = new_size
        return True

    def _take_over_expired(self) -> None:
        """Takes over the batches of work directories whose lease no live process holds (crashed
        workers, scaled-down replicas), so they are uploaded rather than stranded. Caller holds
        ``_persist_lock``. No-op without a lease."""
        if self._lease is None:
            return
        taken = work_dir_lease.take_over_expired(self._work_root, self._lease, self._take_lease_files)
        if taken:
            _log.info("Took over unsent Keewano batches from %d abandoned work directories.", taken)

    def _take_lease_files(self, src_dir: str) -> None:
        """Moves an abandoned work directory's batches (and custom-event maps) into ours, keeping their
        order, and queues them; called by :func:`work_dir_lease.take_over_expired` while it holds that
        directory's lease. Caller holds ``_persist_lock`` (so sequence numbers enter the queue in order)."""
        own = self._lease.path
        for name, path in work_dir_lease.list_files(src_dir, _MAP_SUFFIX):
            dst = os.path.join(own, name)
            if os.path.exists(dst):
                work_dir_lease.remove_quietly(path)  # same version => same content
            else:
                try:
                    os.replace(path, dst)
                except OSError:
                    pass
        found = []
        for name, path in work_dir_lease.list_files(src_dir, _BATCH_SUFFIX):
            parsed = _parse_batch_file_name(name)
            if parsed is not None:
                found.append((parsed[0], parsed[1], path))
        found.sort(key=lambda f: f[0])
        for _, uid, path in found:
            with self._lock:
                seq = self._next_seq
                self._next_seq += 1
            dst = os.path.join(own, _batch_file_name(seq, uid))
            try:
                os.replace(path, dst)
                size = os.path.getsize(dst)
            except OSError:
                continue
            with self._lock:
                self._add_pending_locked(_Pending(seq, uid, size, None, dst))
                self._cond.notify_all()
        for _, path in work_dir_lease.list_files(src_dir, ".tmp"):
            work_dir_lease.remove_quietly(path)

    # --- Upload threads -------------------------------------------------------------------------

    def _pick_locked(self, now: float) -> Optional[_Pending]:
        """The next batch to upload, or None: the oldest batch of a user who has nothing in flight and
        whose oldest batch is due. A user's later batches are never sent ahead of an earlier one, and a
        refused batch holds back only its own user. Nothing is due during an unreachable back-off;
        while draining, delays are ignored but each batch gets one attempt. Caller holds ``_lock``."""
        if self._cancelled:
            return None
        if not self._draining and now < self._backoff_until:
            return None
        seen = set()
        for rec in self._pending.values():
            if rec.user_id in seen:
                continue
            seen.add(rec.user_id)
            if rec.in_flight:
                continue
            if self._draining:
                if rec.drain_tried:
                    continue
            elif rec.not_before > now:
                continue
            return rec
        return None

    def _idle_wait_locked(self, now: float) -> float:
        """How long the upload thread may sleep when nothing is due: until the back-off ends or the
        next refused batch may be retried, at most ``_MAX_TICK_S`` (new batches wake it through
        ``_cond`` anyway). Caller holds ``_lock``."""
        if self._draining:
            return 0.05
        if self._backoff_until > now:
            return max(0.01, self._backoff_until - now)
        due = min((rec.not_before for rec in self._pending.values() if rec.not_before > now), default=None)
        return _MAX_TICK_S if due is None else max(0.01, min(_MAX_TICK_S, due - now))

    def _upload_loop(self) -> None:
        """Body of the ``Keewano-Upload`` thread. Closes the kept-alive connection when the thread ends."""
        try:
            self._upload_until_cancelled()
        finally:
            self._network.close()  # release the kept-alive connection; this thread was its only user

    def _upload_until_cancelled(self) -> None:
        """Uploads batches one at a time until cancelled: picks the next due batch, reserves it, sends it
        outside the lock, then applies the outcome — accepted (or unreadable): removed; refused: retried
        after ``_REJECTED_RETRY_S`` and replaced by a drop marker after ``MAX_BATCH_ATTEMPTS``;
        unreachable: everyone backs off for ``_UNREACHABLE_BACKOFF_S``.
        """
        while True:
            with self._cond:
                rec = None
                while not self._cancelled:
                    now = time.monotonic()
                    rec = self._pick_locked(now)
                    if rec is not None:
                        break
                    self._cond.wait(self._idle_wait_locked(now))
                if rec is None:
                    return
                rec.in_flight = True
                if self._draining:
                    rec.drain_tried = True
            try:
                result = self._upload(rec)
            except Exception:
                _log.exception("Keewano upload failed unexpectedly.")
                result = SendResult.UNREACHABLE
            replace_with_marker = False
            done_file = None
            with self._cond:
                now = time.monotonic()
                if result is SendResult.ACCEPTED or result is _LOAD_FAILED:
                    self._remove_pending_locked(rec)
                    done_file = rec.filename
                elif result is SendResult.REJECTED:
                    rec.attempts += 1
                    rec.not_before = now + _REJECTED_RETRY_S
                    replace_with_marker = rec.attempts >= MAX_BATCH_ATTEMPTS
                else:
                    self._backoff_until = now + _UNREACHABLE_BACKOFF_S
                if not replace_with_marker:
                    rec.in_flight = False
                self._cond.notify_all()
            work_dir_lease.remove_quietly(done_file)  # outside the lock; the record is already gone
            if replace_with_marker:
                # The ingress keeps recoverable failures non-2xx, so a refusal means "retry" — but a
                # batch that is never accepted must not hold its user's queue forever.
                _log.error("A Keewano batch was refused %d times; dropping its events.", rec.attempts)
                new_size = self._make_marker(rec)
                with self._cond:
                    # Unchanged (could not rewrite the file): the batch stays as it is, and its next
                    # refusal - still past the attempt budget - tries the marker again.
                    self._apply_marker_result_locked(rec, new_size)
                    self._cond.notify_all()
            if result is _LOAD_FAILED:
                _log.error("Keewano batch %s could not be read; dropping it.", done_file)

    def _upload(self, rec: _Pending):
        """Sends one batch: loads it (from RAM or disk), sets its install id to its user id, makes sure
        the backend knows its custom-event version, and posts it. Returns a :class:`SendResult`, or
        ``_LOAD_FAILED`` when the file is gone or corrupt. A missing local custom-event definition is
        reported as REJECTED so the batch is eventually dropped instead of blocking forever.
        """
        b = rec.batch
        if b is None:
            b = KBatch(guid.EMPTY, guid.EMPTY, guid.EMPTY)
            if serializer.load_from_file(rec.filename, b) < 0:
                return _LOAD_FAILED
        # The file format carries no install id; for a server batch it is the user id by definition.
        b.install_id = b.user_id
        ce_ok = self._ensure_custom_events_registered(b.custom_events_version)
        if ce_ok is None:
            return SendResult.REJECTED  # no local definition for this version: retry, then drop
        if not ce_ok:
            return SendResult.UNREACHABLE
        return self._network.send_batch(b, self._test_user_name)

    def _ensure_custom_events_registered(self, ce_version: int) -> Optional[bool]:
        """Makes sure the backend has the custom-event mapping for ``ce_version``, registering it from
        the configured set (or a map file in the work directory) when the backend asks for it. True
        when the mapping is confirmed (or ``ce_version`` is 0); False when that could not be confirmed
        right now; None when this process has no definition set for the version at all. Remembers the
        last confirmed version so the backend is asked once per version."""
        if ce_version == 0:
            return True
        with self._ce_lock:
            if ce_version == self._ce_map_version:
                return True
            lookup = self._network.get_custom_event_ids(ce_version)
            has_mapping = lookup.has_mapping
            if lookup.need_to_register:
                ce_set = self._ce_set if ce_version == self._ce_set.version else None
                if ce_set is None and self._lease is not None:
                    ce_set = KStorage.load_custom_event_set_from_file(self._map_file(ce_version))
                if ce_set is None:
                    _log.error("No custom-event definitions for version %d; cannot upload its batches.", ce_version)
                    return None
                has_mapping = self._network.register_custom_events(ce_set.version, ce_set.event_count, ce_set.gzip_data)
            if not has_mapping:
                return False
            self._ce_map_version = ce_version
            return True

    def _map_file(self, version: int) -> str:
        """Path of the persisted custom-event set for ``version`` in our work directory. Needs a lease."""
        return os.path.join(self._lease.path, f"{version}{_MAP_SUFFIX}")

    # --- Introspection (tests, diagnostics) -----------------------------------------------------

    def stats(self) -> Dict[str, int]:
        """A snapshot for tests and diagnostics: tracked users, bytes being aggregated, and the number and
        size of sealed batches not yet uploaded.
        """
        with self._lock:
            return {
                "users": len(self._users),
                "buffered_bytes": self._buffered_bytes,
                "pending_batches": len(self._pending) + len(self._to_persist),
                "pending_bytes": self._pending_bytes + self._to_persist_bytes,
            }
