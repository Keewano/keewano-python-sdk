"""User consent state for GDPR/CCPA compliance.

Ordinals are persisted to disk, so their order must not change.
"""

from __future__ import annotations

from enum import IntEnum


class UserConsentState(IntEnum):
    NOT_REQUIRED = 0
    PENDING = 1
    GRANTED = 2
    DENIED = 3

    @classmethod
    def from_ordinal(cls, ordinal: int) -> "UserConsentState":
        try:
            return cls(ordinal)
        except ValueError:
            return cls.NOT_REQUIRED
