"""Unit tests for the shared atomic_write helper."""

import os
import tempfile
import unittest
from unittest import mock

from keewano_sdk.internal.files import atomic_write


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()

    def _path(self, name="f.bin"):
        return os.path.join(self._dir, name)

    def test_writes_content(self):
        p = self._path()
        atomic_write(p, b"hello")
        with open(p, "rb") as f:
            self.assertEqual(f.read(), b"hello")

    def test_overwrites_existing_atomically(self):
        p = self._path()
        atomic_write(p, b"first")
        atomic_write(p, b"second")
        with open(p, "rb") as f:
            self.assertEqual(f.read(), b"second")

    def test_leaves_no_tmp_on_success(self):
        p = self._path()
        atomic_write(p, b"data")
        self.assertEqual([f for f in os.listdir(self._dir) if f.endswith(".tmp")], [])

    def test_raises_and_cleans_tmp_on_write_failure(self):
        p = self._path()
        # Simulate the write blowing up after the temp file is opened.
        with mock.patch("os.fsync", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                atomic_write(p, b"data")
        # The target was never created, and no partial .tmp is left behind.
        self.assertFalse(os.path.exists(p))
        self.assertEqual([f for f in os.listdir(self._dir) if f.endswith(".tmp")], [])

    def test_target_unchanged_when_write_fails(self):
        p = self._path()
        atomic_write(p, b"original")
        with mock.patch("os.fsync", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                atomic_write(p, b"replacement")
        with open(p, "rb") as f:
            self.assertEqual(f.read(), b"original")  # left untouched


if __name__ == "__main__":
    unittest.main()
