"""Unit tests for the UserConsentState enum (ordinals are persisted, so they must be stable)."""

import unittest

from keewano_sdk.internal.consent import UserConsentState


class UserConsentStateTest(unittest.TestCase):
    def test_ordinal_values_are_pinned(self):
        # These ints are written to disk and shared across SDKs; they must never change.
        self.assertEqual(int(UserConsentState.NOT_REQUIRED), 0)
        self.assertEqual(int(UserConsentState.PENDING), 1)
        self.assertEqual(int(UserConsentState.GRANTED), 2)
        self.assertEqual(int(UserConsentState.DENIED), 3)

    def test_from_ordinal_valid(self):
        for state in UserConsentState:
            self.assertIs(UserConsentState.from_ordinal(int(state)), state)

    def test_from_ordinal_invalid_falls_back_to_not_required(self):
        self.assertIs(UserConsentState.from_ordinal(99), UserConsentState.NOT_REQUIRED)
        self.assertIs(UserConsentState.from_ordinal(-1), UserConsentState.NOT_REQUIRED)


if __name__ == "__main__":
    unittest.main()
