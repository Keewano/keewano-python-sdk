"""Event encoders: the byte layout of every event the SDK emits, as pure functions over a buffer.

Shared by the client dispatcher (one install, one collecting batch) and the server dispatcher (one
collecting batch per end user), so both write byte-identical events — the wire format is a cross-SDK
contract (see ``docs-internal/data-format.md``) and must not be implemented twice.

Every encoder has the shape ``encoder(buf, ts, *payload)``: ``buf`` is the batch's :class:`KBuffer`,
``ts`` the event timestamp (Unix seconds). Encoders do not validate; the public facades do that at the
boundary, before any byte is written (a raise in here would leave a half-written event in the batch).
"""

from __future__ import annotations

import logging
from typing import Sequence

from ..item import Item
from .buffer import KBuffer
from .events import KEvents

_log = logging.getLogger("keewano_sdk")

#: Hard cap on items in one event, so no single event can exceed the slicing threshold.
MAX_ITEMS_PER_EVENT = 512

#: Hard cap on any string payload, for the same reason (one event larger than the cutting threshold
#: cannot be split — cut points only fall between events).
MAX_EVENT_STRING_CHARS = 8 * 1024


def capped(s: str) -> str:
    """Last-mile wire-safety cap on string payloads. One event larger than the cutting threshold
    cannot be split (cut points fall only between events). Python strings index by code point,
    so slicing never splits a surrogate the way a UTF-16 substring would."""
    if len(s) <= MAX_EVENT_STRING_CHARS:
        return s
    _log.warning("Event string of %d chars truncated to %d.", len(s), MAX_EVENT_STRING_CHARS)
    return s[:MAX_EVENT_STRING_CHARS]


def header(buf: KBuffer, ts: int, event_id: int) -> None:
    """Every event starts with ``uint32 timestamp, uint16 eventId``."""
    buf.write_uint32(ts)
    buf.write_uint16(event_id)


def write_items(buf: KBuffer, items: Sequence[Item]) -> None:
    """Writes an item list, capped at ``MAX_ITEMS_PER_EVENT`` so no single event exceeds the
    cutting threshold (which the slicing logic cannot split)."""
    count = min(len(items), MAX_ITEMS_PER_EVENT)
    if count < len(items):
        _log.error("Item list truncated from %d to %d entries.", len(items), count)
    buf.write_int32(count)
    for i in range(count):
        buf.write_string(items[i].name)
        buf.write_uint32(items[i].count)


# --- Generic payload shapes -----------------------------------------------------------------------


def event(buf: KBuffer, ts: int, event_id: int) -> None:
    header(buf, ts, event_id)


def event_str(buf: KBuffer, ts: int, event_id: int, s: str) -> None:
    header(buf, ts, event_id)
    buf.write_string(capped(s))


def event_uint(buf: KBuffer, ts: int, event_id: int, value: int) -> None:
    header(buf, ts, event_id)
    buf.write_uint32(value)


def event_int(buf: KBuffer, ts: int, event_id: int, value: int) -> None:
    header(buf, ts, event_id)
    buf.write_int32(value)


def event_float(buf: KBuffer, ts: int, event_id: int, value: float) -> None:
    header(buf, ts, event_id)
    buf.write_float(value)


def event_ushort_pair(buf: KBuffer, ts: int, event_id: int, x: int, y: int) -> None:
    header(buf, ts, event_id)
    buf.write_uint16(x)
    buf.write_uint16(y)


def event_bool(buf: KBuffer, ts: int, event_id: int, flag: bool) -> None:
    header(buf, ts, event_id)
    buf.write_raw_byte(2 if flag else 1)  # bool wire encoding: 2=true, 1=false


def event_str_items(buf: KBuffer, ts: int, event_id: int, s: str, items: Sequence[Item]) -> None:
    """A string followed by an item list (items reset and the three "items granted" events)."""
    header(buf, ts, event_id)
    buf.write_string(capped(s))
    write_items(buf, items)


# --- Composite events -----------------------------------------------------------------------------


def ab_test_assignment(buf: KBuffer, ts: int, test_name: str, group: str) -> None:
    header(buf, ts, KEvents.AB_TEST_ASSIGNMENT)
    buf.write_string(capped(test_name))
    # A single character written as one byte (its code point), which the backend reads as the group
    # id. The caller guarantees len == 1 and code point <= 255.
    buf.write_raw_byte(ord(group))


def purchase_usd(buf: KBuffer, ts: int, product_name: str, price_usd_cents: int) -> None:
    header(buf, ts, KEvents.PURCHASE_TIMESTAMP)
    buf.write_uint32(ts)
    header(buf, ts, KEvents.PURCHASE_PRODUCT_ID)
    buf.write_string(capped(product_name))
    header(buf, ts, KEvents.PURCHASE_PRODUCT_PRICE_USD_CENTS)
    buf.write_uint32(price_usd_cents)


def purchase_local(buf: KBuffer, ts: int, product_name: str, localized_price: float, currency_code: str) -> None:
    header(buf, ts, KEvents.PURCHASE_TIMESTAMP)
    buf.write_uint32(ts)
    header(buf, ts, KEvents.PURCHASE_PRODUCT_ID)
    buf.write_string(capped(product_name))
    header(buf, ts, KEvents.PURCHASE_LOCAL_CURRENCY_NAME)
    buf.write_string(capped(currency_code))
    header(buf, ts, KEvents.PURCHASE_LOCAL_CURRENCY_AMOUNT)
    buf.write_float(localized_price)


def ad_offered(buf: KBuffer, ts: int, placement: str, ad_type: int) -> None:
    header(buf, ts, KEvents.AD_OFFERED_PLACEMENT)
    buf.write_string(capped(placement))
    header(buf, ts, KEvents.AD_OFFERED_TYPE)
    buf.write_raw_byte(ad_type)


def ad_revenue_usd(buf: KBuffer, ts: int, placement: str, revenue_usd_cents: int) -> None:
    header(buf, ts, KEvents.AD_REVENUE_TIMESTAMP)
    buf.write_uint32(ts)
    header(buf, ts, KEvents.AD_REVENUE_PLACEMENT)
    buf.write_string(capped(placement))
    header(buf, ts, KEvents.AD_REVENUE_USD_CENTS)
    buf.write_uint32(revenue_usd_cents)


def ad_revenue_local(buf: KBuffer, ts: int, placement: str, localized_revenue: float, currency_code: str) -> None:
    header(buf, ts, KEvents.AD_REVENUE_TIMESTAMP)
    buf.write_uint32(ts)
    header(buf, ts, KEvents.AD_REVENUE_PLACEMENT)
    buf.write_string(capped(placement))
    header(buf, ts, KEvents.AD_REVENUE_LOCAL_CURRENCY_NAME)
    buf.write_string(capped(currency_code))
    header(buf, ts, KEvents.AD_REVENUE_LOCAL_CURRENCY_AMOUNT)
    buf.write_float(localized_revenue)


def subscription_revenue_usd(buf: KBuffer, ts: int, package_name: str, revenue_usd_cents: int) -> None:
    header(buf, ts, KEvents.SUBSCRIPTION_REVENUE_TIMESTAMP)
    buf.write_uint32(ts)
    header(buf, ts, KEvents.SUBSCRIPTION_REVENUE_PACKAGE)
    buf.write_string(capped(package_name))
    header(buf, ts, KEvents.SUBSCRIPTION_REVENUE_USD_CENTS)
    buf.write_uint32(revenue_usd_cents)


def subscription_revenue_local(
    buf: KBuffer, ts: int, package_name: str, localized_revenue: float, currency_code: str
) -> None:
    header(buf, ts, KEvents.SUBSCRIPTION_REVENUE_TIMESTAMP)
    buf.write_uint32(ts)
    header(buf, ts, KEvents.SUBSCRIPTION_REVENUE_PACKAGE)
    buf.write_string(capped(package_name))
    header(buf, ts, KEvents.SUBSCRIPTION_LOCAL_CURRENCY_NAME)
    buf.write_string(capped(currency_code))
    header(buf, ts, KEvents.SUBSCRIPTION_LOCAL_CURRENCY_AMOUNT)
    buf.write_float(localized_revenue)


def items_exchange(
    buf: KBuffer, ts: int, exchange_point: str, from_items: Sequence[Item], to_items: Sequence[Item]
) -> None:
    header(buf, ts, KEvents.ITEMS_EXCHANGE)
    buf.write_string(capped(exchange_point))
    write_items(buf, from_items)
    write_items(buf, to_items)


def batch_dropped(buf: KBuffer, ts: int, reason: int) -> None:
    """The BATCH_DROPPED marker that replaces a batch's events when they had to be discarded."""
    header(buf, ts, KEvents.BATCH_DROPPED)
    buf.write_uint32(reason)
