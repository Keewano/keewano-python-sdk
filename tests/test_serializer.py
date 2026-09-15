"""Unit tests for serializer error/edge paths (round-trips are covered in test_binary_format)."""

import os
import struct
import tempfile
import unittest

from keewano_sdk.internal import guid, serializer
from keewano_sdk.internal.batch import KBatch


def _new_batch():
    return KBatch(guid.EMPTY, guid.EMPTY, guid.EMPTY)


class SerializerErrorPathsTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()

    def test_load_missing_file_returns_minus_one(self):
        self.assertEqual(serializer.load_from_file(os.path.join(self._dir, "nope.kwub"), _new_batch()), -1)

    def test_load_bad_fourcc_returns_minus_one(self):
        path = os.path.join(self._dir, "bad.kwub")
        with open(path, "wb") as f:
            f.write(struct.pack("<I", 0xDEADBEEF) + b"\x00" * 60)
        self.assertEqual(serializer.load_from_file(path, _new_batch()), -1)

    def test_load_truncated_returns_minus_one(self):
        src = _new_batch()
        src.data.write_string("hello")
        path = os.path.join(self._dir, "trunc.kwub")
        serializer.save_to_file(src, path)
        with open(path, "rb") as f:
            full = f.read()
        with open(path, "wb") as f:
            f.write(full[:10])  # chop the file
        self.assertEqual(serializer.load_from_file(path, _new_batch()), -1)

    def test_load_rejects_legacy_version_1(self):
        # Legacy v1 acceptance was removed (matches the Android SDK): only the current version loads.
        buf = struct.pack("<i", 0x57554242)  # fourCC
        buf += struct.pack("<I", 1)  # version = 1 (legacy, no longer accepted)
        buf += b"\x00" * 16  # userId
        buf += b"\x00" * 16  # dataSessionId
        buf += struct.pack("<i", 5)  # batchNum
        buf += struct.pack("<I", 100)  # batchStartTime
        buf += struct.pack("<I", 200)  # batchEndTime
        buf += struct.pack("<i", 0)  # dataLength = 0
        buf += struct.pack("<I", 0)  # customEventsVersion
        path = os.path.join(self._dir, "100_5.kwub")
        with open(path, "wb") as f:
            f.write(buf)
        self.assertEqual(serializer.load_from_file(path, _new_batch()), -1)

    # Custom-event-set persistence moved out of the serializer to KStorage (JSON, local-only);
    # its tests live in tests/test_storage.py.


if __name__ == "__main__":
    unittest.main()
