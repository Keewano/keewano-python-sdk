"""Unit tests for the .NET-compatible KGuid."""

import unittest

from keewano_sdk.internal import guid
from keewano_sdk.internal.guid import KGuid


class KGuidTest(unittest.TestCase):
    def test_empty_is_all_zero(self):
        self.assertEqual(guid.EMPTY.to_bytes(), b"\x00" * 16)
        self.assertEqual(str(guid.EMPTY), "00000000-0000-0000-0000-000000000000")

    def test_new_guid_is_16_random_bytes(self):
        a, b = guid.new_guid(), guid.new_guid()
        self.assertEqual(len(a.to_bytes()), 16)
        self.assertNotEqual(a, b)  # astronomically unlikely to collide

    def test_new_guid_sets_rfc4122_v4_bits(self):
        for _ in range(50):
            wire = guid.new_guid().to_bytes()
            self.assertEqual(wire[7] & 0xF0, 0x40, "version nibble must be 4")
            self.assertEqual(wire[8] & 0xC0, 0x80, "variant bits must be 10xx")

    def test_from_uint64_boundaries(self):
        self.assertEqual(guid.from_uint64(0).to_bytes(), b"\x00" * 16)
        g = guid.from_uint64(0xFFFFFFFFFFFFFFFF)
        self.assertEqual(g.to_bytes(), b"\x00" * 8 + b"\xff" * 8)

    def test_from_string_roundtrip_and_case_normalization(self):
        s = "12345678-9ABC-DEF0-1122-334455667788"
        g = guid.from_string(s)
        self.assertEqual(str(g), s.lower())  # canonical form is lower-case

    def test_from_string_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            guid.from_string("too-short")
        with self.assertRaises(ValueError):
            guid.from_string("zzzzzzzz-9abc-def0-1122-334455667788")  # non-hex

    def test_from_bytes_requires_16(self):
        with self.assertRaises(ValueError):
            KGuid(b"\x00" * 15)
        with self.assertRaises(ValueError):
            KGuid(b"\x00" * 17)

    def test_equality_and_hashing(self):
        g1 = guid.from_uint64(42)
        g2 = guid.from_bytes(g1.to_bytes())
        self.assertEqual(g1, g2)
        self.assertEqual(hash(g1), hash(g2))
        self.assertNotEqual(g1, guid.from_uint64(43))
        self.assertNotEqual(g1, "not a guid")
        self.assertEqual(len({g1, g2}), 1)  # usable as a set/dict key

    def test_tostring_groups_are_little_endian_for_first_three(self):
        wire = bytes(range(16))  # 00..0f
        self.assertEqual(str(KGuid(wire)), "03020100-0504-0706-0809-0a0b0c0d0e0f")


if __name__ == "__main__":
    unittest.main()
