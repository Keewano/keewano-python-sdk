"""Unit tests for the default per-user data directory resolution."""

import os
import re
import unittest
from unittest import mock

from keewano_sdk.internal import paths


class DefaultDataDirTest(unittest.TestCase):
    """The per-OS base directory, with no unique seed (blank seed => no subdirectory)."""

    def test_windows_uses_localappdata(self):
        with mock.patch.object(paths.sys, "platform", "win32"), mock.patch.dict(
            os.environ, {"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}, clear=False
        ):
            self.assertEqual(paths.default_data_dir(""), os.path.join(r"C:\Users\me\AppData\Local", "Keewano"))

    def test_macos_uses_application_support(self):
        with mock.patch.object(paths.sys, "platform", "darwin"), mock.patch.object(
            paths.os.path, "expanduser", return_value="/Users/me"
        ):
            self.assertEqual(
                paths.default_data_dir(""),
                os.path.join("/Users/me", "Library", "Application Support", "Keewano"),
            )

    def test_linux_honours_xdg_data_home(self):
        with mock.patch.object(paths.sys, "platform", "linux"), mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": "/custom/xdg"}, clear=False
        ):
            self.assertEqual(paths.default_data_dir(""), os.path.join("/custom/xdg", "keewano"))

    def test_linux_falls_back_to_local_share(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_DATA_HOME"}
        with mock.patch.object(paths.sys, "platform", "linux"), mock.patch.dict(
            os.environ, env, clear=True
        ), mock.patch.object(paths.os.path, "expanduser", return_value="/home/me"):
            self.assertEqual(
                paths.default_data_dir(""),
                os.path.join("/home/me", ".local", "share", "keewano"),
            )

    def test_blank_and_whitespace_seed_add_no_subdirectory(self):
        with mock.patch.object(paths.sys, "platform", "linux"), mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": "/custom/xdg"}, clear=False
        ):
            base = os.path.join("/custom/xdg", "keewano")
            self.assertEqual(paths.default_data_dir(""), base)
            self.assertEqual(paths.default_data_dir("   "), base)

    def test_nonblank_seed_appends_a_hashed_subdirectory(self):
        with mock.patch.object(paths.sys, "platform", "linux"), mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": "/custom/xdg"}, clear=False
        ):
            base = os.path.join("/custom/xdg", "keewano")
            full = paths.default_data_dir("some.jwt.key")
            parent, sub = os.path.split(full)
            self.assertEqual(parent, base)
            self.assertEqual(sub, paths._dir_name_from_seed("some.jwt.key"))


class DirNameFromSeedTest(unittest.TestCase):
    def test_is_deterministic(self):
        self.assertEqual(paths._dir_name_from_seed("some.jwt.key"), paths._dir_name_from_seed("some.jwt.key"))

    def test_distinct_seeds_give_distinct_names(self):
        self.assertNotEqual(paths._dir_name_from_seed("key-a"), paths._dir_name_from_seed("key-b"))

    def test_leading_trailing_whitespace_is_ignored(self):
        self.assertEqual(paths._dir_name_from_seed("  some.jwt.key \n"), paths._dir_name_from_seed("some.jwt.key"))

    def test_name_is_filesystem_and_shell_safe(self):
        # URL-safe base64 without padding: only A-Z a-z 0-9 - _  (no '=', no shell/FS-hostile chars).
        for seed in ("k", "another.longer.jwt.value", "unicode-\u00e9\u00e8", "12345"):
            name = paths._dir_name_from_seed(seed)
            self.assertRegex(name, r"^[A-Za-z0-9_-]+$", name)
            self.assertEqual(len(name), 22)  # 16-byte digest -> 22 base64 chars, padding stripped

    def test_name_does_not_contain_the_seed(self):
        # A one-way hash: no substring of the (high-entropy) seed leaks into the directory name.
        seed = "supersecret.api.key.value"
        name = paths._dir_name_from_seed(seed)
        for chunk in re.split(r"\W+", seed):
            self.assertNotIn(chunk, name)


if __name__ == "__main__":
    unittest.main()
