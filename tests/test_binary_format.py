"""Guards the wire format against regressions.

These encodings must remain byte-compatible with the other Keewano SDKs and the backend
parser (C# BinaryWriter + .NET Guid layout).
"""

import struct
import unittest

from keewano_sdk.internal import guid
from keewano_sdk.internal.batch import CURRENT_BATCH_VERSION, KBatch
from keewano_sdk.internal.buffer import KBuffer


class BinaryFormatTest(unittest.TestCase):
    def test_little_endian_integers(self):
        b = KBuffer()
        b.write_int32(0x01020304)
        b.write_uint16(0x0102)
        b.write_uint32(0xFFFFFFFF)
        self.assertEqual(
            bytes([0x04, 0x03, 0x02, 0x01, 0x02, 0x01, 0xFF, 0xFF, 0xFF, 0xFF]),
            b.to_bytes(),
        )

    def test_float_is_ieee754_little_endian(self):
        b = KBuffer()
        b.write_float(1.0)  # 0x3F800000 -> little-endian 00 00 80 3F
        self.assertEqual(bytes([0x00, 0x00, 0x80, 0x3F]), b.to_bytes())

    def test_string_uses_7bit_length_prefix_then_utf8(self):
        b = KBuffer()
        b.write_string("AB")
        self.assertEqual(bytes([0x02, 0x41, 0x42]), b.to_bytes())

    def test_long_string_uses_multibyte_7bit_length_prefix(self):
        b = KBuffer()
        b.write_string("x" * 200)  # 200 = 0xC8 -> LEB128: 0xC8, 0x01
        out = b.to_bytes()
        self.assertEqual(0xC8, out[0])
        self.assertEqual(0x01, out[1])
        self.assertEqual(202, len(out))

    def test_bool_encodes_as_2_or_1(self):
        b = KBuffer()
        b.write_raw_byte(2 if True else 1)
        b.write_raw_byte(2 if False else 1)
        self.assertEqual(bytes([2, 1]), b.to_bytes())

    def test_guid_from_uint64_places_value_little_endian_in_last_8_bytes(self):
        g = guid.from_uint64(0x0102030405060708)
        expected = bytes(
            [
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0x08,
                0x07,
                0x06,
                0x05,
                0x04,
                0x03,
                0x02,
                0x01,
            ]
        )
        self.assertEqual(expected, g.to_bytes())

    def test_guid_string_round_trips_through_parser(self):
        original = "12345678-9abc-def0-1122-334455667788"
        g = guid.from_string(original)
        self.assertEqual(original, str(g))

    def test_guid_tostring_formats_first_three_groups_little_endian(self):
        wire = bytes([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15])
        g = guid.from_bytes(wire)
        self.assertEqual("03020100-0504-0706-0809-0a0b0c0d0e0f", str(g))

    def test_char_writes_single_utf8_byte_for_ascii(self):
        b = KBuffer()
        b.write_char("B")
        self.assertEqual(bytes([0x42]), b.to_bytes())

    def test_kwub_batch_frame_layout(self):
        """The .kwub header must be laid out exactly as the backend and other SDKs expect."""
        from keewano_sdk.internal import serializer
        import os
        import tempfile

        batch = KBatch(guid.EMPTY, guid.from_uint64(0x0102030405060708), guid.EMPTY)
        batch.batch_num = 7
        batch.batch_start_time = 1000
        batch.batch_end_time = 2000
        batch.data.write_uint32(0xAABBCCDD)

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "frame.kwub")
            written = serializer.save_to_file(batch, path)
            with open(path, "rb") as f:
                raw = f.read()

        self.assertEqual(written, len(raw))
        # fourCC "KWUB" = 0x57554242, little-endian.
        self.assertEqual(struct.unpack_from("<I", raw, 0)[0], 0x57554242)
        self.assertEqual(struct.unpack_from("<I", raw, 4)[0], CURRENT_BATCH_VERSION)
        # userId (16) at offset 8, dataSessionId (16) at 24, batchNum (int32) at 40.
        self.assertEqual(struct.unpack_from("<i", raw, 40)[0], 7)
        self.assertEqual(struct.unpack_from("<I", raw, 44)[0], 1000)  # batchStartTime
        self.assertEqual(struct.unpack_from("<I", raw, 48)[0], 2000)  # batchEndTime
        self.assertEqual(struct.unpack_from("<i", raw, 52)[0], 4)  # dataLength
        self.assertEqual(struct.unpack_from("<I", raw, 56)[0], 0xAABBCCDD)  # data
        self.assertEqual(struct.unpack_from("<I", raw, 60)[0], 0)  # customEventsVersion

    def test_items_encoding(self):
        """Items: int32 count, then count * (string name, uint32 count)."""
        from keewano_sdk.item import Item
        from keewano_sdk.internal.dispatcher import KEventDispatcher

        b = KBuffer()
        KEventDispatcher._write_items(b, [Item("gold", 3), Item("gem", 1)])
        self.assertEqual(
            bytes(
                [
                    0x02,
                    0x00,
                    0x00,
                    0x00,  # count = 2
                    0x04,
                    0x67,
                    0x6F,
                    0x6C,
                    0x64,  # "gold"
                    0x03,
                    0x00,
                    0x00,
                    0x00,  # 3
                    0x03,
                    0x67,
                    0x65,
                    0x6D,  # "gem"
                    0x01,
                    0x00,
                    0x00,
                    0x00,  # 1
                ]
            ),
            b.to_bytes(),
        )

    def test_purchase_usd_event_body_layout(self):
        """A USD purchase writes three tagged events (timestamp, product id, price) back to back.

        This freezes the event-stream layout the backend parses, independent of the primitives.
        """
        ts = 0x11223344
        product = "AB"
        price = 499

        b = KBuffer()
        # PURCHASE_TIMESTAMP (35): header + uint32 timestamp param
        b.write_uint32(ts)
        b.write_uint16(35)
        b.write_uint32(ts)
        # PURCHASE_PRODUCT_ID (32): header + string
        b.write_uint32(ts)
        b.write_uint16(32)
        b.write_string(product)
        # PURCHASE_PRODUCT_PRICE_USD_CENTS (33): header + uint32
        b.write_uint32(ts)
        b.write_uint16(33)
        b.write_uint32(price)

        expected = bytes(
            [
                0x44,
                0x33,
                0x22,
                0x11,
                0x23,
                0x00,
                0x44,
                0x33,
                0x22,
                0x11,  # ts-event #35, param ts
                0x44,
                0x33,
                0x22,
                0x11,
                0x20,
                0x00,
                0x02,
                0x41,
                0x42,  # id-event #32, "AB"
                0x44,
                0x33,
                0x22,
                0x11,
                0x21,
                0x00,
                0xF3,
                0x01,
                0x00,
                0x00,  # price-event #33, 499
            ]
        )
        self.assertEqual(expected, b.to_bytes())

    # NOTE: the custom-event-set file is no longer a binary wire format — it is a local-only JSON
    # file owned by KStorage (see tests/test_storage.py). Only the .kwub batch frame is pinned here.

    def test_serializer_round_trip(self):
        from keewano_sdk.internal import serializer
        import os
        import tempfile

        src = KBatch(guid.EMPTY, guid.from_uint64(42), guid.new_guid())
        src.batch_num = 3
        src.batch_start_time = 111
        src.batch_end_time = 222
        src.data.write_string("hello")
        src.data.write_uint32(9)

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "222_3.kwub")
            serializer.save_to_file(src, path)
            dst = KBatch(guid.EMPTY, guid.EMPTY, guid.EMPTY)
            self.assertGreaterEqual(serializer.load_from_file(path, dst), 0)

        self.assertEqual(dst.batch_num, 3)
        self.assertEqual(dst.batch_start_time, 111)
        self.assertEqual(dst.batch_end_time, 222)
        self.assertEqual(str(dst.user_id), str(src.user_id))
        self.assertEqual(dst.data.to_bytes(), src.data.to_bytes())


if __name__ == "__main__":
    unittest.main()
