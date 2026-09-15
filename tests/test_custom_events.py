"""Tests for custom-event set wiring: the default (empty) codegen target and the dispatcher's
initialization behavior (persist the set and stamp the batch version).
"""

import os
import tempfile
import unittest

import base64
import gzip

from keewano_sdk import CustomEventSet as PublicCustomEventSet
from keewano_sdk.internal import guid
from keewano_sdk.internal.consent import UserConsentState
from keewano_sdk.internal.custom_event_set import CustomEventSet
from keewano_sdk.internal.dispatcher import KEventDispatcher


def _make_dispatcher(work_dir, ce_set):
    return KEventDispatcher(
        working_directory=work_dir,
        endpoint="http://127.0.0.1:1/unused",  # never contacted in these tests
        app_secret="tok",
        initial_consent=UserConsentState.NOT_REQUIRED,
        install_id=guid.new_guid(),
        initial_user_id=guid.EMPTY,
        data_session_id=guid.new_guid(),
        sdk_version="1.0.0",
        custom_event_set=ce_set,
    )


class CustomEventSetTest(unittest.TestCase):
    def test_public_export_is_the_same_type(self):
        self.assertIs(PublicCustomEventSet, CustomEventSet)

    def test_default_is_empty(self):
        ces = CustomEventSet()
        self.assertEqual((ces.version, ces.event_count, ces.gzip_data), (0, 0, b""))

    def test_from_gzip_base64_decodes_verbatim(self):
        gz = gzip.compress(b'{"events":["LevelUp"]}')
        ces = CustomEventSet.from_gzip_base64(123, 1, base64.b64encode(gz).decode("ascii"))
        self.assertEqual(ces.version, 123)
        self.assertEqual(ces.event_count, 1)
        self.assertEqual(ces.gzip_data, gz)  # decoded, not re-compressed


class CustomEventsDispatcherTest(unittest.TestCase):
    def test_empty_set_tags_batches_version_zero_and_writes_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            disp = _make_dispatcher(d, CustomEventSet())
            try:
                self.assertEqual(disp._in_batch.custom_events_version, 0)
                self.assertEqual(disp._sending_batch.custom_events_version, 0)
                maps = [f for f in os.listdir(d) if f.endswith(".map.gz")]
                self.assertEqual(maps, [])
            finally:
                disp.stop()

    def test_add_event_bool_wire_encoding(self):
        """A bool event writes its header then a single byte: 2 = true, 1 = false."""
        import struct

        with tempfile.TemporaryDirectory() as d:
            disp = _make_dispatcher(d, CustomEventSet())
            try:
                event_id = 2501  # a custom-event id
                disp.add_event_bool(event_id, True)
                disp.add_event_bool(event_id, False)
                body = disp._in_batch.data.to_bytes()
            finally:
                disp.stop()

        # Two events back to back: [uint32 ts][uint16 id][byte flag] each.
        self.assertEqual(len(body), 2 * (4 + 2 + 1))
        _ts1, id1, flag1 = struct.unpack_from("<IHB", body, 0)
        _ts2, id2, flag2 = struct.unpack_from("<IHB", body, 7)
        self.assertEqual((id1, flag1), (event_id, 2))  # True  -> 2
        self.assertEqual((id2, flag2), (event_id, 1))  # False -> 1

    def test_nonzero_set_persisted_at_init_and_tags_batches(self):
        ces = CustomEventSet(version=456, event_count=2, gzip_data=b"\x1f\x8b\x00rest")
        with tempfile.TemporaryDirectory() as d:
            disp = _make_dispatcher(d, ces)
            try:
                self.assertEqual(disp._in_batch.custom_events_version, 456)
                self.assertEqual(disp._sending_batch.custom_events_version, 456)
                path = os.path.join(d, "456.map.gz")
                self.assertTrue(os.path.exists(path))
                # Round-trips back to the same set.
                from keewano_sdk.internal.storage import KStorage

                loaded = KStorage.load_custom_event_set_from_file(path)
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded.version, 456)
                self.assertEqual(loaded.event_count, 2)
                self.assertEqual(loaded.gzip_data, b"\x1f\x8b\x00rest")
            finally:
                disp.stop()


if __name__ == "__main__":
    unittest.main()
