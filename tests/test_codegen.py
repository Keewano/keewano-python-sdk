"""Tests for the public KeewanoCodegen reporting bridge (id-range guard + typed dispatch)."""

import struct
import tempfile
import unittest

from keewano_sdk import KeewanoCodegen
from keewano_sdk import sdk as sdkmod
from keewano_sdk.internal import guid
from keewano_sdk.internal.consent import UserConsentState
from keewano_sdk.internal.dispatcher import KEventDispatcher
from keewano_sdk.internal.events import KEvents


def _hdr(body, off):
    ts, eid = struct.unpack_from("<IH", body, off)
    return ts, eid, off + 6


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


class KeewanoCodegenTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._d = KEventDispatcher(
            working_directory=self._dir,
            endpoint="http://127.0.0.1:1/unused",
            app_secret="tok",
            initial_consent=UserConsentState.NOT_REQUIRED,
            install_id=guid.new_guid(),
            initial_user_id=guid.EMPTY,
            data_session_id=guid.new_guid(),
            sdk_version="1.0.0",
        )
        self._prev = sdkmod._dispatcher
        sdkmod._dispatcher = self._d  # the bridge routes through sdk._with_dispatcher

    def tearDown(self):
        sdkmod._dispatcher = self._prev
        self._d.stop()

    def _body(self):
        return self._d._in_batch.data.to_bytes()

    def test_ids_outside_custom_range_are_dropped(self):
        with self.assertLogs("keewano_sdk", level="ERROR"):
            KeewanoCodegen.report_custom_event_str(KEvents.CUSTOM_EVENT_ID_MIN - 1, "x")  # below floor
        with self.assertLogs("keewano_sdk", level="ERROR"):
            KeewanoCodegen.report_custom_event_str(0x1_0000, "x")  # above 65535
        self.assertEqual(self._body(), b"")  # nothing written

    def test_each_typed_entry_point_emits_expected_payload(self):
        KeewanoCodegen.report_custom_event_str(2500, "hi")
        KeewanoCodegen.report_custom_event_uint(2501, 7)
        KeewanoCodegen.report_custom_event_int(2502, -5)
        KeewanoCodegen.report_custom_event_bool(2503, True)
        KeewanoCodegen.report_custom_event_float(2504, 1.5)
        KeewanoCodegen.report_custom_event(2505)
        body = self._body()

        off = 0
        _ts, eid, off = _hdr(body, off)
        s, off = _string(body, off)
        self.assertEqual((eid, s), (2500, "hi"))
        _ts, eid, off = _hdr(body, off)
        (v,) = struct.unpack_from("<I", body, off)
        off += 4
        self.assertEqual((eid, v), (2501, 7))
        _ts, eid, off = _hdr(body, off)
        (v,) = struct.unpack_from("<i", body, off)
        off += 4
        self.assertEqual((eid, v), (2502, -5))
        _ts, eid, off = _hdr(body, off)
        flag = body[off]
        off += 1
        self.assertEqual((eid, flag), (2503, 2))  # True -> 2
        _ts, eid, off = _hdr(body, off)
        (f,) = struct.unpack_from("<f", body, off)
        off += 4
        self.assertEqual(eid, 2504)
        self.assertAlmostEqual(f, 1.5, places=5)
        _ts, eid, off = _hdr(body, off)
        self.assertEqual((eid, off), (2505, len(body)))  # no payload

    def test_uint_negative_is_dropped(self):
        with self.assertLogs("keewano_sdk", level="ERROR"):
            KeewanoCodegen.report_custom_event_uint(2500, -1)
        self.assertEqual(self._body(), b"")

    def test_int_out_of_range_and_non_int_are_dropped(self):
        from keewano_sdk import sdk as sdkmod

        with self.assertLogs("keewano_sdk", level="ERROR"):
            KeewanoCodegen.report_custom_event_int(2500, sdkmod.MAX_INT32 + 1)  # would wrap on the wire
        with self.assertLogs("keewano_sdk", level="ERROR"):
            KeewanoCodegen.report_custom_event_int(2500, sdkmod.MIN_INT32 - 1)
        with self.assertLogs("keewano_sdk", level="ERROR"):
            KeewanoCodegen.report_custom_event_int(2500, 1.5)  # would raise at the wire writer
        self.assertEqual(self._body(), b"")  # nothing written for any of them

    def test_float_nan_is_dropped_but_negative_allowed(self):
        with self.assertLogs("keewano_sdk", level="ERROR"):
            KeewanoCodegen.report_custom_event_float(2500, float("nan"))
        self.assertEqual(self._body(), b"")
        KeewanoCodegen.report_custom_event_float(2500, -2.5)  # finite negative is fine
        _ts, eid, off = _hdr(self._body(), 0)
        self.assertEqual(eid, 2500)

    def test_float_non_number_and_bool_are_dropped_int_allowed(self):
        for bad in ("1.5", None, True):  # str/None would crash math.isfinite; bool must not pass as 1.0
            with self.assertLogs("keewano_sdk", level="ERROR"):
                KeewanoCodegen.report_custom_event_float(2500, bad)
        self.assertEqual(self._body(), b"")
        KeewanoCodegen.report_custom_event_float(2500, 3)  # int serializes cleanly as a float
        _ts, eid, off = _hdr(self._body(), 0)
        (f,) = struct.unpack_from("<f", self._body(), off)
        self.assertEqual((eid, f), (2500, 3.0))

    def test_float_out_of_float32_range_is_dropped(self):
        # A finite Python double beyond 32-bit float range would overflow struct.pack('<f') and crash
        # the report call without this guard.
        # 10**400 is beyond double range, so math.isfinite() would raise on it — the float32 check
        # must run first so this is dropped, not crashed.
        for bad in (1e300, -1e300, 3.5e38, 10**400):
            with self.assertLogs("keewano_sdk", level="ERROR"):
                KeewanoCodegen.report_custom_event_float(2500, bad)
        self.assertEqual(self._body(), b"")  # nothing written

    def test_str_blank_is_dropped(self):
        with self.assertLogs("keewano_sdk", level="ERROR"):
            KeewanoCodegen.report_custom_event_str(2500, "   ")
        self.assertEqual(self._body(), b"")

    def test_ushort_pair_emits_two_uint16(self):
        KeewanoCodegen.report_custom_event_ushort_pair(2500, 1920, 1080)
        body = self._body()
        _ts, eid, off = _hdr(body, 0)
        x, y = struct.unpack_from("<HH", body, off)
        self.assertEqual((eid, x, y), (2500, 1920, 1080))

    def test_ushort_pair_out_of_range_is_dropped(self):
        with self.assertLogs("keewano_sdk", level="ERROR"):
            KeewanoCodegen.report_custom_event_ushort_pair(2500, 70000, 5)  # x > 65535
        with self.assertLogs("keewano_sdk", level="ERROR"):
            KeewanoCodegen.report_custom_event_ushort_pair(2500, 5, -1)  # y < 0
        self.assertEqual(self._body(), b"")


if __name__ == "__main__":
    unittest.main()
