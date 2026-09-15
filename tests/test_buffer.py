"""Unit tests for KBuffer primitive encoders (beyond the byte-freeze cases in test_binary_format)."""

import struct
import unittest

from keewano_sdk.internal.buffer import KBuffer


class KBufferTest(unittest.TestCase):
    def test_length_and_raw_buffer_track_writes(self):
        b = KBuffer()
        self.assertEqual(b.length, 0)
        self.assertEqual(len(b), 0)
        b.write_raw_byte(0x41)
        self.assertEqual(b.length, 1)
        self.assertEqual(len(b), 1)
        self.assertIs(type(b.raw_buffer()), bytearray)
        self.assertEqual(bytes(b.raw_buffer()[: b.length]), b"A")

    def test_set_length_shrinks_only(self):
        b = KBuffer()
        b.write_bytes(b"abcdef")
        b.set_length(3)
        self.assertEqual(b.to_bytes(), b"abc")
        with self.assertRaises(ValueError):
            b.set_length(10)  # growing is unsupported
        with self.assertRaises(ValueError):
            b.set_length(-1)

    def test_write_bytes_offset_and_count(self):
        b = KBuffer()
        b.write_bytes(b"HELLO", 1, 3)  # "ELL"
        self.assertEqual(b.to_bytes(), b"ELL")

    def test_signed_and_unsigned_int32_boundaries(self):
        b = KBuffer()
        b.write_int32(-1)
        b.write_uint32(0xFFFFFFFF)
        b.write_int32(-2147483648)
        self.assertEqual(
            b.to_bytes(),
            struct.pack("<i", -1) + struct.pack("<I", 0xFFFFFFFF) + struct.pack("<i", -2147483648),
        )

    def test_uint16_wraps_to_two_bytes(self):
        b = KBuffer()
        b.write_uint16(0xFFFF)
        self.assertEqual(b.to_bytes(), b"\xff\xff")

    def test_7bit_encoding_boundaries(self):
        for value, expected in [
            (0, b"\x00"),
            (1, b"\x01"),
            (127, b"\x7f"),
            (128, b"\x80\x01"),
            (16383, b"\xff\x7f"),
            (16384, b"\x80\x80\x01"),
        ]:
            b = KBuffer()
            b.write_string("x" * value)
            self.assertEqual(b.to_bytes()[: len(expected)], expected, f"len prefix for {value}")

    def test_string_and_char_utf8(self):
        b = KBuffer()
        b.write_string("héllo")  # é is 2 UTF-8 bytes -> byte length 6
        out = b.to_bytes()
        self.assertEqual(out[0], 6)
        self.assertEqual(out[1:], "héllo".encode("utf-8"))

        b2 = KBuffer()
        b2.write_char("€")  # 3 UTF-8 bytes, no length prefix
        self.assertEqual(b2.to_bytes(), "€".encode("utf-8"))

    def test_char_rejects_multichar(self):
        b = KBuffer()
        with self.assertRaises(ValueError):
            b.write_char("ab")

    def test_float_roundtrips_via_struct(self):
        b = KBuffer()
        b.write_float(3.5)
        self.assertEqual(struct.unpack("<f", b.to_bytes())[0], 3.5)


if __name__ == "__main__":
    unittest.main()
