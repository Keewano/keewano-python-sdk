"""Public enum for advertisement types."""

from __future__ import annotations

from enum import IntEnum


class AdType(IntEnum):
    """Type of advertisement. The wire values must match the other Keewano SDKs' ``AdType``."""

    #: Rewarded video ads - user chooses to watch for in-game rewards.
    REWARDED = 1

    #: Full-screen ads at transition points.
    INTERSTITIAL = 2

    #: Small persistent ads displayed on screen.
    BANNER = 3

    #: Interactive mini-game ads.
    PLAYABLE = 4

    #: List of tasks/offers for rewards.
    OFFERWALL = 5
