"""Unit tests for the public value types: KeewanoConfig, Item, AdType."""

import unittest

from keewano_sdk import AdType, Item, KeewanoConfig
from keewano_sdk.config import DEFAULT_ENDPOINT


class KeewanoConfigTest(unittest.TestCase):
    def test_defaults(self):
        c = KeewanoConfig(api_key="k")
        self.assertEqual(c.api_key, "k")
        self.assertFalse(c.require_user_consent)
        self.assertEqual(c.app_version, "")
        self.assertIsNone(c.data_dir)
        self.assertFalse(c.disable_exception_tracking)
        self.assertEqual(c.endpoint, DEFAULT_ENDPOINT)
        self.assertIsNone(c.proxy_auth_bearer)
        self.assertIsNone(c.custom_event_set)

    def test_default_endpoint_is_production_https(self):
        self.assertTrue(DEFAULT_ENDPOINT.startswith("https://"))

    def test_overrides(self):
        c = KeewanoConfig(
            api_key="k",
            require_user_consent=True,
            endpoint="http://x/y",
            proxy_auth_bearer="tok",
            app_version="2.0",
            data_dir="/tmp/x",
        )
        self.assertTrue(c.require_user_consent)
        self.assertEqual(c.endpoint, "http://x/y")
        self.assertEqual(c.proxy_auth_bearer, "tok")


class ItemTest(unittest.TestCase):
    def test_default_count_is_one(self):
        self.assertEqual(Item("gold").count, 1)
        self.assertEqual(Item("gold").name, "gold")

    def test_is_frozen(self):
        item = Item("gold", 5)
        with self.assertRaises(Exception):  # FrozenInstanceError (a subclass of Exception)
            item.count = 10  # type: ignore[misc]

    def test_equality(self):
        self.assertEqual(Item("gold", 3), Item("gold", 3))
        self.assertNotEqual(Item("gold", 3), Item("gold", 4))


class AdTypeTest(unittest.TestCase):
    def test_wire_values_are_pinned(self):
        # Must match the other SDKs' AdType wire values.
        self.assertEqual(int(AdType.REWARDED), 1)
        self.assertEqual(int(AdType.INTERSTITIAL), 2)
        self.assertEqual(int(AdType.BANNER), 3)
        self.assertEqual(int(AdType.PLAYABLE), 4)
        self.assertEqual(int(AdType.OFFERWALL), 5)


if __name__ == "__main__":
    unittest.main()
