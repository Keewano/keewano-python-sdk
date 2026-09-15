"""Public value type for virtual-economy items."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    """A game item (or app item) with a unique identifier and a quantity.

    :param name: Unique name of the item.
    :param count: Quantity of the item (defaults to 1). Sent as an unsigned 32-bit integer.
    """

    name: str
    count: int = 1
