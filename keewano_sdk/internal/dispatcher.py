"""Core event-processing engine.

Design:

 * **Double-buffered batching.** Events are appended to ``_in_batch`` under ``_swap_lock``. A
   background thread periodically swaps it with ``_sending_batch``, slices the collected bytes into
   ``.kwub`` sub-batches on disk, and uploads them.
 * **Durable & offline-first.** Pending batches live on disk and survive restarts; storage is capped
   at 50 MB (oldest batches are collapsed into a BATCH_DROPPED marker when exceeded). Collected
   events are flushed to disk at most every ``FLUSH_INTERVAL_MS`` even below the send threshold, so a
   short session ending in a crash does not lose its events.
 * **Consent-aware.** Nothing is uploaded while consent is Pending; a Denied decision discards the
   collected batch, keeps denied events off disk, and purges what is already on disk.
 * **Head-of-queue delivery.** Batches upload oldest-first and stop at the first failure, so ordering
   is preserved. A *refused* batch (vs an unreachable server) is retried up to ``MAX_BATCH_ATTEMPTS``
   times; past that it is replaced with a BATCH_DROPPED marker so one poison batch can't stall the
   queue forever.

Lock order, where more than one is held: ``_flush_lock`` -> ``_queue_lock`` -> ``_swap_lock``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, List, Optional, Sequence

from ..item import Item
from . import encoding, guid, serializer
from .batch import CutPoint, KBatch
from .buffer import KBuffer
from .consent import UserConsentState
from .custom_event_set import CustomEventSet
from .events import KBatchDropReason, KEvents
from .guid import KGuid
from .network import KNetwork, SendResult
from .storage import KStorage

_log = logging.getLogger("keewano_sdk")

_DISK_USAGE_LIMIT = 50 * 1024 * 1024  # 50 MB
_SEND_THRESHOLD = 1024
_BATCH_CUTTING_THRESHOLD = 50 * 1024
_MAX_BATCHES_PER_CYCLE = 30
_BATCH_DROP_THRESHOLD = 70
_IDLE_WAIT_MS = 30_000

#: Upper bound on how long collected events may sit in RAM before being written to disk. Without
#: this, anything under ``_SEND_THRESHOLD`` is persisted only when a batch crosses the threshold or
#: on an explicit flush — so a session ending in an unclean kill (SIGKILL, os._exit, power loss;
#: none caught by the exception hooks) loses its events, including the launch burst.
#:
#: As opposed to mobile SDKs a native Python process has no foreground/background lifecycle
#: to force a flush, so it may idle for a long time, but it also has no OS pressure to keep the
#: interval short. A larger value coalesces a low-rate app's data into fewer, larger batches (less
#: backend work) while still bounding the RAM-loss window and delivering mid-session.
FLUSH_INTERVAL_MS = 180_000

#: How many times the head batch may be refused before its events are dropped and replaced with a
#: BATCH_DROPPED marker. Bounds the damage from a single un-sendable ("poison") batch that would
#: otherwise block the queue behind it indefinitely. In-memory, so a restart gives a fresh budget.
MAX_BATCH_ATTEMPTS = 10

# Re-exported: the caps live with the encoders but are part of this module's long-standing surface.
MAX_ITEMS_PER_EVENT = encoding.MAX_ITEMS_PER_EVENT
MAX_EVENT_STRING_CHARS = encoding.MAX_EVENT_STRING_CHARS


class _AutoResetEvent:
    """Auto-reset signal. ``wait_one(0)`` blocks indefinitely; otherwise the timeout is in ms."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def wait_one(self, timeout_ms: int) -> bool:
        timeout = None if timeout_ms == 0 else timeout_ms / 1000.0
        signaled = self._event.wait(timeout)
        if signaled:
            self._event.clear()
        return signaled


class _PendingBatchInfo:
    __slots__ = ("batch_end_time", "batch_num", "size")

    def __init__(self, batch_end_time: int, batch_num: int, size: int) -> None:
        self.batch_end_time = batch_end_time
        self.batch_num = batch_num
        self.size = size


class KEventDispatcher:
    def __init__(
        self,
        working_directory: str,
        endpoint: str,
        app_secret: str,
        initial_consent: UserConsentState,
        install_id: KGuid,
        initial_user_id: KGuid,
        data_session_id: KGuid,
        sdk_version: str,
        proxy_auth_bearer: Optional[str] = None,
        custom_event_set: Optional[CustomEventSet] = None,
        disk_usage_limit: int = _DISK_USAGE_LIMIT,
    ) -> None:
        # Guards _in_batch/_sending_batch and the fields the app threads share with the send thread.
        # Plain (non-reentrant) Lock: no path re-acquires it while held — the one writer that runs
        # under it, report_onboarding_milestone, calls the lock-free _write_locked.
        self._swap_lock = threading.Lock()
        # Serializes collect-and-persist so the send thread and a synchronous crash-path flush cannot
        # interleave. Only ever held across *local disk* work — never across network I/O.
        self._flush_lock = threading.Lock()
        # Guards _pending_batches and _next_batch_num. Held briefly, never across an upload.
        self._queue_lock = threading.Lock()

        self._ready_to_send = _AutoResetEvent()
        self._cancelled = threading.Event()

        self._in_batch = KBatch(install_id, initial_user_id, data_session_id)
        self._sending_batch = KBatch(install_id, initial_user_id, data_session_id)

        self._frame_timestamp = self._now_seconds()
        self._user_consent_state = initial_consent

        self._install_id = install_id
        self._user_id = initial_user_id
        self._work_folder = working_directory
        self._disk_usage_limit = disk_usage_limit

        self._network = KNetwork(endpoint, app_secret, proxy_auth_bearer, sdk_version)

        # Batch files waiting to be uploaded, oldest first. Guarded by _queue_lock.
        self._pending_batches: List[_PendingBatchInfo] = []
        self._next_batch_num = 0
        self._pending_batches_loaded = False
        # Consecutive refusals of the current head batch. Send-thread-only, so it needs no lock.
        self._head_batch_attempts = 0

        self._onboarding_milestones: Optional[Dict[str, int]] = None
        self._onboarding_dirty = False
        self._test_user_name: Optional[str] = None
        self._send_test_user_name: Optional[str] = None
        self._test_user_name_to_persist: Optional[str] = None

        # The custom-event version the backend is confirmed to have a mapping for. Send-thread-only.
        self._ce_map_version = 0

        try:
            os.makedirs(self._work_folder, exist_ok=True)
        except OSError:
            pass
        # Set _send_test_user_name directly so batch files already on disk that are uploaded at
        # startup (before any swap) are still tagged with the test user.
        self._send_test_user_name = KStorage.load_test_user_name(self._test_user_file_name())

        # Persist the custom-event set (so it survives to registration time) and stamp its version
        # onto both batches. A zero version means "no custom events" and short-circuits all
        # custom-event work in the send path.
        ce_set = custom_event_set if custom_event_set is not None else CustomEventSet()
        if ce_set.version != 0:
            KStorage.save_custom_event_set_to_file(self._custom_event_set_filename(ce_set.version), ce_set)
        self._in_batch.custom_events_version = ce_set.version
        self._sending_batch.custom_events_version = ce_set.version

        self._send_thread = threading.Thread(target=self._send_thread_func, name="Keewano-Send", daemon=True)
        self._send_thread.start()

    # --- Lifecycle ------------------------------------------------------------------------------

    def stop(self) -> None:
        self._cancelled.set()
        self._ready_to_send.set()
        self._send_thread.join(2.0)

    def send_now(self) -> None:
        self._ready_to_send.set()

    def set_user_consent(self, granted: bool) -> UserConsentState:
        """Records the consent decision and returns the resulting state. Withdrawal is honoured from
        **any** state (GDPR Art. 7(3) is the right to withdraw at any time)."""
        with self._swap_lock:
            if granted and self._user_consent_state == UserConsentState.NOT_REQUIRED:
                # The app declared consent isn't required; granting it explicitly changes nothing.
                self._user_consent_state = UserConsentState.NOT_REQUIRED
            elif granted:
                self._user_consent_state = UserConsentState.GRANTED
            else:
                self._user_consent_state = UserConsentState.DENIED
            self._ready_to_send.set()
            return self._user_consent_state

    # --- Background send loop -------------------------------------------------------------------

    def _send_thread_func(self) -> None:
        self._load_pending_batches()
        last_flush = time.monotonic()

        while not self._cancelled.is_set():
            progressing = self._send_pending_batches()
            self._flush_deferred_writes()

            timeout_ms = self._next_wait_millis(progressing)
            signalled = self._ready_to_send.wait_one(timeout_ms)
            if self._cancelled.is_set():
                break

            # A timeout is a flush opportunity too, not just a retry tick: _send_if_needed only
            # signals once a batch reaches _SEND_THRESHOLD, so without this a handful of small events
            # would never reach disk on their own.
            now = time.monotonic()
            if signalled or (now - last_flush) * 1000.0 >= FLUSH_INTERVAL_MS:
                last_flush = now
                self._collect_and_persist(reduce_storage=True)

        # One last pass on the way out, so nothing is stranded in RAM no matter how we left the loop.
        # Two exits reach here and neither flushes on its own: (1) cancelled during the wait, which
        # `break`s before the collect above; (2) cancelled before the loop ever ran (stop() moments
        # after start). The queue was already loaded at the top, so this won't trigger the expensive
        # first-call directory walk.
        self._collect_and_persist(reduce_storage=False)
        self._flush_deferred_writes()
        self._network.close()  # release the kept-alive connection; this thread was its only user

    def _next_wait_millis(self, uploads_progressing: bool) -> int:
        with self._swap_lock:
            has_unflushed_data = self._in_batch.data.length > 0
        with self._queue_lock:
            queue_empty = not self._pending_batches
        consent_pending = self._user_consent_state == UserConsentState.PENDING

        if not uploads_progressing:
            timeout = _IDLE_WAIT_MS
        elif consent_pending:
            timeout = _IDLE_WAIT_MS
        elif not queue_empty:
            timeout = 1
        else:
            timeout = _IDLE_WAIT_MS

        # Never wait past the flush deadline while there is data only held in RAM.
        if has_unflushed_data and timeout > FLUSH_INTERVAL_MS:
            return FLUSH_INTERVAL_MS
        return timeout

    def _collect_and_persist(self, reduce_storage: bool) -> None:
        """Swaps the collecting batch out and writes it to disk. Runs on the send thread every cycle
        and synchronously on the crashing/exiting thread via :meth:`persist_now`."""
        with self._flush_lock:
            # persist_now callers can arrive before the send thread has loaded the queue; loading is
            # idempotent, and loading first is what stops a new batch from being written as number 0
            # over a leftover file from a previous process.
            self._load_pending_batches()
            consent = self._swap_batches()
            if self._sending_batch.data.length == 0:
                return

            # Consent withdrawn (or still Denied): discard rather than persist, so denied events
            # never hit disk. A withdrawal landing after this swap applies to the next batch, and
            # _send_pending_batches purges the disk as soon as it sees Denied.
            if consent == UserConsentState.DENIED:
                self._discard_collected_batch()
                with self._queue_lock:
                    self._purge_pending_batches()
                return

            self._sending_batch.batch_end_time = self._now_seconds()
            over_cap = False
            if reduce_storage:
                with self._queue_lock:
                    over_cap = (
                        self._reduce_storage_size(self._pending_batches, self._disk_usage_limit)
                        >= self._disk_usage_limit
                    )

            if not over_cap:
                self._slice_and_persist()
            else:
                self._persist_drop_marker()

            # A flush driven from another thread just added files; wake the send thread (it may be
            # parked on a 30 s idle wait). Skipped when the send thread is the caller.
            if threading.current_thread() is not self._send_thread:
                self._ready_to_send.set()

    def persist_now(self) -> None:
        """Persists whatever has been collected so far, synchronously, on the calling thread. The
        crash path: uploading can stay asynchronous (next launch); only the write must be synchronous."""
        try:
            self._collect_and_persist(reduce_storage=False)
        except Exception:
            pass

    def _discard_collected_batch(self) -> None:
        self._sending_batch.data.set_length(0)
        self._sending_batch.cut_positions.clear()

    def _purge_pending_batches(self) -> None:
        """Deletes every batch file still on disk. Caller holds _queue_lock."""
        for info in self._pending_batches:
            self._delete_file(self._batch_filename_for(info.batch_end_time, info.batch_num))
        self._pending_batches.clear()
        self._head_batch_attempts = 0

    def _load_pending_batches(self) -> None:
        with self._queue_lock:
            if self._pending_batches_loaded:
                return
            self._pending_batches_loaded = True
            self._pending_batches.extend(self._load_unsent_batches_list(self._work_folder))
            # Seed the counter past anything already on disk, so a new batch cannot land on the same
            # <end>_<num>.kwub filename as a leftover from a previous process and overwrite it — a
            # window that opens exactly during a crash loop.
            self._next_batch_num = (max((b.batch_num for b in self._pending_batches), default=-1)) + 1
            self._reduce_storage_size(self._pending_batches, self._disk_usage_limit)

    def _slice_and_persist(self) -> None:
        """Splits the collected batch on its cut points into self-contained sub-batches on disk.
        Call while holding _flush_lock; the queue append takes _queue_lock itself."""
        persisted: List[_PendingBatchInfo] = []
        with self._queue_lock:
            next_num = self._next_batch_num

        sending = self._sending_batch
        sub_batch = KBatch(sending.install_id, sending.user_id, sending.data_session_id)
        sub_batch.custom_events_version = sending.custom_events_version

        last_read_pos = 0
        slice_start = sending.batch_start_time
        data_buff = sending.data.raw_buffer()

        for cut in sending.cut_positions:
            data_size = cut.pos - last_read_pos
            sub_batch.data.set_length(0)
            sub_batch.batch_num = next_num
            next_num += 1
            sub_batch.batch_start_time = slice_start
            sub_batch.batch_end_time = cut.last_event_time
            sub_batch.data.write_bytes(data_buff, last_read_pos, data_size)

            written = serializer.save_to_file(sub_batch, self._batch_filename(sub_batch))
            persisted.append(_PendingBatchInfo(sub_batch.batch_end_time, sub_batch.batch_num, written))

            last_read_pos = cut.pos
            slice_start = cut.last_event_time

        remaining = sending.data.length - last_read_pos
        if remaining > 0:
            sub_batch.data.set_length(0)
            sub_batch.batch_num = next_num
            next_num += 1
            sub_batch.batch_start_time = slice_start
            sub_batch.batch_end_time = sending.batch_end_time
            sub_batch.data.write_bytes(data_buff, last_read_pos, remaining)

            written = serializer.save_to_file(sub_batch, self._batch_filename(sub_batch))
            persisted.append(_PendingBatchInfo(sub_batch.batch_end_time, sub_batch.batch_num, written))

        with self._queue_lock:
            self._pending_batches.extend(persisted)
            # Safe to assign (not max) because this method runs only on the send thread as well as
            # _persist_drop_marker() which is the only other method modifying _next_batch_num.
            self._next_batch_num = next_num

    def _persist_drop_marker(self) -> None:
        """Replaces the collected batch with a single BATCH_DROPPED marker (over the disk cap).
        called only on the send_thread"""
        with self._queue_lock:
            num = self._next_batch_num
            self._next_batch_num += 1
        sending = self._sending_batch
        reduced = KBatch(sending.install_id, sending.user_id, sending.data_session_id)
        reduced.batch_start_time = sending.batch_start_time
        reduced.batch_end_time = sending.batch_end_time
        reduced.custom_events_version = sending.custom_events_version
        reduced.batch_num = num
        encoding.batch_dropped(reduced.data, reduced.batch_start_time, KBatchDropReason.TOO_MANY_UNSENT_EVENTS)

        written = serializer.save_to_file(reduced, self._batch_filename(reduced))
        with self._queue_lock:
            self._pending_batches.append(_PendingBatchInfo(reduced.batch_end_time, reduced.batch_num, written))

    def _swap_batches(self) -> UserConsentState:
        """Swaps the collecting and sending batches; returns the consent state observed at the swap,
        so the caller's decision about the batch it just took can't be based on a value that changed
        in between."""
        with self._swap_lock:
            self._in_batch, self._sending_batch = self._sending_batch, self._in_batch

            self._in_batch.batch_num = 0
            self._in_batch.user_id = self._user_id
            self._in_batch.data.set_length(0)
            self._in_batch.cut_positions.clear()

            if self._test_user_name is not None:
                self._send_test_user_name = self._test_user_name
                self._test_user_name = None
            return self._user_consent_state

    def _send_pending_batches(self) -> bool:
        """Uploads up to ``_MAX_BATCHES_PER_CYCLE`` pending batches, oldest first. Returns False if
        the cycle ended on a failure (the caller's signal to back off). Sending stops at the first
        batch that doesn't get through, so batches are delivered in order and none is skipped.
        The failing batch therefore stays at the head of the queue, which is why _head_batch_attempts
        can be a single counter rather than per-batch state.
        Only a *refusal* is counted. An unreachable server means we never got a verdict on the batch,
        and counting those would turn any long offline stretch into data loss -- the exact opposite of
        what an offline-first SDK should do."""
        consent = self._user_consent_state
        if consent == UserConsentState.PENDING:
            return True
        if consent == UserConsentState.DENIED:
            with self._queue_lock:
                self._purge_pending_batches()
            return True

        # Snapshot the head of the queue, then do the HTTP with no lock held: uploads can sit in a
        # 30 s read timeout, and the crashing thread must never wait behind one of those.
        with self._queue_lock:
            if not self._pending_batches:
                return True
            cycle = list(self._pending_batches[: min(_MAX_BATCHES_PER_CYCLE, len(self._pending_batches))])

        b = KBatch(self._install_id, guid.EMPTY, guid.EMPTY)
        num_settled = 0
        failed = False

        for info in cycle:
            filename = self._batch_filename_for(info.batch_end_time, info.batch_num)

            if serializer.load_from_file(filename, b) < 0:
                # Unparseable or already gone: nothing to retry.
                self._delete_file(filename)
                num_settled += 1
                continue

            result = self._send_batch(b)
            if result == SendResult.ACCEPTED:
                self._delete_file(filename)
                num_settled += 1
                continue

            failed = True
            if result == SendResult.REJECTED:
                # Anything settled earlier means the head moved, so this batch's count starts fresh.
                if num_settled > 0:
                    self._head_batch_attempts = 0
                self._head_batch_attempts += 1

                # The ingress deliberately keeps recoverable failures non-2xx (a 401/403 during a key
                # rotation, a transient 400) so that we retry them, and ACKs permanently-invalid
                # batches with 200 so we delete them. So a refusal means "try again", but a batch that
                # will never be accepted would hold up the whole queue forever. Past the attempt budget,
                # replace it with a BATCH_DROPPED marker (same as the disk cap): the events are gone
                # either way, and a marker tells the backend a gap exists here rather than letting the
                # batches simply stop arriving.
                if self._head_batch_attempts >= MAX_BATCH_ATTEMPTS:
                    _log.error(
                        "Batch %d was refused %d times; dropping its events.", info.batch_num, self._head_batch_attempts
                    )
                    # TODO: switch to KBatchDropReason.SEND_FAILED once the backend recognizes it;
                    # for now report a value the backend already understands so the gap is visible.
                    self._replace_with_drop_marker(info, KBatchDropReason.TOO_MANY_UNSENT_EVENTS)
                    self._head_batch_attempts = 0
            break

        if num_settled > 0:
            with self._queue_lock:
                # min guards the one case where the list shrinks underneath us: a consent
                # withdrawal on another thread purges the whole queue mid-cycle.
                for _ in range(min(num_settled, len(self._pending_batches))):
                    self._pending_batches.pop(0)
            if not failed:
                self._head_batch_attempts = 0
        return not failed

    def _send_batch(self, b: KBatch) -> SendResult:
        """Upload of one batch. Consent states are filtered by the caller."""
        # Make sure the backend knows this batch's custom-event mapping before uploading; if we
        # can't (re)register right now, leave the batch on disk and retry later.
        if not self._ensure_custom_events_registered(b.custom_events_version):
            return SendResult.UNREACHABLE
        return self._network.send_batch(b, self._send_test_user_name)

    def _ensure_custom_events_registered(self, ce_version: int) -> bool:
        """Ensures the backend has the custom-event mapping this batch was built against.
        Skips work when the version is 0 or already confirmed this run; otherwise asks the backend
        whether it has the mapping and, if not, loads the persisted set from disk and registers it.
        Returns False (so the caller retries later) if the mapping can't be confirmed."""
        if ce_version == 0 or ce_version == self._ce_map_version:
            return True

        lookup = self._network.get_custom_event_ids(ce_version)
        has_mapping = lookup.has_mapping
        if lookup.need_to_register:
            ce_set = KStorage.load_custom_event_set_from_file(self._custom_event_set_filename(ce_version))
            if ce_set is None:
                _log.error(f"Cannot load custom event from file {self._custom_event_set_filename(ce_version)}")
                return False
            has_mapping = self._network.register_custom_events(ce_set.version, ce_set.event_count, ce_set.gzip_data)

        if not has_mapping:
            return False
        self._ce_map_version = ce_version
        return True

    def _replace_with_drop_marker(self, info: _PendingBatchInfo, reason: int) -> int:
        """Rewrites ``info``'s batch file in place as a single BATCH_DROPPED marker, keeping its slot
        in the queue. The batch's identity (ids, batch number, time span) is preserved; only the
        events are replaced.
        Returns the marker's size on disk, or -1 if the file could not be read."""
        filename = self._batch_filename_for(info.batch_end_time, info.batch_num)
        marker = KBatch(guid.EMPTY, guid.EMPTY, guid.EMPTY)
        if serializer.load_from_file(filename, marker) < 0:
            return -1

        marker.data.set_length(0)
        encoding.batch_dropped(marker.data, marker.batch_start_time, reason)

        new_size = serializer.save_to_file(marker, filename)
        info.size = new_size
        return new_size

    def _reduce_storage_size(self, unsent_batches: List[_PendingBatchInfo], top_limit: int) -> int:
        total = sum(info.size for info in unsent_batches)
        if total <= top_limit:
            return total

        i = 0
        while total > top_limit and i < len(unsent_batches):
            info = unsent_batches[i]
            if info.size > _BATCH_DROP_THRESHOLD:
                before = info.size
                new_size = self._replace_with_drop_marker(info, KBatchDropReason.TOO_MANY_UNSENT_EVENTS)
                if new_size >= 0:
                    total += new_size - before
            i += 1
        return total

    # --- Event writers (called from the app thread) ---------------------------------------------

    def _mark_batch_start_if_needed(self) -> None:
        if self._in_batch.data.length == 0:
            self._in_batch.batch_start_time = self._frame_timestamp

    @staticmethod
    def _write_event_id(buf: KBuffer, timestamp: int, event_id: int) -> None:
        encoding.header(buf, timestamp, event_id)

    def _send_if_needed(self) -> None:
        size = self._in_batch.data.length
        if size < _SEND_THRESHOLD:
            return

        cuts = self._in_batch.cut_positions
        last_cut_pos = 0 if not cuts else cuts[-1].pos
        if size - last_cut_pos >= _BATCH_CUTTING_THRESHOLD:
            cuts.append(CutPoint(size, self._frame_timestamp))
        self._ready_to_send.set()

    def _refresh_now(self) -> None:
        self._frame_timestamp = self._now_seconds()

    @staticmethod
    def _capped(s: str) -> str:
        return encoding.capped(s)

    def _write(self, encoder, *args) -> None:
        """Appends one event, encoded by ``encoder(buf, ts, *args)`` (see :mod:`.encoding`)."""
        with self._swap_lock:
            self._write_locked(encoder, *args)

    def _write_locked(self, encoder, *args) -> None:
        """Body of :meth:`_write`; call only while holding ``_swap_lock``."""
        self._refresh_now()
        self._mark_batch_start_if_needed()
        encoder(self._in_batch.data, self._frame_timestamp, *args)
        self._send_if_needed()

    def add_event(self, event_type: int) -> None:
        self._write(encoding.event, event_type)

    def add_event_str(self, event_type: int, s: str) -> None:
        self._write(encoding.event_str, event_type, s)

    def add_event_uint(self, event_type: int, value: int) -> None:
        self._write(encoding.event_uint, event_type, value)

    def add_event_int(self, event_type: int, value: int) -> None:
        self._write(encoding.event_int, event_type, value)

    def add_event_float(self, event_type: int, value: float) -> None:
        self._write(encoding.event_float, event_type, value)

    def add_event_ushort_pair(self, event_type: int, x: int, y: int) -> None:
        self._write(encoding.event_ushort_pair, event_type, x, y)

    def add_event_bool(self, event_type: int, flag: bool) -> None:
        self._write(encoding.event_bool, event_type, flag)

    def _add_event_timestamp_seconds(self, event_type: int, seconds: int) -> None:
        self._write(encoding.event_uint, event_type, seconds)

    # --- Public report API (invoked by the SDK facade) ------------------------------------------

    def set_user_id(self, uid: KGuid) -> None:
        with self._swap_lock:
            self._refresh_now()
            self._mark_batch_start_if_needed()
            self._user_id = uid
            self._in_batch.user_id = uid
            encoding.header(self._in_batch.data, self._frame_timestamp, KEvents.USER_ID_ASSIGNED)
            self._send_if_needed()

    def assign_to_ab_test_group(self, test_name: str, group: str) -> None:
        self._write(encoding.ab_test_assignment, test_name, group)

    def report_in_app_purchase_usd(self, product_name: str, price_usd_cents: int) -> None:
        self._write(encoding.purchase_usd, product_name, price_usd_cents)

    def report_in_app_purchase_local(self, product_name: str, localized_price: float, currency_code: str) -> None:
        self._write(encoding.purchase_local, product_name, localized_price, currency_code)

    def report_ad_offered(self, placement: str, ad_type: int) -> None:
        self._write(encoding.ad_offered, placement, ad_type)

    def report_ad_revenue_usd(self, placement: str, revenue_usd_cents: int) -> None:
        self._write(encoding.ad_revenue_usd, placement, revenue_usd_cents)

    def report_ad_revenue_local(self, placement: str, localized_revenue: float, currency_code: str) -> None:
        self._write(encoding.ad_revenue_local, placement, localized_revenue, currency_code)

    def report_subscription_revenue_usd(self, package_name: str, revenue_usd_cents: int) -> None:
        self._write(encoding.subscription_revenue_usd, package_name, revenue_usd_cents)

    def report_subscription_revenue_local(
        self, package_name: str, localized_revenue: float, currency_code: str
    ) -> None:
        self._write(encoding.subscription_revenue_local, package_name, localized_revenue, currency_code)

    def report_item_exchange(self, exchange_point: str, from_items: Sequence[Item], to_items: Sequence[Item]) -> None:
        self._write(encoding.items_exchange, exchange_point, from_items, to_items)

    def report_items_reset(self, location: str, items: Sequence[Item]) -> None:
        self._write(encoding.event_str_items, KEvents.ITEMS_RESET, location, items)

    def report_in_app_purchase_items_granted(self, product_id: str, items: Sequence[Item]) -> None:
        self._write(encoding.event_str_items, KEvents.ITEMS_PURCHASED_GRANT, product_id, items)

    def report_ad_items_granted(self, placement: str, items: Sequence[Item]) -> None:
        self._write(encoding.event_str_items, KEvents.ITEMS_AD_GRANTED, placement, items)

    def report_subscription_items_granted(self, package_name: str, items: Sequence[Item]) -> None:
        self._write(encoding.event_str_items, KEvents.ITEMS_SUBSCRIPTION_GRANTED, package_name, items)

    def report_pre_sdk_registration_date(self, seconds: int) -> None:
        self._add_event_timestamp_seconds(KEvents.PRE_SDK_REGISTRATION_DATE, seconds)

    def report_install_campaign(self, campaign_name: str) -> None:
        self.add_event_str(KEvents.INSTALL_CAMPAIGN, campaign_name)

    def report_window_open(self, name: str) -> None:
        self.add_event_str(KEvents.WINDOW_OPEN, name)

    def report_window_close(self, name: str) -> None:
        self.add_event_str(KEvents.WINDOW_CLOSE, name)

    def report_user_country(self, name: str) -> None:
        self.add_event_str(KEvents.COUNTRY, name)

    def report_button_click(self, name: str) -> None:
        self.add_event_str(KEvents.BUTTON_CLICK, name)

    def report_game_language(self, language: str) -> None:
        self.add_event_str(KEvents.GAME_LANG, language)

    def report_internet_connected(self) -> None:
        self.add_event(KEvents.INTERNET_CONNECTED)

    def report_internet_disconnected(self) -> None:
        self.add_event(KEvents.INTERNET_DISCONNECTED)

    def report_low_memory(self) -> None:
        self.add_event(KEvents.LOW_MEM_WARNING)

    def report_scene_loaded(self, name: str) -> None:
        self.add_event_str(KEvents.SCENE_LOADED, name)

    def report_scene_unloaded(self, name: str) -> None:
        self.add_event_str(KEvents.SCENE_UNLOADED, name)

    def report_deep_link(self, link: str) -> None:
        self.add_event_str(KEvents.DEEP_LINK_ACTIVATED, link)

    def report_app_pause(self) -> None:
        self._add_event_timestamp_seconds(KEvents.APP_PAUSE, self._now_seconds())

    def report_app_resume(self) -> None:
        self._add_event_timestamp_seconds(KEvents.APP_RESUME, self._now_seconds())

    def log_error(self, msg: str) -> None:
        self.add_event_str(KEvents.ERROR_MSG, msg)

    def report_onboarding_milestone(self, milestone: str) -> None:
        """Reports an onboarding milestone, numbering repeats as ``"x"``, ``"x (#2)"``, … across
        launches. The disk write is deferred to the send thread (see :meth:`_flush_deferred_writes`)
        so the FTUE funnel — the busiest stretch of a new user's session — keeps I/O off the caller."""
        with self._swap_lock:
            if self._onboarding_milestones is None:
                self._onboarding_milestones = KStorage.load_onboarding_counters(self._onboarding_counters_file_name())
            counters = self._onboarding_milestones

            occurrences = counters.get(milestone, 0) + 1
            counters[milestone] = occurrences
            self._onboarding_dirty = True

            name = f"{milestone} (#{occurrences})" if occurrences > 1 else milestone
            # Lock-free variant: we already hold _swap_lock (which is non-reentrant).
            self._write_locked(encoding.event_str, KEvents.ONBOARDING_MILESTONE, name)

    def set_test_user_name(self, tester_name: str) -> None:
        """Marks this device as a test user. The disk write is deferred to the send thread."""
        with self._swap_lock:
            self._test_user_name = tester_name
            self._test_user_name_to_persist = tester_name
            self._ready_to_send.set()

    def _flush_deferred_writes(self) -> None:
        """Performs the disk writes deferred by the report methods. Runs on the send thread."""
        with self._swap_lock:
            counters = (
                dict(self._onboarding_milestones)
                if (self._onboarding_dirty and self._onboarding_milestones is not None)
                else None
            )
            self._onboarding_dirty = False
            tester = self._test_user_name_to_persist
            self._test_user_name_to_persist = None
        if counters is not None:
            KStorage.save_onboarding_counters(self._onboarding_counters_file_name(), counters)
        if tester is not None:
            KStorage.save_test_user_name(self._test_user_file_name(), tester)

    @staticmethod
    def _write_items(buf: KBuffer, items: Sequence[Item]) -> None:
        encoding.write_items(buf, items)

    # --- Helpers --------------------------------------------------------------------------------

    @staticmethod
    def _now_seconds() -> int:
        return int(time.time())

    def _batch_filename(self, batch: KBatch) -> str:
        return self._batch_filename_for(batch.batch_end_time, batch.batch_num)

    def _batch_filename_for(self, batch_end_time: int, batch_num: int) -> str:
        return os.path.join(self._work_folder, f"{batch_end_time}_{batch_num}.kwub")

    def _custom_event_set_filename(self, version: int) -> str:
        return os.path.join(self._work_folder, f"{version}.map.gz")

    def _test_user_file_name(self) -> str:
        return os.path.join(self._work_folder, "test_user.info")

    def _onboarding_counters_file_name(self) -> str:
        return os.path.join(self._work_folder, "onboarding.counters")

    @staticmethod
    def _delete_file(filename: str) -> None:
        try:
            if os.path.exists(filename):
                os.remove(filename)
        except OSError:
            pass

    def _load_unsent_batches_list(self, folder: str) -> List[_PendingBatchInfo]:
        result: List[_PendingBatchInfo] = []
        try:
            if not os.path.isdir(folder):
                return result
            for entry in os.scandir(folder):
                if not entry.name.endswith(".kwub"):
                    continue
                name_no_ext = entry.name[: -len(".kwub")]
                parts = name_no_ext.split("_")
                if len(parts) == 2:
                    try:
                        timestamp = int(parts[0])
                        num = int(parts[1])
                    except ValueError:
                        continue
                    result.append(_PendingBatchInfo(timestamp, num, entry.stat().st_size))
            result.sort(key=lambda b: (b.batch_end_time, b.batch_num))
        except OSError:
            result.clear()
        return result
