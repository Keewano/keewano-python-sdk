"""Unit tests for KStorage: durable identifiers, consent, and the pre-SDK marker."""

import os
import tempfile
import unittest

from keewano_sdk.internal import guid
from keewano_sdk.internal.consent import UserConsentState
from keewano_sdk.internal.storage import KStorage, UserIdentifiers


class KStorageTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()

    def test_creates_base_dir(self):
        nested = os.path.join(self._dir, "a", "b")
        KStorage(nested)
        self.assertTrue(os.path.isdir(nested))

    def test_load_or_init_creates_and_persists_identifiers(self):
        store = KStorage(self._dir)
        ids = store.load_or_init_identifiers()
        # A fresh install: random install id, empty user id.
        self.assertNotEqual(ids.install_id, guid.EMPTY)
        self.assertEqual(ids.user_id, guid.EMPTY)
        # Persisted: a second store instance loads the same install id.
        again = KStorage(self._dir).load_or_init_identifiers()
        self.assertEqual(again.install_id, ids.install_id)

    def test_save_and_reload_identifiers_roundtrip(self):
        store = KStorage(self._dir)
        ids = store.load_or_init_identifiers()
        ids.user_id = guid.from_uint64(1234567890)
        store.save_identifiers(ids)
        reloaded = KStorage(self._dir).load_or_init_identifiers()
        self.assertEqual(reloaded.install_id, ids.install_id)
        self.assertEqual(reloaded.user_id, guid.from_uint64(1234567890))

    def test_consent_defaults_to_none_then_roundtrips(self):
        store = KStorage(self._dir)
        self.assertIsNone(store.load_user_consent_state())
        for state in UserConsentState:
            store.save_user_consent_state(state)
            self.assertEqual(KStorage(self._dir).load_user_consent_state(), state)

    def test_pre_sdk_registration_marker(self):
        store = KStorage(self._dir)
        self.assertFalse(store.has_pre_sdk_registration_been_reported())
        store.mark_pre_sdk_registration_as_reported()
        self.assertTrue(store.has_pre_sdk_registration_been_reported())
        # Durable across instances.
        self.assertTrue(KStorage(self._dir).has_pre_sdk_registration_been_reported())

    def test_corrupt_short_ids_file_reinitializes(self):
        store = KStorage(self._dir)
        store.load_or_init_identifiers()
        # Truncate the ids file to fewer than 32 bytes.
        ids_path = os.path.join(self._dir, "Keewano_Ids")
        with open(ids_path, "wb") as f:
            f.write(b"\x01\x02\x03")
        ids = KStorage(self._dir).load_or_init_identifiers()  # should not raise
        self.assertNotEqual(ids.install_id, guid.EMPTY)

    def test_concurrent_writes_do_not_corrupt_files(self):
        import threading

        store = KStorage(self._dir)
        install = store.load_or_init_identifiers().install_id
        barrier = threading.Barrier(16)

        def hammer(i):
            barrier.wait()  # maximize overlap on the shared "<file>.tmp" name
            for _ in range(20):
                store.save_identifiers(UserIdentifiers(install, guid.from_uint64(i)))
                store.save_user_consent_state(UserConsentState(i % 4))
                store.mark_pre_sdk_registration_as_reported()

        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Files are never torn: a fresh read yields a complete, valid record (and a leftover .tmp
        # never survives).
        reloaded = KStorage(self._dir).load_or_init_identifiers()
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.install_id, install)
        self.assertIn(reloaded.user_id, [guid.from_uint64(i) for i in range(16)])  # some writer won
        self.assertIsNotNone(KStorage(self._dir).load_user_consent_state())
        self.assertTrue(store.has_pre_sdk_registration_been_reported())
        leftovers = [f for f in os.listdir(self._dir) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_unreadable_ids_file_returns_none_not_reminted(self):
        from unittest import mock

        store = KStorage(self._dir)
        ids = store.load_or_init_identifiers()  # create the file
        original_install = ids.install_id
        # An existing-but-unreadable identifiers file must return None (never mint a replacement).
        with mock.patch("builtins.open", side_effect=OSError("disk error")):
            self.assertIsNone(KStorage(self._dir).load_or_init_identifiers())
        # And the on-disk identity is untouched: a later good read returns the original install id.
        self.assertEqual(KStorage(self._dir).load_or_init_identifiers().install_id, original_install)

    def test_corrupt_short_consent_file_returns_none(self):
        store = KStorage(self._dir)
        with open(os.path.join(self._dir, "Keewano_UserConsent"), "wb") as f:
            f.write(b"\x01")  # fewer than 4 bytes
        self.assertIsNone(store.load_user_consent_state())


class CustomEventSetFileTest(unittest.TestCase):
    """The custom-event-set file is local-only, so KStorage writes it as JSON, not a .kwub frame."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()

    def _path(self):
        return os.path.join(self._dir, "3202169926.map.gz")

    def test_round_trips_through_kstorage(self):
        from keewano_sdk.internal.custom_event_set import CustomEventSet

        # version deliberately > int32 max, to exercise uint32 handling.
        gz = bytes([0x1F, 0x8B, 0x08, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05])
        KStorage.save_custom_event_set_to_file(self._path(), CustomEventSet(3_202_169_926, 5, gz))
        loaded = KStorage.load_custom_event_set_from_file(self._path())
        self.assertIsNotNone(loaded)
        self.assertEqual((loaded.version, loaded.event_count, loaded.gzip_data), (3_202_169_926, 5, gz))

    def test_missing_file_returns_none(self):
        self.assertIsNone(KStorage.load_custom_event_set_from_file("/no/such/dir/123.map.gz"))

    def test_truncated_file_reads_as_none(self):
        from keewano_sdk.internal.custom_event_set import CustomEventSet

        KStorage.save_custom_event_set_to_file(self._path(), CustomEventSet(7, 1, b"\x01\x02\x03"))
        with open(self._path(), "rb") as f:
            good = f.read()
        with open(self._path(), "wb") as f:
            f.write(good[: len(good) // 2])  # chop it in half -> invalid JSON
        self.assertIsNone(KStorage.load_custom_event_set_from_file(self._path()))

    def test_unknown_format_version_reads_as_none(self):
        import json

        with open(self._path(), "wb") as f:
            f.write(
                json.dumps({"format_version": 2, "version": 1, "event_count": 0, "gzip_base64": ""}).encode("utf-8")
            )
        self.assertIsNone(KStorage.load_custom_event_set_from_file(self._path()))


class OnboardingCountersFileTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()

    def _path(self):
        return os.path.join(self._dir, "onboarding.counters")

    def test_round_trip(self):
        self.assertEqual(KStorage.load_onboarding_counters(self._path()), {})
        counters = {"level_1": 2, "boss": 1, "tut": 5}
        KStorage.save_onboarding_counters(self._path(), counters)
        self.assertEqual(KStorage.load_onboarding_counters(self._path()), counters)

    def test_values_above_uint32_survive(self):
        KStorage.save_onboarding_counters(self._path(), {"m": 5_000_000_000})
        self.assertEqual(KStorage.load_onboarding_counters(self._path())["m"], 5_000_000_000)

    def test_resave_replaces_whole_file(self):
        KStorage.save_onboarding_counters(self._path(), {"a": 1, "b": 2})
        KStorage.save_onboarding_counters(self._path(), {"a": 9})
        self.assertEqual(KStorage.load_onboarding_counters(self._path()), {"a": 9})

    def test_corrupt_file_reads_as_empty(self):
        with open(self._path(), "wb") as f:
            f.write(b"{not valid json")
        self.assertEqual(KStorage.load_onboarding_counters(self._path()), {})

    def test_non_int_value_rejects_whole_file(self):
        import json

        with open(self._path(), "wb") as f:
            f.write(json.dumps({"a": 1, "b": "oops"}).encode("utf-8"))
        self.assertEqual(KStorage.load_onboarding_counters(self._path()), {})

    def test_wrong_root_type_reads_as_empty(self):
        import json

        with open(self._path(), "wb") as f:
            f.write(json.dumps([1, 2, 3]).encode("utf-8"))
        self.assertEqual(KStorage.load_onboarding_counters(self._path()), {})

    def test_missing_file_yields_empty_map(self):
        self.assertEqual(KStorage.load_onboarding_counters("/no/such/dir/onboarding.counters"), {})

    def test_saving_to_unwritable_path_does_not_raise(self):
        KStorage.save_onboarding_counters("/no/such/dir/onboarding.counters", {"m": 1})  # no exception


class TestUserNameFileTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()

    def _path(self):
        return os.path.join(self._dir, "test_user.info")

    def test_round_trip(self):
        self.assertIsNone(KStorage.load_test_user_name(self._path()))
        KStorage.save_test_user_name(self._path(), "qa-alice")
        self.assertEqual(KStorage.load_test_user_name(self._path()), "qa-alice")

    def test_stored_as_plain_text(self):
        KStorage.save_test_user_name(self._path(), "qa-alice")
        with open(self._path(), "rb") as f:
            self.assertEqual(f.read(), b"qa-alice")

    def test_blank_or_whitespace_reads_as_none(self):
        for blank in ("", "   ", "\n", "  \t\n "):
            with open(self._path(), "w", encoding="utf-8") as f:
                f.write(blank)
            self.assertIsNone(KStorage.load_test_user_name(self._path()), repr(blank))

    def test_surrounding_whitespace_is_trimmed(self):
        with open(self._path(), "w", encoding="utf-8") as f:
            f.write("  qa-alice\n")
        self.assertEqual(KStorage.load_test_user_name(self._path()), "qa-alice")

    def test_missing_file_returns_none(self):
        self.assertIsNone(KStorage.load_test_user_name("/no/such/dir/test_user.info"))

    def test_invalid_utf8_reads_as_none(self):
        # A corrupt/non-UTF-8 file makes f.read() raise UnicodeDecodeError (a ValueError, not an
        # OSError). It must read as "no test user", not propagate out and disable the SDK.
        with open(self._path(), "wb") as f:
            f.write(b"\xff\xfe\x00\x80qa")  # invalid UTF-8 bytes
        self.assertIsNone(KStorage.load_test_user_name(self._path()))

    def test_saving_to_unwritable_path_does_not_raise(self):
        KStorage.save_test_user_name("/no/such/dir/test_user.info", "qa-alice")  # no exception


if __name__ == "__main__":
    unittest.main()
