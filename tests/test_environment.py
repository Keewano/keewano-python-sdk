"""Unit tests for best-effort host introspection.

These are environment-dependent, so the contract is: never raise, always return the right type,
and map known values correctly. Exact values (RAM, language) are not asserted.
"""

import unittest
from unittest import mock

from keewano_sdk.internal import environment


class EnvironmentTest(unittest.TestCase):
    def test_platform_name_maps_darwin(self):
        with mock.patch.object(environment.platform, "system", return_value="Darwin"):
            self.assertEqual(environment.platform_name(), "macOS")

    def test_platform_name_passthrough_and_unknown(self):
        with mock.patch.object(environment.platform, "system", return_value="Linux"):
            self.assertEqual(environment.platform_name(), "Linux")
        with mock.patch.object(environment.platform, "system", return_value=""):
            self.assertEqual(environment.platform_name(), "Unknown")

    def test_os_description_combines_system_and_release(self):
        with mock.patch.object(environment.platform, "system", return_value="Linux"), mock.patch.object(
            environment.platform, "release", return_value="6.8.0"
        ):
            self.assertEqual(environment.os_description(), "Linux 6.8.0")

    def test_device_type_returns_machine_or_unknown(self):
        with mock.patch.object(environment.platform, "machine", return_value="x86_64"):
            self.assertEqual(environment.device_type(), "x86_64")
        with mock.patch.object(environment.platform, "machine", return_value=""):
            self.assertEqual(environment.device_type(), "Unknown")

    def test_system_language_returns_str_and_never_raises(self):
        self.assertIsInstance(environment.system_language(), str)
        # Even if the process locale lookup blows up and there is no usable env var, it returns "".
        with mock.patch.object(environment.locale, "getlocale", side_effect=Exception("boom")), mock.patch.dict(
            environment.os.environ, {v: "" for v in environment._LOCALE_ENV_VARS}, clear=False
        ), mock.patch.object(environment.sys, "platform", "linux"):
            self.assertEqual(environment.system_language(), "")

    def test_system_language_uses_process_locale_when_set(self):
        with mock.patch.object(environment.locale, "getlocale", return_value=("en_US", "UTF-8")):
            self.assertEqual(environment.system_language(), "en")

    def test_system_language_emits_no_deprecation_warning(self):
        # Directly guards the 3.15 concern: locale.getdefaultlocale() would warn now and be gone then.
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            self.assertIsInstance(environment.system_language(), str)

    def test_system_language_falls_back_to_env_vars(self):
        # No process locale configured ("C"): the LC_*/LANG env vars are consulted, in priority order.
        with mock.patch.object(environment.locale, "getlocale", return_value=("C", None)), mock.patch.object(
            environment.sys, "platform", "linux"
        ):
            with mock.patch.dict(
                environment.os.environ, {"LC_ALL": "", "LC_CTYPE": "", "LANG": "fr_FR.UTF-8"}, clear=False
            ):
                self.assertEqual(environment.system_language(), "fr")
            # LC_ALL wins over LANG.
            with mock.patch.dict(environment.os.environ, {"LC_ALL": "de_DE.UTF-8", "LANG": "fr_FR.UTF-8"}, clear=False):
                self.assertEqual(environment.system_language(), "de")
            # A bare "C"/"POSIX" env value is ignored (it is not a real user language).
            with mock.patch.dict(environment.os.environ, {v: "" for v in environment._LOCALE_ENV_VARS}, clear=False):
                with mock.patch.dict(environment.os.environ, {"LANG": "C"}, clear=False):
                    self.assertEqual(environment.system_language(), "")

    def test_system_language_normalises_windows_hyphen_form(self):
        with mock.patch.object(environment.locale, "getlocale", return_value=("C", None)), mock.patch.object(
            environment.sys, "platform", "win32"
        ), mock.patch.object(environment, "_windows_locale_name", return_value="en-US"), mock.patch.dict(
            environment.os.environ, {v: "" for v in environment._LOCALE_ENV_VARS}, clear=False
        ):
            self.assertEqual(environment.system_language(), "en")

    def test_ram_size_mb_is_nonnegative_int(self):
        ram = environment.ram_size_mb()
        self.assertIsInstance(ram, int)
        self.assertGreaterEqual(ram, 0)


if __name__ == "__main__":
    unittest.main()
