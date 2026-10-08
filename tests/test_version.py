"""The SDK version has one source of truth, ``keewano_sdk.__version__``: the build reads it from there and
both SDKs send it as ``K-SDK: Python/<version>``. These tests keep the copies from drifting apart."""

import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import keewano_sdk  # noqa: E402
from keewano_sdk import KeewanoConfig, KeewanoServerConfig, server_sdk  # noqa: E402
from keewano_sdk import sdk as sdkmod  # noqa: E402

try:  # Python 3.8+
    from importlib import metadata
except ImportError:  # pragma: no cover
    metadata = None

_PYPROJECT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pyproject.toml")


class VersionSourceTest(unittest.TestCase):
    def test_version_is_major_minor_patch(self):
        self.assertRegex(keewano_sdk.__version__, r"^\d+\.\d+\.\d+$")

    def test_pyproject_reads_the_version_from_the_package(self):
        # A text check rather than tomllib, which needs Python 3.11 (the SDK supports 3.8).
        with open(_PYPROJECT, encoding="utf-8") as f:
            text = f.read()
        project = re.search(r"^\[project\]\n(.*?)(?=^\[)", text, re.S | re.M).group(1)
        self.assertIsNone(re.search(r"^version\s*=", project, re.M), "pyproject.toml must not pin a version of its own")
        self.assertRegex(project, r'(?m)^dynamic\s*=\s*\[[^\]]*"version"', "version must be declared dynamic")
        self.assertRegex(
            text, r'\[tool\.setuptools\.dynamic\]\s*\nversion\s*=\s*\{\s*attr\s*=\s*"keewano_sdk\.__version__"\s*\}'
        )

    def test_installed_metadata_matches_the_package(self):
        if metadata is None:
            self.skipTest("importlib.metadata unavailable")
        try:
            installed = metadata.version("keewano-sdk")
        except metadata.PackageNotFoundError:
            self.skipTest("keewano-sdk is not installed (CI installs it with `pip install -e .`)")
        # An editable install records the version at install time: reinstall after a bump.
        self.assertEqual(installed, keewano_sdk.__version__)


class VersionOnTheWireTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()

    def tearDown(self):
        keewano_sdk.shutdown()
        server_sdk.shutdown(0)
        sdkmod._dispatcher = sdkmod._storage = sdkmod._user_identifiers = None

    def test_client_sends_the_package_version(self):
        keewano_sdk.initialize(
            KeewanoConfig(
                api_key="k", data_dir=self._dir, disable_exception_tracking=True, endpoint="http://127.0.0.1:1/b"
            )
        )
        self.assertEqual(sdkmod._dispatcher._network._sdk_version, f"Python/{keewano_sdk.__version__}")

    def test_server_sends_the_package_version(self):
        server_sdk.initialize(KeewanoServerConfig(api_key="k", endpoint="http://127.0.0.1:1/b"))
        self.assertEqual(server_sdk._dispatcher._network._sdk_version, f"Python/{keewano_sdk.__version__}")


if __name__ == "__main__":
    unittest.main()
