"""Unit tests for KEventDispatcher: event encoding, local state, and the background send loop."""

import os
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mockserver import MockIngress, wait_until  # noqa: E402

import keewano_sdk.internal.dispatcher as disp_mod  # noqa: E402
from keewano_sdk.internal import guid, serializer  # noqa: E402
from keewano_sdk.internal.batch import KBatch  # noqa: E402
from keewano_sdk.internal.consent import UserConsentState  # noqa: E402
from keewano_sdk.internal.custom_event_set import CustomEventSet  # noqa: E402
from keewano_sdk.internal.dispatcher import (  # noqa: E402
    MAX_BATCH_ATTEMPTS,
    MAX_EVENT_STRING_CHARS,
    MAX_ITEMS_PER_EVENT,
    KEventDispatcher,
    _PendingBatchInfo,
)
from keewano_sdk.internal.events import KBatchDropReason, KEvents  # noqa: E402
from keewano_sdk.internal.network import SendResult  # noqa: E402
from keewano_sdk.item import Item  # noqa: E402


class _FakeTransport:
    """Substitutable transport so retry logic can be driven without real HTTP or timing."""

    def __init__(self, result):
        self.result = result
        self.send_calls = 0

    def send_batch(self, batch, test_user):
        self.send_calls += 1
        return self.result

    def get_custom_event_ids(self, ce_version):
        from keewano_sdk.internal.network import CustomEventLookup

        return CustomEventLookup(has_mapping=True, need_to_register=False)

    def register_custom_events(self, version, event_count, gzip_data):
        return True


def _make_dispatcher(
    work_dir,
    endpoint="http://127.0.0.1:1/base",
    consent=UserConsentState.NOT_REQUIRED,
    ce_set=None,
    disk_usage_limit=None,
):
    kwargs = dict(
        working_directory=work_dir,
        endpoint=endpoint,
        app_secret="tok",
        initial_consent=consent,
        install_id=guid.from_uint64(11),
        initial_user_id=guid.EMPTY,
        data_session_id=guid.from_uint64(33),
        sdk_version="1.0.0",
        custom_event_set=ce_set,
    )
    if disk_usage_limit is not None:
        kwargs["disk_usage_limit"] = disk_usage_limit
    return KEventDispatcher(**kwargs)


# --- tiny event-stream reader (little-endian, matching the wire format) ---


def _hdr(body, off):
    ts, eid = struct.unpack_from("<IH", body, off)
    return ts, eid, off + 6


def _u32(body, off):
    return struct.unpack_from("<I", body, off)[0], off + 4


def _i32(body, off):
    return struct.unpack_from("<i", body, off)[0], off + 4


def _string(body, off):
    length, shift = 0, 0
    while True:
        byte = body[off]
        off += 1
        length |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
    return body[off : off + length].decode("utf-8"), off + length


class DispatcherEncodingTest(unittest.TestCase):
    """Encoding tests read _in_batch directly; events are tiny so nothing flushes to the network."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._disp = _make_dispatcher(self._dir)

    def tearDown(self):
        self._disp.stop()

    def _body(self):
        return self._disp._in_batch.data.to_bytes()

    def test_plain_event_header_only(self):
        self._disp.add_event(KEvents.LOW_MEM_WARNING)
        body = self._body()
        ts, eid, off = _hdr(body, 0)
        self.assertEqual(eid, KEvents.LOW_MEM_WARNING)
        self.assertEqual(off, len(body))  # no params
        self.assertGreater(ts, 0)

    def test_batch_start_time_set_on_first_event(self):
        self.assertEqual(self._disp._in_batch.data.length, 0)
        self._disp.add_event_str(KEvents.BUTTON_CLICK, "Play")
        ts, _eid, _off = _hdr(self._body(), 0)
        self.assertEqual(self._disp._in_batch.batch_start_time, ts)

    def test_str_uint_int_ushortpair_encodings(self):
        self._disp.add_event_str(KEvents.BUTTON_CLICK, "Play")
        self._disp.add_event_uint(KEvents.RAM_SIZE, 4096)
        self._disp.add_event_int(KEvents.RAM_SIZE, -5)
        self._disp.add_event_ushort_pair(KEvents.SCREEN_RESOLUTION, 1920, 1080)
        body = self._body()

        _ts, eid, off = _hdr(body, 0)
        self.assertEqual(eid, KEvents.BUTTON_CLICK)
        s, off = _string(body, off)
        self.assertEqual(s, "Play")

        _ts, eid, off = _hdr(body, off)
        val, off = _u32(body, off)
        self.assertEqual((eid, val), (KEvents.RAM_SIZE, 4096))

        _ts, eid, off = _hdr(body, off)
        val, off = _i32(body, off)
        self.assertEqual((eid, val), (KEvents.RAM_SIZE, -5))

        _ts, eid, off = _hdr(body, off)
        x, y = struct.unpack_from("<HH", body, off)
        self.assertEqual((eid, x, y), (KEvents.SCREEN_RESOLUTION, 1920, 1080))

    def test_set_user_id_writes_event_and_updates_batch(self):
        uid = guid.from_uint64(777)
        self._disp.set_user_id(uid)
        self.assertEqual(self._disp._in_batch.user_id, uid)
        self.assertEqual(self._disp._user_id, uid)
        _ts, eid, _off = _hdr(self._body(), 0)
        self.assertEqual(eid, KEvents.USER_ID_ASSIGNED)

    def test_ab_test_group_writes_string_and_byte(self):
        self._disp.assign_to_ab_test_group("shop_layout", chr(200))  # a single char -> one wire byte
        body = self._body()
        _ts, eid, off = _hdr(body, 0)
        name, off = _string(body, off)
        self.assertEqual((eid, name), (KEvents.AB_TEST_ASSIGNMENT, "shop_layout"))
        self.assertEqual(body[off : off + 1], bytes([200]))  # a single raw byte, no length prefix

    def test_item_exchange_encodes_from_and_to_lists(self):
        self._disp.report_item_exchange("shop", [Item("coins", 100)], [Item("sword", 1), Item("shield", 2)])
        body = self._body()
        _ts, eid, off = _hdr(body, 0)
        self.assertEqual(eid, KEvents.ITEMS_EXCHANGE)
        loc, off = _string(body, off)
        self.assertEqual(loc, "shop")
        # from list
        count, off = _i32(body, off)
        self.assertEqual(count, 1)
        name, off = _string(body, off)
        qty, off = _u32(body, off)
        self.assertEqual((name, qty), ("coins", 100))
        # to list
        count, off = _i32(body, off)
        self.assertEqual(count, 2)
        n1, off = _string(body, off)
        q1, off = _u32(body, off)
        n2, off = _string(body, off)
        q2, off = _u32(body, off)
        self.assertEqual([(n1, q1), (n2, q2)], [("sword", 1), ("shield", 2)])

    def test_purchase_usd_writes_three_tagged_events(self):
        self._disp.report_in_app_purchase_usd("gems_100", 499)
        body = self._body()
        ids = []
        off = 0
        # event 1: PURCHASE_TIMESTAMP + uint32
        _ts, eid, off = _hdr(body, off)
        _v, off = _u32(body, off)
        ids.append(eid)
        # event 2: PURCHASE_PRODUCT_ID + string
        _ts, eid, off = _hdr(body, off)
        s, off = _string(body, off)
        ids.append(eid)
        # event 3: PURCHASE_PRODUCT_PRICE_USD_CENTS + uint32
        _ts, eid, off = _hdr(body, off)
        price, off = _u32(body, off)
        ids.append(eid)
        self.assertEqual(
            ids, [KEvents.PURCHASE_TIMESTAMP, KEvents.PURCHASE_PRODUCT_ID, KEvents.PURCHASE_PRODUCT_PRICE_USD_CENTS]
        )
        self.assertEqual(s, "gems_100")
        self.assertEqual(price, 499)

    def test_purchase_local_currency_writes_currency_and_float(self):
        self._disp.report_in_app_purchase_local("gems_100", 4.99, "EUR")
        body = self._body()
        off = 0
        _ts, eid, off = _hdr(body, off)
        _v, off = _u32(body, off)  # PURCHASE_TIMESTAMP
        self.assertEqual(eid, KEvents.PURCHASE_TIMESTAMP)
        _ts, eid, off = _hdr(body, off)
        product, off = _string(body, off)  # PURCHASE_PRODUCT_ID
        self.assertEqual((eid, product), (KEvents.PURCHASE_PRODUCT_ID, "gems_100"))
        _ts, eid, off = _hdr(body, off)
        currency, off = _string(body, off)  # LOCAL_CURRENCY_NAME
        self.assertEqual((eid, currency), (KEvents.PURCHASE_LOCAL_CURRENCY_NAME, "EUR"))
        _ts, eid, off = _hdr(body, off)  # LOCAL_CURRENCY_AMOUNT
        (amount,) = struct.unpack_from("<f", body, off)
        self.assertEqual(eid, KEvents.PURCHASE_LOCAL_CURRENCY_AMOUNT)
        self.assertAlmostEqual(amount, 4.99, places=5)


class DispatcherLocalStateTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()

    def test_onboarding_milestone_counter_increments_and_persists(self):
        disp = _make_dispatcher(self._dir)
        try:
            disp.report_onboarding_milestone("tut_1")
            disp.report_onboarding_milestone("tut_1")
            disp.report_onboarding_milestone("tut_2")
            body = disp._in_batch.data.to_bytes()
        finally:
            disp.stop()

        names = []
        off = 0
        while off < len(body):
            _ts, _eid, off = _hdr(body, off)
            s, off = _string(body, off)
            names.append(s)
        self.assertEqual(names, ["tut_1", "tut_1 (#2)", "tut_2"])
        self.assertTrue(os.path.exists(os.path.join(self._dir, "onboarding.counters")))

        # A fresh dispatcher reloads the counters and continues numbering.
        disp2 = _make_dispatcher(self._dir)
        try:
            disp2.report_onboarding_milestone("tut_1")
            body2 = disp2._in_batch.data.to_bytes()
        finally:
            disp2.stop()
        _ts, _eid, off = _hdr(body2, 0)
        s, _off = _string(body2, off)
        self.assertEqual(s, "tut_1 (#3)")

    def test_set_test_user_name_persists_to_file(self):
        disp = _make_dispatcher(self._dir)
        try:
            disp.set_test_user_name("qa-bob")
        finally:
            disp.stop()
        with open(os.path.join(self._dir, "test_user.info"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "qa-bob")

    def test_load_unsent_batches_list_parses_and_sorts(self):
        disp = _make_dispatcher(self._dir)
        disp.stop()  # stop first so the send thread can't touch the files we write
        for name in ["200_1.kwub", "100_2.kwub", "100_1.kwub", "junk.kwub", "not_a_batch.txt"]:
            with open(os.path.join(self._dir, name), "wb") as f:
                f.write(b"x" * 10)
        listed = disp._load_unsent_batches_list(self._dir)
        order = [(b.batch_end_time, b.batch_num) for b in listed]
        self.assertEqual(order, [(100, 1), (100, 2), (200, 1)])  # sorted, junk ignored

    def test_reduce_storage_size_collapses_when_over_limit(self):
        disp = _make_dispatcher(self._dir)
        disp.stop()  # stop first so the send thread can't race on the files
        from keewano_sdk.internal.batch import KBatch

        infos = []
        for i in range(3):
            b = KBatch(guid.EMPTY, guid.EMPTY, guid.EMPTY)
            b.batch_num = i
            b.batch_end_time = 100 + i
            b.data.write_string("some payload to exceed the drop threshold" * 3)
            size = serializer.save_to_file(b, os.path.join(self._dir, f"{b.batch_end_time}_{i}.kwub"))
            infos.append(_PendingBatchInfo(b.batch_end_time, i, size))

        total_before = sum(x.size for x in infos)
        # Force a tiny limit so reduction kicks in.
        new_total = disp._reduce_storage_size(infos, top_limit=total_before // 2)
        self.assertLess(new_total, total_before)  # collapsed some batches into drop markers


class DispatcherSendLoopTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()

    def _kwub_files(self):
        return [f for f in os.listdir(self._dir) if f.endswith(".kwub")]

    def test_not_required_consent_uploads(self):
        with MockIngress() as srv:
            disp = _make_dispatcher(self._dir, endpoint=srv.endpoint)
            try:
                disp.report_button_click("Play")
                disp.send_now()
                self.assertTrue(wait_until(lambda: len(srv.batch_posts) >= 1))
                self.assertTrue(wait_until(lambda: not self._kwub_files()))  # deleted after upload
            finally:
                disp.stop()

    def test_pending_consent_buffers_then_grant_flushes(self):
        with MockIngress() as srv:
            disp = _make_dispatcher(self._dir, endpoint=srv.endpoint, consent=UserConsentState.PENDING)
            try:
                disp.report_button_click("Play")
                disp.send_now()
                self.assertTrue(wait_until(lambda: self._kwub_files()))  # persisted, not sent
                time.sleep(0.3)
                self.assertEqual(len(srv.batch_posts), 0)
                disp.set_user_consent(True)
                self.assertTrue(wait_until(lambda: len(srv.batch_posts) >= 1))
            finally:
                disp.stop()

    def test_denied_consent_discards_without_sending(self):
        with MockIngress() as srv:
            disp = _make_dispatcher(self._dir, endpoint=srv.endpoint, consent=UserConsentState.DENIED)
            try:
                disp.report_button_click("Play")
                disp.send_now()
                self.assertTrue(wait_until(lambda: not self._kwub_files()))  # discarded
                self.assertEqual(len(srv.batch_posts), 0)
            finally:
                disp.stop()

    def test_offline_retains_batches_on_disk(self):
        disp = _make_dispatcher(self._dir, endpoint="http://127.0.0.1:1/base")  # nothing listening
        try:
            disp.report_button_click("Play")
            disp.send_now()
            self.assertTrue(wait_until(lambda: self._kwub_files()))
            time.sleep(0.3)
            self.assertTrue(self._kwub_files())  # still there; will retry next run
        finally:
            disp.stop()

    def test_custom_events_registered_on_204_then_batch_sent(self):
        ce = CustomEventSet(version=123, event_count=1, gzip_data=b"\x1f\x8bDEFS")
        with MockIngress() as srv:
            srv.set_custom_get_status(204)  # backend needs the mapping
            disp = _make_dispatcher(self._dir, endpoint=srv.endpoint, ce_set=ce)
            try:
                disp.report_button_click("Play")
                disp.send_now()
                self.assertTrue(wait_until(lambda: len(srv.custom_posts) >= 1))
                self.assertTrue(wait_until(lambda: len(srv.batch_posts) >= 1))
                _hdrs, body = srv.custom_posts[0]
                self.assertEqual(body, b"\x1f\x8bDEFS")  # the persisted set, verbatim
            finally:
                disp.stop()

    def test_custom_events_has_mapping_skips_registration(self):
        ce = CustomEventSet(version=55, event_count=1, gzip_data=b"g")
        with MockIngress() as srv:
            srv.set_custom_get_status(200)  # already known
            disp = _make_dispatcher(self._dir, endpoint=srv.endpoint, ce_set=ce)
            try:
                disp.report_button_click("Play")
                disp.send_now()
                self.assertTrue(wait_until(lambda: len(srv.batch_posts) >= 1))
                time.sleep(0.2)
                self.assertEqual(len(srv.custom_posts), 0)  # no registration POST
            finally:
                disp.stop()

    def test_custom_events_missing_set_file_blocks_send(self):
        ce = CustomEventSet(version=99, event_count=1, gzip_data=b"g")
        with MockIngress() as srv:
            srv.set_custom_get_status(204)
            disp = _make_dispatcher(self._dir, endpoint=srv.endpoint, ce_set=ce)
            try:
                os.remove(os.path.join(self._dir, "99.map.gz"))  # simulate lost set file
                disp.report_button_click("Play")
                disp.send_now()
                self.assertTrue(wait_until(lambda: self._kwub_files()))
                time.sleep(0.3)
                self.assertEqual(len(srv.batch_posts), 0)  # cannot register -> batch not sent
            finally:
                disp.stop()


class DispatcherStringAndItemCapsTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._disp = _make_dispatcher(self._dir)
        self._disp.stop()  # stop the send thread so _in_batch is never swapped out from under us

    def test_string_payload_is_capped(self):
        self._disp.add_event_str(KEvents.BUTTON_CLICK, "x" * (MAX_EVENT_STRING_CHARS + 500))
        body = self._disp._in_batch.data.to_bytes()
        _ts, _eid, off = _hdr(body, 0)
        s, _off = _string(body, off)
        self.assertEqual(len(s), MAX_EVENT_STRING_CHARS)

    def test_item_list_is_capped(self):
        items = [Item(f"i{i}", 1) for i in range(MAX_ITEMS_PER_EVENT + 10)]
        self._disp.report_items_reset("loc", items)
        body = self._disp._in_batch.data.to_bytes()
        _ts, _eid, off = _hdr(body, 0)
        _loc, off = _string(body, off)
        count, _off = _i32(body, off)
        self.assertEqual(count, MAX_ITEMS_PER_EVENT)


class DispatcherReliabilityTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()

    def _kwub_files(self):
        return [f for f in os.listdir(self._dir) if f.endswith(".kwub")]

    def _write_pending_batch(self, disp, end_time=100, num=0):
        b = KBatch(guid.from_uint64(1), guid.EMPTY, guid.EMPTY)
        b.batch_end_time = end_time
        b.batch_num = num
        b.data.write_string("payload")
        path = os.path.join(self._dir, f"{end_time}_{num}.kwub")
        size = serializer.save_to_file(b, path)
        disp._pending_batches_loaded = True
        disp._pending_batches = [_PendingBatchInfo(end_time, num, size)]
        return path

    def _first_event_id(self, path):
        b = KBatch(guid.EMPTY, guid.EMPTY, guid.EMPTY)
        serializer.load_from_file(path, b)
        return struct.unpack_from("<IH", b.data.to_bytes(), 0)[1]

    def test_rejected_head_batch_dropped_after_max_attempts(self):
        disp = _make_dispatcher(self._dir)
        disp.stop()  # drive the send path manually
        disp._network = _FakeTransport(SendResult.REJECTED)
        path = self._write_pending_batch(disp)

        for _ in range(MAX_BATCH_ATTEMPTS - 1):
            disp._send_pending_batches()
            self.assertNotEqual(self._first_event_id(path), KEvents.BATCH_DROPPED)  # still retrying

        disp._send_pending_batches()  # the MAX_BATCH_ATTEMPTS-th refusal
        self.assertEqual(self._first_event_id(path), KEvents.BATCH_DROPPED)  # replaced with a marker

    def test_unreachable_never_counts_toward_drop(self):
        disp = _make_dispatcher(self._dir)
        disp.stop()
        disp._network = _FakeTransport(SendResult.UNREACHABLE)
        path = self._write_pending_batch(disp)

        for _ in range(MAX_BATCH_ATTEMPTS * 3):
            disp._send_pending_batches()
        # An unreachable server is an outage, not a poison batch: the events are preserved forever.
        self.assertNotEqual(self._first_event_id(path), KEvents.BATCH_DROPPED)
        self.assertEqual(disp._head_batch_attempts, 0)

    def test_consent_withdrawal_from_granted_purges_disk(self):
        with MockIngress() as srv:
            srv.set_batch_status(500)  # keep the batch on disk (rejected)
            disp = _make_dispatcher(self._dir, endpoint=srv.endpoint, consent=UserConsentState.GRANTED)
            try:
                disp.report_button_click("A")
                disp.send_now()
                self.assertTrue(wait_until(lambda: self._kwub_files()))  # persisted
                disp.set_user_consent(False)  # withdraw from GRANTED
                disp.send_now()
                self.assertTrue(wait_until(lambda: not self._kwub_files()))  # purged
            finally:
                disp.stop()

    def test_next_batch_num_seeded_past_leftover_files(self):
        # A leftover file from a previous process must not be overwritten by numbering from 0.
        # Use a valid batch so the send thread (dead endpoint -> unreachable) leaves it in place.
        leftover = KBatch(guid.from_uint64(1), guid.EMPTY, guid.EMPTY)
        leftover.batch_end_time = 100
        leftover.batch_num = 5
        leftover.data.write_string("payload")
        serializer.save_to_file(leftover, os.path.join(self._dir, "100_5.kwub"))

        disp = _make_dispatcher(self._dir)
        try:
            disp._load_pending_batches()  # idempotent; seeds past the leftover on first load
            self.assertEqual(disp._next_batch_num, 6)
        finally:
            disp.stop()

    def test_persist_now_writes_sub_threshold_events_synchronously(self):
        disp = _make_dispatcher(self._dir)
        try:
            disp.report_button_click("A")  # small event, below the send threshold
            self.assertEqual(self._kwub_files(), [])  # not persisted yet
            disp.persist_now()
            self.assertTrue(self._kwub_files())  # now on disk, synchronously
        finally:
            disp.stop()

    def test_periodic_flush_persists_without_explicit_send(self):
        original = disp_mod.FLUSH_INTERVAL_MS
        disp_mod.FLUSH_INTERVAL_MS = 150  # speed up the flush cadence for the test
        try:
            disp = _make_dispatcher(self._dir)
            try:
                disp.report_button_click("A")  # small; never call send_now/flush
                self.assertTrue(wait_until(lambda: self._kwub_files(), timeout=3.0))
            finally:
                disp.stop()
        finally:
            disp_mod.FLUSH_INTERVAL_MS = original

    def test_stop_persists_unflushed_tail(self):
        # stop() itself doesn't persist; the send thread's loop-exit final drain does. A small event
        # that never hit the send threshold must still be on disk once stop()/join returns.
        disp = _make_dispatcher(self._dir)  # dead endpoint -> nothing uploads/deletes
        disp.report_button_click("A")
        self.assertEqual(self._kwub_files(), [])  # below threshold, not yet persisted
        disp.stop()
        self.assertTrue(self._kwub_files())  # final drain on the way out saved it

    def test_drop_marker_carries_the_reason(self):
        disp = _make_dispatcher(self._dir)
        disp.stop()
        disp._network = _FakeTransport(SendResult.REJECTED)
        path = self._write_pending_batch(disp)
        for _ in range(MAX_BATCH_ATTEMPTS):
            disp._send_pending_batches()
        b = KBatch(guid.EMPTY, guid.EMPTY, guid.EMPTY)
        serializer.load_from_file(path, b)
        body = b.data.to_bytes()
        _ts, event_id = struct.unpack_from("<IH", body, 0)
        (reason,) = struct.unpack_from("<I", body, 6)
        self.assertEqual(event_id, KEvents.BATCH_DROPPED)
        self.assertEqual(reason, KBatchDropReason.TOO_MANY_UNSENT_EVENTS)

    def test_rejected_then_accepted_resets_attempts_and_clears_queue(self):
        disp = _make_dispatcher(self._dir)
        disp.stop()
        ft = _FakeTransport(SendResult.REJECTED)
        disp._network = ft
        self._write_pending_batch(disp)

        disp._send_pending_batches()  # one refusal
        self.assertEqual(disp._head_batch_attempts, 1)

        ft.result = SendResult.ACCEPTED
        disp._send_pending_batches()  # now it goes through
        self.assertEqual(disp._head_batch_attempts, 0)  # counter reset
        self.assertEqual(disp._pending_batches, [])  # queue drained
        self.assertEqual(self._kwub_files(), [])  # file deleted

    def test_persist_now_saves_tail_when_sender_is_stuck_in_upload(self):
        # The gap sdk.shutdown() closes: stop()'s join is bounded (2 s), but the send thread can be
        # parked in a ~30 s upload when we cancel, so its own loop-exit drain never runs before the
        # daemon thread is torn down. A synchronous persist_now() must still save the RAM tail.
        import threading

        entered = threading.Event()
        release = threading.Event()

        class _BlockingTransport:
            def send_batch(self, batch, test_user):
                entered.set()
                release.wait(5.0)  # park here, as a real upload stuck on the socket read would
                return SendResult.UNREACHABLE

        disp = _make_dispatcher(self._dir)  # dead endpoint: sender idle until we feed it a batch
        disp._network = _BlockingTransport()
        try:
            # Give the sender a batch and wake it, so it parks inside the (blocking) upload.
            self._write_pending_batch(disp)
            disp.send_now()
            self.assertTrue(entered.wait(5.0))  # sender is now stuck in send_batch

            # A small tail event that only a final RAM->disk drain would persist.
            before = set(self._kwub_files())
            disp.report_button_click("TAIL")

            disp.stop()  # join times out; the stuck sender never runs its loop-exit drain
            self.assertGreater(disp._in_batch.data.length, 0)  # tail still stranded in RAM

            disp.persist_now()  # what shutdown() now does
            self.assertEqual(disp._in_batch.data.length, 0)  # tail moved out of RAM
            self.assertTrue(set(self._kwub_files()) - before)  # a new tail batch is on disk
        finally:
            release.set()
            disp.stop()


class DispatcherConsentTransitionTest(unittest.TestCase):
    """Direct coverage of the set_user_consent state machine (withdraw from any state)."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()

    def _disp(self, consent):
        d = _make_dispatcher(self._dir, consent=consent)
        d.stop()  # we only inspect the pure state transition; no send loop needed
        return d

    def test_pending_grant_and_deny(self):
        self.assertEqual(self._disp(UserConsentState.PENDING).set_user_consent(True), UserConsentState.GRANTED)
        self.assertEqual(self._disp(UserConsentState.PENDING).set_user_consent(False), UserConsentState.DENIED)

    def test_withdraw_from_granted(self):
        self.assertEqual(self._disp(UserConsentState.GRANTED).set_user_consent(False), UserConsentState.DENIED)

    def test_grant_when_not_required_stays_not_required(self):
        self.assertEqual(
            self._disp(UserConsentState.NOT_REQUIRED).set_user_consent(True), UserConsentState.NOT_REQUIRED
        )

    def test_deny_from_not_required_becomes_denied(self):
        self.assertEqual(self._disp(UserConsentState.NOT_REQUIRED).set_user_consent(False), UserConsentState.DENIED)

    def test_regrant_after_deny(self):
        d = self._disp(UserConsentState.PENDING)
        self.assertEqual(d.set_user_consent(False), UserConsentState.DENIED)
        self.assertEqual(d.set_user_consent(True), UserConsentState.GRANTED)  # not stuck at Denied


if __name__ == "__main__":
    unittest.main()
