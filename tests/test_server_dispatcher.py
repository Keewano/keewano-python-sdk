"""Unit tests for KServerDispatcher: per-user aggregation and identity, memory caps, per-user ordered
upload, persistent work_dir_lease, and fork handling."""

import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mockserver import wait_until  # noqa: E402

import keewano_sdk.internal.server_dispatcher as sd_mod  # noqa: E402
from keewano_sdk.internal import encoding, guid, serializer, work_dir_lease  # noqa: E402
from keewano_sdk.internal.batch import KBatch  # noqa: E402
from keewano_sdk.internal.custom_event_set import CustomEventSet  # noqa: E402
from keewano_sdk.internal.consent import UserConsentState  # noqa: E402
from keewano_sdk.internal.dispatcher import MAX_BATCH_ATTEMPTS, KEventDispatcher  # noqa: E402
from keewano_sdk.internal.events import KBatchDropReason, KEvents  # noqa: E402
from keewano_sdk.internal.network import CustomEventLookup, SendResult  # noqa: E402
from keewano_sdk.internal.server_dispatcher import KServerDispatcher  # noqa: E402
from keewano_sdk.internal.storage import KStorage  # noqa: E402
from keewano_sdk.item import Item  # noqa: E402

U1 = guid.from_uint64(1001)
U2 = guid.from_uint64(1002)
U3 = guid.from_uint64(1003)


class FakeNet:
    """Thread-safe stand-in for KNetwork that records accepted batches and detects a user having two
    uploads in flight at once."""

    def __init__(self, result=SendResult.ACCEPTED, delay=0.0):
        self.lock = threading.Lock()
        self.result = result
        self.results_by_user = {}
        self.delay = delay
        self.sent = []
        self.calls = 0
        self.inflight = set()
        self.overlap = False
        self.ce_lookup = CustomEventLookup(has_mapping=True, need_to_register=False)
        self.registered = []

    def send_batch(self, b, tester):
        uid = str(b.user_id)
        with self.lock:
            self.calls += 1
            if uid in self.inflight:
                self.overlap = True
            self.inflight.add(uid)
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.inflight.discard(uid)
            r = self.results_by_user.get(uid, self.result)
            if r == SendResult.ACCEPTED:
                self.sent.append(
                    dict(
                        install=str(b.install_id),
                        uid=uid,
                        ds=str(b.data_session_id),
                        num=b.batch_num,
                        ce=b.custom_events_version,
                        body=b.data.to_bytes(),
                        tester=tester,
                        start=b.batch_start_time,
                        end=b.batch_end_time,
                    )
                )
        return r

    def get_custom_event_ids(self, ce_version):
        return self.ce_lookup

    def register_custom_events(self, version, event_count, gzip_data):
        self.registered.append(version)
        return True

    def close(self):
        pass

    def for_user(self, user):
        with self.lock:
            return [s for s in self.sent if s["uid"] == str(user)]


def make(net, work_root=None, **overrides):
    kwargs = dict(
        endpoint="http://127.0.0.1:1/base",
        app_secret="tok",
        sdk_version="1.0.0",
        work_root=work_root,
        flush_interval=60.0,
        user_idle_timeout=600.0,
        max_users=1000,
        max_buffered_bytes=16 * 1024 * 1024,
        max_pending_bytes=16 * 1024 * 1024,
        network=net,
    )
    kwargs.update(overrides)
    d = KServerDispatcher(**kwargs)
    d.start()
    return d


def event_ids(body):
    """Event ids of a body made of (header + string) events, as written by event_str."""
    out, off = [], 0
    while off < len(body):
        _, eid = struct.unpack_from("<IH", body, off)
        off += 6
        if eid == KEvents.BATCH_DROPPED:
            off += 4
        else:
            length, shift = 0, 0
            while True:
                byte = body[off]
                off += 1
                length |= (byte & 0x7F) << shift
                if not (byte & 0x80):
                    break
                shift += 7
            off += length
        out.append(eid)
    return out


def click(d, user, name="b"):
    d.add_event_str(user, KEvents.BUTTON_CLICK, name)


class _Base(unittest.TestCase):
    def setUp(self):
        self.dispatchers = []
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        for d in self.dispatchers:
            d.shutdown(0)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, net, **kw):
        d = make(net, **kw)
        self.dispatchers.append(d)
        return d


class AggregationAndIdentityTest(_Base):
    def test_events_aggregate_per_user_and_identity_is_per_user(self):
        net = FakeNet()
        d = self.make(net)
        for _ in range(5):
            click(d, U1)
            click(d, U2)
        click(d, U1)
        d.flush()
        self.assertTrue(wait_until(lambda: len(net.sent) == 2))
        b1, b2 = net.for_user(U1)[0], net.for_user(U2)[0]
        # One batch per user, not one per event.
        self.assertEqual(len(event_ids(b1["body"])), 6)
        self.assertEqual(len(event_ids(b2["body"])), 5)
        # install id == user id; each user starts at batch 0 in a data session of its own.
        self.assertEqual(b1["install"], str(U1))
        self.assertEqual(b2["install"], str(U2))
        self.assertEqual((b1["num"], b2["num"]), (0, 0))
        self.assertNotEqual(b1["ds"], b2["ds"])
        self.assertNotIn(KEvents.USER_ID_ASSIGNED, event_ids(b1["body"]))

    def test_nothing_is_sealed_before_the_flush_interval(self):
        net = FakeNet()
        d = self.make(net, flush_interval=0.3)
        click(d, U1)
        time.sleep(0.1)
        click(d, U1)
        self.assertEqual(net.sent, [])
        self.assertTrue(wait_until(lambda: len(net.sent) == 1, timeout=3))
        self.assertEqual(len(event_ids(net.sent[0]["body"])), 2)

    def test_batch_numbers_count_per_user_within_one_data_session(self):
        net = FakeNet()
        d = self.make(net)
        for _ in range(3):
            click(d, U1)
            d.flush(U1)
        click(d, U2)
        d.flush(U2)
        self.assertTrue(wait_until(lambda: len(net.sent) == 4))
        u1 = sorted(net.for_user(U1), key=lambda s: s["num"])
        self.assertEqual([s["num"] for s in u1], [0, 1, 2])
        self.assertEqual(len({s["ds"] for s in u1}), 1)
        self.assertEqual(net.for_user(U2)[0]["num"], 0)

    def test_seal_allocates_nothing_until_the_users_next_event(self):
        net = FakeNet()
        d = self.make(net)
        click(d, U1)
        d.flush(U1)
        self.assertIsNone(d._users[U1].batch)  # nothing collecting, nothing allocated
        click(d, U1)
        self.assertIsNotNone(d._users[U1].batch)
        d.flush(U1)
        self.assertTrue(wait_until(lambda: len(net.sent) == 2))
        first, second = sorted(net.sent, key=lambda b: b["num"])
        self.assertEqual((first["num"], second["num"]), (0, 1))
        self.assertEqual(first["ds"], second["ds"])  # same data session across the lazy re-creation

    def test_idle_user_is_forgotten_and_returns_with_a_new_data_session(self):
        net = FakeNet()
        d = self.make(net, user_idle_timeout=0.2)
        click(d, U1)
        self.assertTrue(wait_until(lambda: len(net.sent) == 1, timeout=3))  # idle eviction sealed it
        self.assertEqual(d.stats()["users"], 0)
        click(d, U1)
        d.flush()
        self.assertTrue(wait_until(lambda: len(net.sent) == 2))
        first, second = net.sent
        self.assertNotEqual(first["ds"], second["ds"])
        self.assertEqual(second["num"], 0)

    def test_max_users_evicts_least_recently_active(self):
        net = FakeNet()
        d = self.make(net, max_users=2)
        click(d, U1)
        click(d, U2)
        click(d, U1)  # U2 is now the least recently active
        click(d, U3)
        self.assertTrue(wait_until(lambda: len(net.sent) == 1))
        self.assertEqual(net.sent[0]["uid"], str(U2))
        self.assertEqual(d.stats()["users"], 2)

    def test_buffer_memory_cap_seals_oldest_first(self):
        net = FakeNet()
        d = self.make(net, max_buffered_bytes=200)
        click(d, U1, "x" * 120)
        click(d, U2, "y" * 120)  # over the cap: U1's (older) batch is sealed early
        self.assertTrue(wait_until(lambda: len(net.sent) == 1))
        self.assertEqual(net.sent[0]["uid"], str(U1))
        self.assertLessEqual(d.stats()["buffered_bytes"], 200)

    def test_large_buffer_is_sealed_at_the_size_threshold(self):
        net = FakeNet()
        d = self.make(net)
        while not net.sent:
            click(d, U1, "z" * 1000)
            if d.stats()["buffered_bytes"] == 0:
                break
        self.assertTrue(wait_until(lambda: len(net.sent) == 1))
        self.assertGreaterEqual(len(net.sent[0]["body"]), sd_mod._SEAL_BYTES)

    def test_onboarding_milestones_are_sent_verbatim_without_a_counter(self):
        net = FakeNet()
        d = self.make(net)
        d.report_onboarding_milestone(U1, "step")
        d.report_onboarding_milestone(U1, "step")
        d.flush()
        self.assertTrue(wait_until(lambda: len(net.sent) == 1))
        body = net.sent[0]["body"]
        self.assertEqual(event_ids(body), [KEvents.ONBOARDING_MILESTONE, KEvents.ONBOARDING_MILESTONE])
        self.assertEqual(body.count(b"step"), 2)
        self.assertNotIn(b"(#", body)

    def test_events_after_shutdown_are_refused(self):
        net = FakeNet()
        d = self.make(net)
        d.shutdown(1)
        click(d, U1)
        self.assertEqual(d.stats()["buffered_bytes"], 0)


class ChunkedSealingTest(_Base):
    """Loops that seal many users do it _SEAL_CHUNK at a time, releasing the lock in between, so a
    reporting thread never waits behind a mass seal. Chunk size patched to 2 to make it observable."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(sd_mod, "_SEAL_CHUNK", 2)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _count_lock_holds(self, d):
        """Counts the sealing chunks (each one is one hold of _lock)."""
        calls = []
        original = d._seal_oldest_in_chunk

        def counting(due, limit=sd_mod._SEAL_CHUNK):
            n = original(due, limit)
            calls.append(n)
            return n

        d._seal_oldest_in_chunk = counting
        return calls

    def test_maintenance_seals_every_aged_user_across_several_lock_holds(self):
        net = FakeNet()
        d = self.make(net, flush_interval=3600.0)
        users = [guid.from_uint64(30_000 + i) for i in range(7)]
        for u in users:
            click(d, u)
        calls = self._count_lock_holds(d)
        d._flush_interval = 0.0  # everything is now old enough
        d._maintain()
        self.assertTrue(wait_until(lambda: len(net.sent) == 7))
        sealing = [n for n in calls if n]
        self.assertEqual(sum(sealing), 7)
        self.assertLessEqual(max(sealing), 2)  # never more than one chunk per hold of the lock

    def test_flush_of_everyone_seals_across_chunks(self):
        net = FakeNet()
        d = self.make(net)
        for i in range(5):
            click(d, guid.from_uint64(31_000 + i))
        calls = self._count_lock_holds(d)
        d.flush()
        self.assertTrue(wait_until(lambda: len(net.sent) == 5))
        self.assertEqual([n for n in calls if n], [2, 2, 1])

    def test_reporting_thread_seals_at_most_one_chunk_and_maintenance_finishes(self):
        net = FakeNet(result=SendResult.UNREACHABLE)  # keep sealed batches countable
        d = self.make(net, max_buffered_bytes=10_000_000)
        for i in range(6):
            click(d, guid.from_uint64(32_000 + i), "x" * 100)
        d._max_buffered_bytes = 200  # far over the cap now: the next report triggers memory pressure
        sealed_before = d.stats()["pending_batches"]
        # Only the reporting call may seal here: the maintenance thread's pass is a no-op meanwhile.
        with mock.patch.object(d, "_maintain"):
            click(d, guid.from_uint64(32_999), "y" * 100)
            self.assertEqual(d.stats()["pending_batches"] - sealed_before, 2)  # one chunk, not all of them
            self.assertGreater(d.stats()["buffered_bytes"], 200)
        d._maintain()  # the next maintenance pass seals the remaining excess
        self.assertLessEqual(d.stats()["buffered_bytes"], 200)


class UploadTest(_Base):
    def test_each_users_batches_upload_in_order(self):
        net = FakeNet(delay=0.005)
        d = self.make(net)
        users = [guid.from_uint64(5000 + i) for i in range(6)]
        for _ in range(4):
            for u in users:
                click(d, u)
            d.flush()
        self.assertTrue(wait_until(lambda: len(net.sent) == 24, timeout=10))
        self.assertFalse(net.overlap, "two batches of one user were in flight at once")
        for u in users:
            self.assertEqual([s["num"] for s in net.for_user(u)], [0, 1, 2, 3])

    def test_refused_batch_holds_back_only_its_own_user(self):
        sd_mod._REJECTED_RETRY_S, saved = 0.05, sd_mod._REJECTED_RETRY_S
        self.addCleanup(setattr, sd_mod, "_REJECTED_RETRY_S", saved)
        net = FakeNet()
        net.results_by_user[str(U1)] = SendResult.REJECTED
        d = self.make(net)
        logs = self.assertLogs("keewano_sdk", level="ERROR")  # the repeated "refused" errors
        logs.__enter__()
        self.addCleanup(logs.__exit__, None, None, None)
        click(d, U1)
        d.flush(U1)
        click(d, U1)
        d.flush(U1)
        click(d, U2)
        d.flush(U2)
        self.assertTrue(wait_until(lambda: len(net.for_user(U2)) == 1))
        # Past the attempt budget U1's head batch becomes a drop marker; then U1's queue moves on.
        # Wait for the marker itself, not a call count: a call is counted before FakeNet reads its
        # result, so clearing the refusal on a count can let the real batch through mid-attempt.
        # is_marker is set only once the marker has replaced the batch, so after it nothing can.
        self.assertTrue(wait_until(lambda: any(r.is_marker for r in list(d._pending.values())), timeout=5))
        # ...and only after the whole attempt budget was refused (calls also counts U2's one upload).
        self.assertGreaterEqual(net.calls - 1, MAX_BATCH_ATTEMPTS)
        net.results_by_user.clear()
        self.assertTrue(wait_until(lambda: len(net.for_user(U1)) == 2, timeout=5))
        head, nxt = sorted(net.for_user(U1), key=lambda s: s["num"])
        self.assertEqual(event_ids(head["body"]), [KEvents.BATCH_DROPPED])
        self.assertEqual(event_ids(nxt["body"]), [KEvents.BUTTON_CLICK])

    def test_unreachable_keeps_batches_and_ephemeral_shutdown_reports_loss(self):
        net = FakeNet(result=SendResult.UNREACHABLE)
        d = self.make(net)
        click(d, U1)
        d.flush()
        self.assertTrue(wait_until(lambda: net.calls >= 1))
        self.assertEqual(d.stats()["pending_batches"], 1)
        with self.assertLogs("keewano_sdk", level="WARNING") as cm:
            remaining = d.shutdown(0.5)
        self.assertEqual(remaining, 1)
        self.assertTrue(any("lost" in m for m in cm.output))

    def test_shutdown_seals_and_uploads_everything(self):
        net = FakeNet()
        d = self.make(net)
        for i in range(20):
            click(d, guid.from_uint64(7000 + i))
        self.assertEqual(d.shutdown(5), 0)
        self.assertEqual(len(net.sent), 20)

    def test_pending_cap_collapses_oldest_batches_into_markers(self):
        net = FakeNet(result=SendResult.UNREACHABLE)
        cap = 3 * (sd_mod._RECORD_OVERHEAD + 600)
        d = self.make(net, max_pending_bytes=cap)
        for i in range(5):
            click(d, guid.from_uint64(8000 + i), "p" * 500)
            d.flush()
        self.assertTrue(wait_until(lambda: d.stats()["pending_bytes"] <= cap))
        net.result = SendResult.ACCEPTED
        d.shutdown(5)
        kinds = [event_ids(s["body"]) for s in sorted(net.sent, key=lambda s: s["uid"])]
        self.assertIn([KEvents.BATCH_DROPPED], kinds)
        self.assertEqual(kinds[-1], [KEvents.BUTTON_CLICK])  # the newest survived

    def test_custom_event_set_is_registered_before_upload(self):
        net = FakeNet()
        net.ce_lookup = CustomEventLookup(has_mapping=False, need_to_register=True)
        ce = CustomEventSet(version=77, event_count=1, gzip_data=b"\x1f\x8b")
        d = self.make(net, custom_event_set=ce)
        d.add_event_uint(U1, 2500, 5)
        d.flush()
        self.assertTrue(wait_until(lambda: len(net.sent) == 1))
        self.assertEqual(net.registered, [77])
        self.assertEqual(net.sent[0]["ce"], 77)

    def test_test_user_name_tags_every_batch(self):
        net = FakeNet()
        d = self.make(net, test_user_name="qa")
        click(d, U1)
        d.flush()
        self.assertTrue(wait_until(lambda: len(net.sent) == 1))
        self.assertEqual(net.sent[0]["tester"], "qa")


class ConcurrencyInvariantTest(_Base):
    def _run(self, work_root=None):
        net = FakeNet()
        d = make(
            net,
            work_root=work_root,
            flush_interval=0.05,
            max_users=50,
            max_buffered_bytes=4096,
        )
        users = [guid.from_uint64(20_000 + i) for i in range(200)]
        per_thread = 800

        def worker(t):
            for i in range(per_thread):
                click(d, users[(t * 7919 + i * 31) % len(users)], f"{t}:{i}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(d.shutdown(30), 0)
        self.assertFalse(net.overlap)

        names = []
        sessions = {}
        for s in net.sent:
            names.extend(_strings(s["body"]))
            sessions.setdefault((s["uid"], s["ds"]), []).append(s["num"])
            self.assertEqual(s["install"], s["uid"])
        # Every event arrived exactly once ...
        self.assertEqual(len(names), 8 * per_thread)
        self.assertEqual(len(set(names)), 8 * per_thread)
        # ... and within every data session the batch numbers are 0..n-1, uploaded in order.
        for nums in sessions.values():
            self.assertEqual(nums, list(range(len(nums))))
        # LRU eviction happened, so some users had more than one data session.
        self.assertGreater(len(sessions), len(users))

    def test_ephemeral(self):
        self._run()

    def test_persistent(self):
        self._run(os.path.join(self.tmp, "server"))


def _strings(body):
    out, off = [], 0
    while off < len(body):
        off += 6
        length, shift = 0, 0
        while True:
            byte = body[off]
            off += 1
            length |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                break
            shift += 7
        out.append(body[off : off + length].decode())
        off += length
    return out


class PersistentStorageTest(_Base):
    def test_unsent_batches_survive_a_restart(self):
        root = os.path.join(self.tmp, "server")
        net = FakeNet(result=SendResult.UNREACHABLE)
        d = make(net, work_root=root)
        click(d, U1)
        click(d, U2)
        self.assertEqual(d.shutdown(0.2), 2)
        files = [n for n, _ in work_dir_lease.list_files(os.path.join(root, "worker-0"), ".kwub")]
        self.assertEqual(len(files), 2)

        net2 = FakeNet()
        self.make(net2, work_root=root)
        self.assertTrue(wait_until(lambda: len(net2.sent) == 2))
        self.assertEqual({s["install"] for s in net2.sent}, {str(U1), str(U2)})
        self.assertEqual({s["uid"] for s in net2.sent}, {str(U1), str(U2)})
        self.assertTrue(wait_until(lambda: not work_dir_lease.list_files(os.path.join(root, "worker-0"), ".kwub")))

    def test_batch_file_is_a_standard_kwub(self):
        root = os.path.join(self.tmp, "server")
        net = FakeNet(result=SendResult.UNREACHABLE)
        d = make(net, work_root=root)
        click(d, U1)
        d.shutdown(0)
        ((name, path),) = work_dir_lease.list_files(os.path.join(root, "worker-0"), ".kwub")
        b = KBatch(guid.EMPTY, guid.EMPTY, guid.EMPTY)
        self.assertGreater(serializer.load_from_file(path, b), 0)
        self.assertEqual(b.user_id, U1)
        self.assertEqual(b.batch_num, 0)
        self.assertEqual(event_ids(b.data.to_bytes()), [KEvents.BUTTON_CLICK])

    @unittest.skipUnless(work_dir_lease.LOCKING_SUPPORTED, "needs OS file locking")
    def test_concurrent_processes_get_separate_work_dirs(self):
        root = os.path.join(self.tmp, "server")
        a = work_dir_lease.acquire(root)
        b = work_dir_lease.acquire(root)
        try:
            self.assertNotEqual(a.path, b.path)
        finally:
            a.release()
            b.release()
        c = work_dir_lease.acquire(root)
        self.assertTrue(c.path.endswith("worker-0"))  # released leases are reusable
        c.release()

    @unittest.skipUnless(work_dir_lease.LOCKING_SUPPORTED, "needs OS file locking")
    def test_batches_of_an_expired_lease_are_taken_over(self):
        root = os.path.join(self.tmp, "server")
        expired = os.path.join(root, "worker-7")
        os.makedirs(expired)
        b = KBatch(U3, U3, guid.new_guid())
        encoding.event_str(b.data, int(time.time()), KEvents.BUTTON_CLICK, "left-over")
        serializer.save_to_file(b, os.path.join(expired, sd_mod._batch_file_name(4, U3)))
        net = FakeNet()
        self.make(net, work_root=root)
        self.assertTrue(wait_until(lambda: len(net.sent) == 1))
        self.assertEqual(net.sent[0]["uid"], str(U3))
        self.assertEqual(work_dir_lease.list_files(expired, ".kwub"), [])

    @unittest.skipUnless(work_dir_lease.LOCKING_SUPPORTED, "needs OS file locking")
    def test_a_live_lease_is_not_taken_over(self):
        root = os.path.join(self.tmp, "server")
        held = work_dir_lease.acquire(root)  # worker-0, leased by "another process"
        self.addCleanup(held.release)
        b = KBatch(U3, U3, guid.new_guid())
        encoding.event(b.data, int(time.time()), KEvents.LOW_MEM_WARNING)
        serializer.save_to_file(b, os.path.join(held.path, sd_mod._batch_file_name(0, U3)))
        net = FakeNet()
        d = self.make(net, work_root=root)
        click(d, U1)
        d.flush()
        self.assertTrue(wait_until(lambda: len(net.sent) == 1))
        time.sleep(0.2)
        self.assertEqual([s["uid"] for s in net.sent], [str(U1)])
        self.assertEqual(len(work_dir_lease.list_files(held.path, ".kwub")), 1)

    def test_restart_ignores_foreign_files_and_removes_torn_writes(self):
        root = os.path.join(self.tmp, "server")
        work = os.path.join(root, "worker-0")
        _leftover_batch(work, 2, U1)
        foreign = ["notes.kwub", "1_2_3.kwub", "x_" + "0" * 32 + ".kwub", "1_abc.kwub", "readme.txt"]
        for name in foreign:
            with open(os.path.join(work, name), "w") as f:
                f.write("not ours")
        with open(os.path.join(work, "9_torn.tmp"), "w") as f:
            f.write("half a batch")
        net = FakeNet()
        self.make(net, work_root=root)
        self.assertTrue(wait_until(lambda: len(net.sent) == 1))
        time.sleep(0.1)
        self.assertEqual([s["uid"] for s in net.sent], [str(U1)])
        self.assertEqual(sorted(os.listdir(work)), sorted(foreign + [".lock"]))  # never uploaded nor deleted

    def test_batch_file_names_round_trip_and_reject_anything_else(self):
        self.assertEqual(sd_mod._parse_batch_file_name(sd_mod._batch_file_name(42, U2)), (42, U2))
        for name in (
            "42.kwub",
            "42_" + "ab" * 15 + ".kwub",
            "x_" + "ab" * 16 + ".kwub",
            "1_" + "zz" * 16 + ".kwub",
            "1_" + "ab" * 16 + ".tmp",
        ):
            with self.subTest(name=name):
                self.assertIsNone(sd_mod._parse_batch_file_name(name))

    def test_unusable_data_dir_falls_back_to_memory(self):
        blocker = os.path.join(self.tmp, "file")
        with open(blocker, "w") as f:
            f.write("x")
        net = FakeNet()
        with self.assertLogs("keewano_sdk", level="ERROR"):
            d = self.make(net, work_root=os.path.join(blocker, "server"))
            click(d, U1)
            d.flush()
            self.assertTrue(wait_until(lambda: len(net.sent) == 1))
        self.assertFalse(d.persistent)


def _leftover_batch(directory, seq, user, ce_version=0, text="left-over"):
    """Writes a sealed one-event batch file for ``user`` into ``directory``, as a previous run would."""
    os.makedirs(directory, exist_ok=True)
    b = KBatch(user, user, guid.new_guid())
    b.custom_events_version = ce_version
    encoding.event_str(b.data, int(time.time()), KEvents.BUTTON_CLICK, text)
    path = os.path.join(directory, sd_mod._batch_file_name(seq, user))
    serializer.save_to_file(b, path)
    return path


def _load(path):
    b = KBatch(guid.EMPTY, guid.EMPTY, guid.EMPTY)
    return b if serializer.load_from_file(path, b) > 0 else None


def _resume_uploads(d):
    """Ends an unreachable back-off now, so a test does not wait out _UNREACHABLE_BACKOFF_S."""
    with d._cond:
        d._backoff_until = 0.0
        d._cond.notify_all()


class PendingCapTest(_Base):
    def test_persistent_markers_are_rewritten_on_disk_keeping_identity(self):
        root = os.path.join(self.tmp, "server")
        net = FakeNet(result=SendResult.UNREACHABLE)
        cap = 3 * (sd_mod._RECORD_OVERHEAD + 700)
        with self.assertLogs("keewano_sdk", level="ERROR"):
            d = self.make(net, work_root=root, max_pending_bytes=cap)
            for i in range(5):
                click(d, guid.from_uint64(8100 + i), "p" * 500)
                d.flush()
            self.assertTrue(wait_until(lambda: any(r.is_marker for r in list(d._pending.values()))))
        self.assertTrue(wait_until(lambda: d.stats()["pending_bytes"] <= cap))
        with d._lock:
            markers = [r for r in d._pending.values() if r.is_marker]
        self.assertTrue(markers)
        for rec in markers:
            self.assertIsNone(rec.batch)  # still on disk, not moved to RAM
            b = _load(rec.filename)
            self.assertEqual(event_ids(b.data.to_bytes()), [KEvents.BATCH_DROPPED])
            self.assertEqual((b.user_id, b.batch_num, b.custom_events_version), (rec.user_id, 0, 0))
            self.assertEqual(rec.size, os.path.getsize(rec.filename))

    def test_markers_are_discarded_when_even_markers_do_not_fit(self):
        net = FakeNet(result=SendResult.UNREACHABLE)
        with self.assertLogs("keewano_sdk", level="ERROR"):
            d = self.make(net, max_pending_bytes=1)  # not even one record fits
            for i in range(4):
                click(d, guid.from_uint64(8200 + i))
            d.flush()
            self.assertTrue(wait_until(lambda: d.stats()["pending_batches"] == 0))
        self.assertEqual(d.stats()["pending_bytes"], 0)


class UploadEdgeCaseTest(_Base):
    def test_unreadable_batch_file_is_dropped_without_an_upload(self):
        root = os.path.join(self.tmp, "server")
        net = FakeNet()
        with mock.patch.object(sd_mod.serializer, "load_from_file", return_value=-1):
            d = self.make(net, work_root=root)
            click(d, U1)
            d.flush()
            self.assertTrue(wait_until(lambda: d.stats()["pending_batches"] == 0 and net.calls == 0))
            time.sleep(0.1)
        self.assertEqual(net.calls, 0)
        self.assertEqual(work_dir_lease.list_files(os.path.join(root, "worker-0"), ".kwub"), [])

    def test_unconfirmed_custom_event_mapping_backs_off_like_an_outage(self):
        net = FakeNet()
        net.ce_lookup = CustomEventLookup(has_mapping=False, need_to_register=False)  # e.g. lookup failed
        ce = CustomEventSet(version=78, event_count=1, gzip_data=b"\x1f\x8b")
        d = self.make(net, custom_event_set=ce)
        d.add_event_uint(U1, 2500, 5)
        d.flush()
        self.assertTrue(wait_until(lambda: d._backoff_until > time.monotonic()))
        self.assertEqual((net.calls, net.registered), (0, []))
        self.assertEqual(d.stats()["pending_batches"], 1)
        net.ce_lookup = CustomEventLookup(has_mapping=True, need_to_register=False)
        _resume_uploads(d)
        self.assertTrue(wait_until(lambda: len(net.sent) == 1))
        self.assertEqual(net.sent[0]["ce"], 78)

    def test_custom_event_version_without_definitions_is_refused_then_dropped(self):
        sd_mod._REJECTED_RETRY_S, saved = 0.01, sd_mod._REJECTED_RETRY_S
        self.addCleanup(setattr, sd_mod, "_REJECTED_RETRY_S", saved)
        root = os.path.join(self.tmp, "server")
        # A previous run's batch stamped with a custom-event version nobody kept the definitions of.
        _leftover_batch(os.path.join(root, "worker-0"), 0, U1, ce_version=91)
        net = FakeNet()
        net.ce_lookup = CustomEventLookup(has_mapping=False, need_to_register=True)
        with self.assertLogs("keewano_sdk", level="ERROR") as cm:
            self.make(net, work_root=root)
            self.assertTrue(wait_until(lambda: len(net.sent) == 1, timeout=5))
        self.assertTrue(any("No custom-event definitions for version 91" in m for m in cm.output))
        self.assertEqual(net.registered, [])
        self.assertEqual(event_ids(net.sent[0]["body"]), [KEvents.BATCH_DROPPED])
        self.assertEqual(net.sent[0]["ce"], 0)  # a marker never waits on a registration

    def test_custom_event_definitions_are_registered_from_a_previous_runs_map_file(self):
        root = os.path.join(self.tmp, "server")
        ce = CustomEventSet(version=79, event_count=2, gzip_data=b"\x1f\x8bdefs")
        net = FakeNet(result=SendResult.UNREACHABLE)
        d = make(net, work_root=root, custom_event_set=ce)
        d.add_event_uint(U1, 2500, 5)
        self.assertEqual(d.shutdown(0), 1)
        self.assertTrue(os.path.exists(os.path.join(root, "worker-0", "79" + sd_mod._MAP_SUFFIX)))

        net2 = FakeNet()  # the new run no longer ships version 79 (the app was updated)
        net2.ce_lookup = CustomEventLookup(has_mapping=False, need_to_register=True)
        self.make(net2, work_root=root)
        self.assertTrue(wait_until(lambda: len(net2.sent) == 1))
        self.assertEqual(net2.registered, [79])
        self.assertEqual(net2.sent[0]["ce"], 79)


@unittest.skipUnless(work_dir_lease.LOCKING_SUPPORTED, "needs OS file locking")
class TakeOverTest(_Base):
    def test_running_engine_periodically_takes_over_expired_directories(self):
        sd_mod._TAKE_OVER_INTERVAL_S, saved = 0.05, sd_mod._TAKE_OVER_INTERVAL_S
        self.addCleanup(setattr, sd_mod, "_TAKE_OVER_INTERVAL_S", saved)
        root = os.path.join(self.tmp, "server")
        net = FakeNet()
        ce = CustomEventSet(version=80, event_count=1, gzip_data=b"\x1f\x8b")
        d = self.make(net, work_root=root, custom_event_set=ce, flush_interval=0.2)
        click(d, U1)
        d.flush()
        self.assertTrue(wait_until(lambda: len(net.sent) == 1))
        own = d._lease.path

        # A worker that died after startup: its lease is free, its batches and map are stranded. Built
        # aside and renamed into place, so a take-over pass never sees it half-written.
        net.ce_lookup = CustomEventLookup(has_mapping=False, need_to_register=True)
        staging = os.path.join(self.tmp, "staging")
        _leftover_batch(staging, 3, U2, ce_version=81, text="stranded-a")
        _leftover_batch(staging, 5, U2, ce_version=81, text="stranded-b")
        KStorage.save_custom_event_set_to_file(
            os.path.join(staging, "81" + sd_mod._MAP_SUFFIX), CustomEventSet(81, 1, b"\x1f\x8b")
        )
        KStorage.save_custom_event_set_to_file(os.path.join(staging, "80" + sd_mod._MAP_SUFFIX), ce)  # we have it
        with open(os.path.join(staging, "torn.tmp"), "w") as f:
            f.write("x")
        dead = os.path.join(root, "worker-9")
        os.rename(staging, dead)

        self.assertTrue(wait_until(lambda: len(net.for_user(U2)) == 2))
        bodies = [s["body"] for s in net.for_user(U2)]
        self.assertIn(b"stranded-a", bodies[0])  # the dead worker's order is kept
        self.assertIn(b"stranded-b", bodies[1])
        self.assertIn(81, net.registered)  # registered from the map file moved into our directory
        self.assertTrue(os.path.exists(os.path.join(own, "81" + sd_mod._MAP_SUFFIX)))
        self.assertEqual(sorted(os.listdir(dead)), [".lock"])  # batches, maps and torn writes all gone


class ClientParityTest(unittest.TestCase):
    """Every server report method must write exactly the bytes its client KEventDispatcher namesake
    writes — the two engines share the method list and the wire format."""

    CASES = [
        ("add_event", (KEvents.LOW_MEM_WARNING,)),
        ("add_event_str", (2600, "s")),
        ("add_event_uint", (2600, 7)),
        ("add_event_int", (2600, -7)),
        ("add_event_float", (2600, 1.5)),
        ("add_event_ushort_pair", (2600, 3, 4)),
        ("add_event_bool", (2600, True)),
        ("assign_to_ab_test_group", ("t", "B")),
        ("report_in_app_purchase_usd", ("p", 499)),
        ("report_in_app_purchase_local", ("p", 4.99, "EUR")),
        ("report_ad_offered", ("ad", 2)),
        ("report_ad_revenue_usd", ("ad", 2)),
        ("report_ad_revenue_local", ("ad", 0.02, "EUR")),
        ("report_subscription_revenue_usd", ("vip", 999)),
        ("report_subscription_revenue_local", ("vip", 9.99, "EUR")),
        ("report_item_exchange", ("shop", [Item("coins", 5)], [Item("sword")])),
        ("report_items_reset", ("init", [Item("coins", 500)])),
        ("report_in_app_purchase_items_granted", ("p", [Item("gems", 100)])),
        ("report_ad_items_granted", ("ad", [Item("coins", 1)])),
        ("report_subscription_items_granted", ("vip", [Item("chest")])),
        ("report_install_campaign", ("summer",)),
        ("report_game_language", ("fr",)),
        ("report_onboarding_milestone", ("step",)),
        ("log_error", ("boom",)),
    ]

    def test_server_methods_encode_like_the_client(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch("time.time", return_value=1_700_000_000.0):
            for method, args in self.CASES:
                with self.subTest(method=method):
                    client = KEventDispatcher(
                        working_directory=os.path.join(tmp, method),
                        endpoint="http://127.0.0.1:1/base",
                        app_secret="t",
                        initial_consent=UserConsentState.NOT_REQUIRED,
                        install_id=U1,
                        initial_user_id=U1,
                        data_session_id=guid.new_guid(),
                        sdk_version="1",
                    )
                    try:
                        getattr(client, method)(*args)
                        expected = client._in_batch.data.to_bytes()
                    finally:
                        client.stop()
                    server = make(FakeNet())
                    try:
                        getattr(server, method)(U1, *args)
                        actual = server._users[U1].batch.data.to_bytes()
                    finally:
                        server.shutdown(0)
                    self.assertGreaterEqual(len(expected), 6)  # at least the event header: not vacuous
                    self.assertEqual(actual, expected)


class OnDiskMarkerRewriteTest(_Base):
    """Collapsing a batch stored on disk into a drop marker must never leave two versions of it: a
    marker in RAM next to the old, full file would orphan the file here and get it uploaded alongside
    the marker by whichever process next finds it."""

    def _disk_record(self):
        net = FakeNet(result=SendResult.UNREACHABLE)  # keep the batch queued
        d = self.make(net, work_root=os.path.join(self.tmp, "server"))
        click(d, U1, "full-batch")
        d.flush(U1)
        self.assertTrue(wait_until(lambda: any(r.filename for r in d._pending.values())))
        with d._lock:
            rec = next(iter(d._pending.values()))
            rec.in_flight = True  # reserved, as both real callers do
        with open(rec.filename, "rb") as f:
            original = f.read()
        return d, rec, rec.filename, original

    def test_rewrite_succeeds(self):
        d, rec, path, _ = self._disk_record()
        size = d._make_marker(rec)
        self.assertEqual(size, os.path.getsize(path))
        self.assertTrue(rec.is_marker)
        b = KBatch(guid.EMPTY, guid.EMPTY, guid.EMPTY)
        serializer.load_from_file(path, b)
        self.assertEqual(event_ids(b.data.to_bytes()), [KEvents.BATCH_DROPPED])

    def test_rewrite_fails_so_the_old_file_is_removed_and_the_marker_kept_in_ram(self):
        d, rec, path, _ = self._disk_record()
        with mock.patch.object(serializer, "save_to_file", return_value=0):
            size = d._make_marker(rec)
        self.assertFalse(os.path.exists(path))  # no orphan full batch left behind
        self.assertIsNone(rec.filename)
        self.assertTrue(rec.is_marker)
        self.assertEqual(size, rec.batch.data.length)
        self.assertEqual(event_ids(rec.batch.data.to_bytes()), [KEvents.BATCH_DROPPED])
        self.assertEqual((rec.batch.user_id, rec.batch.batch_num), (U1, 0))  # identity kept

    def test_rewrite_and_removal_both_fail_so_nothing_changes(self):
        d, rec, path, original = self._disk_record()
        with mock.patch.object(serializer, "save_to_file", return_value=0), mock.patch.object(
            sd_mod.os, "remove", side_effect=PermissionError("read-only")
        ), self.assertLogs("keewano_sdk", level="ERROR"):
            result = d._make_marker(rec)
        self.assertIsNone(result)
        self.assertEqual((rec.filename, rec.batch, rec.is_marker, rec.attempts), (path, None, False, 0))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), original)  # the full batch is still there, untouched

    def test_cap_enforcement_leaves_an_unchangeable_batch_alone_and_retries_next_pass(self):
        d, rec, path, _ = self._disk_record()
        with d._lock:
            rec.in_flight = False
            before = d._pending_bytes
            d._max_pending_bytes = before - 1  # over the cap by a byte; the (smaller) marker will fit
        with mock.patch.object(serializer, "save_to_file", return_value=0), mock.patch.object(
            sd_mod.os, "remove", side_effect=PermissionError("read-only")
        ), self.assertLogs("keewano_sdk", level="ERROR") as logs:
            d._reduce_pending()
        self.assertFalse(any("dropped the events" in m for m in logs.output))  # nothing was dropped
        with d._lock:
            self.assertEqual((rec.in_flight, rec.is_marker, d._pending_bytes), (False, False, before))
        d._reduce_pending()  # the disk recovered: the next pass converts it
        self.assertTrue(rec.is_marker)
        self.assertTrue(os.path.exists(path))


class DropMarkerTest(unittest.TestCase):
    def test_marker_keeps_identity_and_drops_custom_event_version(self):
        rec_batch = KBatch(U1, U1, guid.from_uint64(9))
        rec_batch.batch_num = 3
        rec_batch.batch_start_time = 123
        rec_batch.custom_events_version = 55
        encoding.event_str(rec_batch.data, 123, KEvents.BUTTON_CLICK, "x")
        d = KServerDispatcher(
            endpoint="e",
            app_secret="t",
            sdk_version="1",
            work_root=None,
            flush_interval=1,
            user_idle_timeout=1,
            max_users=1,
            max_buffered_bytes=1,
            max_pending_bytes=1,
            network=FakeNet(),
        )
        rec = sd_mod._Pending(0, U1, rec_batch.data.length, rec_batch, None)
        d._make_marker(rec)
        self.assertTrue(rec.is_marker)
        self.assertEqual((rec_batch.batch_num, rec_batch.user_id, rec_batch.custom_events_version), (3, U1, 0))
        body = rec_batch.data.to_bytes()
        self.assertEqual(
            struct.unpack_from("<IHI", body, 0), (123, KEvents.BATCH_DROPPED, KBatchDropReason.TOO_MANY_UNSENT_EVENTS)
        )


@unittest.skipUnless(hasattr(os, "fork"), "needs os.fork")
class ForkTest(_Base):
    def test_child_gets_a_clean_engine_and_parent_keeps_its_events(self):
        net = FakeNet()
        d = self.make(net)
        click(d, U1, "parent")
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            code = 1
            try:
                os.close(r)
                d.after_fork_in_child()  # what the facade's os.register_at_fork hook does
                st = d.stats()
                clean = st["users"] == 0 and st["buffered_bytes"] == 0
                click(d, U2, "child")
                remaining = d.shutdown(5)
                sent = [(s["uid"], s["body"]) for s in net.sent]
                ok = clean and remaining == 0 and len(sent) == 1 and sent[0][0] == str(U2)
                os.write(w, b"ok" if ok else repr((st, remaining, sent)).encode())
                code = 0
            finally:
                os._exit(code)
        os.close(w)
        _, status = os.waitpid(pid, 0)
        out = os.read(r, 4096)
        os.close(r)
        self.assertEqual(os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status, 0)
        self.assertEqual(out, b"ok")
        # The parent still owns (and alone uploads) the event it collected before the fork.
        d.flush()
        self.assertTrue(wait_until(lambda: len(net.sent) == 1))
        self.assertEqual(net.sent[0]["uid"], str(U1))

    @unittest.skipUnless(work_dir_lease.LOCKING_SUPPORTED, "needs OS file locking")
    def test_forked_child_leases_its_own_dir_and_parent_keeps_its_lease(self):
        root = os.path.join(self.tmp, "server")
        net = FakeNet(result=SendResult.UNREACHABLE)
        d = self.make(net, work_root=root)
        click(d, U1)
        d.flush()
        self.assertTrue(wait_until(lambda: d._lease is not None))
        parent_dir = d._lease.path
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            code = 1
            try:
                os.close(r)
                d.after_fork_in_child()
                click(d, U2)
                d.shutdown(0)
                os.write(w, b"done")
                code = 0
            finally:
                os._exit(code)
        os.close(w)
        os.waitpid(pid, 0)
        self.assertEqual(os.read(r, 16), b"done")
        os.close(r)
        child_files = work_dir_lease.list_files(os.path.join(root, "worker-1"), ".kwub")
        self.assertEqual(len(child_files), 1)
        self.assertIn(U2.to_bytes().hex(), child_files[0][0])
        # The parent's lease is still held: nobody else can acquire its directory.
        probe = work_dir_lease.acquire(root)
        try:
            self.assertNotEqual(probe.path, parent_dir)
        finally:
            probe.release()


if __name__ == "__main__":
    unittest.main()
