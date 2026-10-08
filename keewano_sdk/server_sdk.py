"""Server-side entry point: one process reporting Keewano events on behalf of many end users.

Use this instead of the client API (``keewano_sdk.initialize`` / ``keewano_sdk.report_*``) in a backend
service — a game server, a purchase-validation webhook, a Celery task — where each event belongs to
whichever user the request is about. Every reporting function takes that user's id as its first
argument::

    from keewano_sdk import server_sdk as keewano, KeewanoServerConfig, Item

    keewano.initialize(KeewanoServerConfig(api_key="YOUR_KEEWANO_API_KEY"))
    keewano.report_in_app_purchase(user_id, "gems_100", price_usd_cents=499)
    keewano.report_in_app_purchase_items_granted(user_id, "gems_100", [Item("gems", 100)])

Events are aggregated per user and uploaded in the background; see ``docs/server-side.md``. Like the
client API, :func:`initialize` never raises and every reporting call is safe from any thread and a
silent no-op (with a warning) before initialization. Forked worker processes (gunicorn, Celery,
``multiprocessing``) get a clean engine of their own automatically.
"""

from __future__ import annotations

import atexit
import logging
import math
import os
import threading
import uuid
from typing import Optional, Sequence, Union

from . import __version__
from .ad_type import AdType
from .config import KeewanoServerConfig
from .item import Item
from .internal import guid, paths
from .internal.guid import KGuid
from .internal.server_dispatcher import KServerDispatcher
from .sdk import (
    MAX_ERROR_LENGTH,
    _ab_group,
    _items,
    _name,
    _revenue,
    _validate_test_user,
)

_log = logging.getLogger("keewano_sdk")

#: An end user's id, as every server-side call takes it: a positive 64-bit ``int``, a GUID/UUID
#: ``str``, or a :class:`uuid.UUID`.
UserId = Union[int, str, uuid.UUID]

_init_lock = threading.Lock()
_dispatcher: Optional[KServerDispatcher] = None
_shutdown_timeout = 0.0

_MIB = 1024 * 1024


# --------------------------------------------------------------------------------------------
# Initialization & lifecycle
# --------------------------------------------------------------------------------------------


def initialize(config: KeewanoServerConfig) -> None:
    """Initializes the server SDK. Idempotent — subsequent calls are ignored. Never raises: any
    failure is logged and leaves the SDK disabled for this process."""
    global _dispatcher, _shutdown_timeout
    with _init_lock:
        if _dispatcher is not None:
            return
        try:
            if not config.api_key or not config.api_key.strip():
                _log.error("No API key was provided; the Keewano server SDK will not start.")
                return
            persistent = bool(config.persistent_storage)
            work_root = None
            if persistent:
                base = config.data_dir or paths.default_data_dir(config.api_key)
                work_root = os.path.join(base, "server")
            d = KServerDispatcher(
                endpoint=config.endpoint,
                app_secret=config.api_key,
                sdk_version=__version__,
                work_root=work_root,
                flush_interval=_positive(config.flush_interval, 60.0 if persistent else 15.0, "flush_interval"),
                user_idle_timeout=_positive(config.user_idle_timeout, 30 * 60.0, "user_idle_timeout"),
                max_users=int(_positive(config.max_users, 10_000, "max_users", integer=True)),
                max_buffered_bytes=int(
                    _positive(config.max_buffered_bytes, 16 * _MIB, "max_buffered_bytes", integer=True)
                ),
                max_pending_bytes=int(
                    _positive(
                        config.max_pending_bytes, (50 if persistent else 16) * _MIB, "max_pending_bytes", integer=True
                    )
                ),
                proxy_auth_bearer=config.proxy_auth_bearer,
                custom_event_set=config.custom_event_set,
                test_user_name=_validate_test_user(
                    _optional_name(config.test_user_name, "test_user_name"), "KeewanoServerConfig"
                ),
            )
            _shutdown_timeout = _non_negative(config.shutdown_timeout, 2.0 if persistent else 10.0, "shutdown_timeout")
            # Not started here: the first event or flush starts it (see KServerDispatcher.start).
            _dispatcher = d
            atexit.register(shutdown)
        except Exception:
            _log.exception("Server SDK initialization failed; the SDK is disabled for this process.")
            _dispatcher = None


def _positive(value, default, param: str, integer: bool = False):
    """``value`` if it is a usable positive number, else ``default`` (logged when ``value`` was set)."""
    if value is None:
        return default
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
    if ok and integer and value != int(value):
        ok = False
    if not ok:
        _log.error("KeewanoServerConfig.%s must be a positive number, got %r; using %r.", param, value, default)
        return default
    return value


def _non_negative(value, default: float, param: str) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value)
    _log.error("KeewanoServerConfig.%s must be a non-negative number, got %r; using %r.", param, value, default)
    return default


def _optional_name(value: Optional[str], param: str) -> Optional[str]:
    if value is None:
        return None
    return _name(value, param, "KeewanoServerConfig")


def shutdown(timeout: Optional[float] = None) -> None:
    """Seals every user's buffered events, spends up to ``timeout`` seconds (default:
    :attr:`KeewanoServerConfig.shutdown_timeout`) uploading what is queued, then stops. Registered with
    ``atexit`` automatically; call it yourself where the process is torn down without running
    ``atexit`` (e.g. Celery's ``worker_process_shutdown`` signal — see ``docs/server-side.md``)."""
    global _dispatcher
    with _init_lock:
        d = _dispatcher
        if d is None:
            return
        _dispatcher = None
        try:
            d.shutdown(_shutdown_timeout if timeout is None else max(0.0, float(timeout)))
        except Exception:
            _log.exception("Server SDK shutdown failed.")
        atexit.unregister(shutdown)


def flush(user_id: Optional[UserId] = None) -> None:
    """Seals the batch of ``user_id`` (or of every user when omitted) and starts uploading it now,
    instead of waiting for :attr:`KeewanoServerConfig.flush_interval`. Asynchronous. The background
    threads start on the first event or flush, so calling this right after :func:`initialize` is also
    how a process starts uploading batches a previous run left on disk."""
    d = _dispatcher
    if d is None:
        return
    if user_id is None:
        d.flush()
        return
    uid = _user(user_id, "flush")
    if uid is not None:
        d.flush(uid)


def is_initialized() -> bool:
    """True once :func:`initialize` has succeeded (and until :func:`shutdown`)."""
    return _dispatcher is not None


def _after_fork_in_child() -> None:
    # The child is single-threaded here. A lock held by a parent thread at fork time would stay held
    # forever in the child, so every lock is replaced — the SDK's own and the facade's.
    global _init_lock
    _init_lock = threading.Lock()
    d = _dispatcher
    if d is not None:
        d.after_fork_in_child()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


# --------------------------------------------------------------------------------------------
# User identity
# --------------------------------------------------------------------------------------------


def _user(user_id: UserId, caller: str) -> Optional[KGuid]:
    """Parses a user id: a positive 64-bit ``int``, a GUID/UUID ``str``, or a :class:`uuid.UUID`. The
    same mapping as the client's ``set_user_id``, so a user reported from both sides is one user."""
    if isinstance(user_id, bool):
        g = None
    elif isinstance(user_id, int):
        if not 0 < user_id <= 0xFFFFFFFFFFFFFFFF:
            _log.error("%s: user_id %s is not a positive 64-bit integer; event dropped.", caller, user_id)
            return None
        g = guid.from_uint64(user_id)
    elif isinstance(user_id, uuid.UUID):
        g = guid.from_string(str(user_id))
    elif isinstance(user_id, str):
        try:
            g = guid.from_string(user_id.strip())
        except ValueError:
            _log.error("%s: user_id '%s' is not a valid GUID string; event dropped.", caller, user_id)
            return None
    else:
        g = None
    if g is None:
        _log.error("%s: user_id must be an int, a GUID str or a uuid.UUID, got %s.", caller, type(user_id).__name__)
        return None
    if g == guid.EMPTY:
        _log.error("%s: user_id must not be the empty GUID; event dropped.", caller)
        return None
    return g


def _with_user_dispatcher(caller: str, user_id: UserId, fn) -> None:
    """Common path of every report call (the server counterpart of ``sdk._with_dispatcher``): SDK
    initialized → valid user → ``fn(dispatcher, user)`` validates the payload and reports it."""
    d = _dispatcher
    if d is None:
        _log.warning("Server SDK not initialized; event dropped. Call keewano_sdk.server_sdk.initialize() first.")
        return
    uid = _user(user_id, caller)
    if uid is not None:
        fn(d, uid)


# --------------------------------------------------------------------------------------------
# Monetization
# --------------------------------------------------------------------------------------------


def report_in_app_purchase(
    user_id: UserId,
    product_name: str,
    price_usd_cents: Optional[int] = None,
    localized_price: Optional[float] = None,
    currency_code: Optional[str] = None,
) -> None:
    """Reports a validated in-app purchase by ``user_id``. Provide either ``price_usd_cents`` or both
    ``localized_price`` and ``currency_code`` (ISO 4217)."""

    def _do(d: KServerDispatcher, uid: KGuid) -> None:
        args = _revenue(
            product_name,
            "product_name",
            price_usd_cents,
            "price_usd_cents",
            localized_price,
            "localized_price",
            currency_code,
            "report_in_app_purchase",
        )
        if args is None:
            return
        if len(args) == 2:
            d.report_in_app_purchase_usd(uid, *args)
        else:
            d.report_in_app_purchase_local(uid, *args)

    _with_user_dispatcher("report_in_app_purchase", user_id, _do)


def report_in_app_purchase_items_granted(
    user_id: UserId, product_name: str, items: Optional[Sequence[Item]] = None
) -> None:
    """Reports virtual items granted to ``user_id`` from a validated in-app purchase."""

    def _do(d: KServerDispatcher, uid: KGuid) -> None:
        product = _name(product_name, "product_name", "report_in_app_purchase_items_granted")
        granted = _items(items, "items", "report_in_app_purchase_items_granted")
        if product is None or granted is None:
            return
        d.report_in_app_purchase_items_granted(uid, product, granted)

    _with_user_dispatcher("report_in_app_purchase_items_granted", user_id, _do)


def report_ad_offered(user_id: UserId, placement: str, ad_type: AdType) -> None:
    """Reports that an ad opportunity was offered to ``user_id``."""

    def _do(d: KServerDispatcher, uid: KGuid) -> None:
        where = _name(placement, "placement", "report_ad_offered")
        if where is None:
            return
        if not isinstance(ad_type, AdType):
            _log.error("report_ad_offered: 'ad_type' must be an AdType, got %s; event dropped.", type(ad_type).__name__)
            return
        d.report_ad_offered(uid, where, int(ad_type))

    _with_user_dispatcher("report_ad_offered", user_id, _do)


def report_ad_revenue(
    user_id: UserId,
    placement: str,
    revenue_usd_cents: Optional[int] = None,
    localized_revenue: Optional[float] = None,
    currency_code: Optional[str] = None,
) -> None:
    """Reports ad revenue earned from ``user_id``. Provide either ``revenue_usd_cents`` or both
    ``localized_revenue`` and ``currency_code`` (ISO 4217)."""

    def _do(d: KServerDispatcher, uid: KGuid) -> None:
        args = _revenue(
            placement,
            "placement",
            revenue_usd_cents,
            "revenue_usd_cents",
            localized_revenue,
            "localized_revenue",
            currency_code,
            "report_ad_revenue",
        )
        if args is None:
            return
        if len(args) == 2:
            d.report_ad_revenue_usd(uid, *args)
        else:
            d.report_ad_revenue_local(uid, *args)

    _with_user_dispatcher("report_ad_revenue", user_id, _do)


def report_ad_items_granted(user_id: UserId, placement: str, items: Optional[Sequence[Item]] = None) -> None:
    """Reports virtual items granted to ``user_id`` for watching an ad."""

    def _do(d: KServerDispatcher, uid: KGuid) -> None:
        where = _name(placement, "placement", "report_ad_items_granted")
        granted = _items(items, "items", "report_ad_items_granted")
        if where is None or granted is None:
            return
        d.report_ad_items_granted(uid, where, granted)

    _with_user_dispatcher("report_ad_items_granted", user_id, _do)


def report_subscription_revenue(
    user_id: UserId,
    package_name: str,
    revenue_usd_cents: Optional[int] = None,
    localized_revenue: Optional[float] = None,
    currency_code: Optional[str] = None,
) -> None:
    """Reports a subscription billing event of ``user_id``. Provide either ``revenue_usd_cents`` or
    both ``localized_revenue`` and ``currency_code`` (ISO 4217)."""

    def _do(d: KServerDispatcher, uid: KGuid) -> None:
        args = _revenue(
            package_name,
            "package_name",
            revenue_usd_cents,
            "revenue_usd_cents",
            localized_revenue,
            "localized_revenue",
            currency_code,
            "report_subscription_revenue",
        )
        if args is None:
            return
        if len(args) == 2:
            d.report_subscription_revenue_usd(uid, *args)
        else:
            d.report_subscription_revenue_local(uid, *args)

    _with_user_dispatcher("report_subscription_revenue", user_id, _do)


def report_subscription_items_granted(
    user_id: UserId, package_name: str, items: Optional[Sequence[Item]] = None
) -> None:
    """Reports virtual items granted to ``user_id`` from an active subscription."""

    def _do(d: KServerDispatcher, uid: KGuid) -> None:
        pkg = _name(package_name, "package_name", "report_subscription_items_granted")
        granted = _items(items, "items", "report_subscription_items_granted")
        if pkg is None or granted is None:
            return
        d.report_subscription_items_granted(uid, pkg, granted)

    _with_user_dispatcher("report_subscription_items_granted", user_id, _do)


# --------------------------------------------------------------------------------------------
# Economy
# --------------------------------------------------------------------------------------------


def report_items_exchange(
    user_id: UserId,
    exchange_location: str,
    from_items: Optional[Sequence[Item]] = None,
    to_items: Optional[Sequence[Item]] = None,
) -> None:
    """Reports an item exchange by ``user_id``: ``from_items`` are deducted, ``to_items`` added."""

    def _do(d: KServerDispatcher, uid: KGuid) -> None:
        where = _name(exchange_location, "exchange_location", "report_items_exchange")
        src = _items(from_items, "from_items", "report_items_exchange")
        dst = _items(to_items, "to_items", "report_items_exchange")
        if where is None or src is None or dst is None:
            return
        d.report_item_exchange(uid, where, src, dst)

    _with_user_dispatcher("report_items_exchange", user_id, _do)


def report_items_reset(user_id: UserId, location: str, items: Optional[Sequence[Item]] = None) -> None:
    """Resets/initializes ``user_id``'s item balances at a location."""

    def _do(d: KServerDispatcher, uid: KGuid) -> None:
        where = _name(location, "location", "report_items_reset")
        balances = _items(items, "items", "report_items_reset")
        if where is None or balances is None:
            return
        d.report_items_reset(uid, where, balances)

    _with_user_dispatcher("report_items_reset", user_id, _do)


# --------------------------------------------------------------------------------------------
# Acquisition, progression, experimentation & diagnostics
# --------------------------------------------------------------------------------------------


def report_install_campaign(user_id: UserId, campaign_name: str) -> None:
    """Reports the marketing campaign that acquired ``user_id``."""

    def _do(d: KServerDispatcher, uid: KGuid) -> None:
        checked = _name(campaign_name, "campaign_name", "report_install_campaign")
        if checked is not None:
            d.report_install_campaign(uid, checked)

    _with_user_dispatcher("report_install_campaign", user_id, _do)


def report_game_language(user_id: UserId, language: str) -> None:
    """Reports the in-app language ``user_id`` uses."""

    def _do(d: KServerDispatcher, uid: KGuid) -> None:
        checked = _name(language, "language", "report_game_language")
        if checked is not None:
            d.report_game_language(uid, checked)

    _with_user_dispatcher("report_game_language", user_id, _do)


def report_onboarding_milestone(user_id: UserId, milestone_name: str) -> None:
    """Reports an onboarding milestone reached by ``user_id``, sent exactly as given (no occurrence
    counter is appended to repeats)."""

    def _do(d: KServerDispatcher, uid: KGuid) -> None:
        checked = _name(milestone_name, "milestone_name", "report_onboarding_milestone")
        if checked is not None:
            d.report_onboarding_milestone(uid, checked)

    _with_user_dispatcher("report_onboarding_milestone", user_id, _do)


def report_ab_test_group_assignment(user_id: UserId, test_name: str, group: str) -> None:
    """Assigns ``user_id`` to an A/B test group (a single character with code point 0..255)."""

    def _do(d: KServerDispatcher, uid: KGuid) -> None:
        test = _name(test_name, "test_name", "report_ab_test_group_assignment")
        if test is None or not _ab_group(group, "report_ab_test_group_assignment"):
            return
        d.assign_to_ab_test_group(uid, test, group)

    _with_user_dispatcher("report_ab_test_group_assignment", user_id, _do)


def log_error(user_id: UserId, message: str) -> None:
    """Logs a technical error in the context of ``user_id`` (truncated, never dropped, when long)."""

    def _do(d: KServerDispatcher, uid: KGuid) -> None:
        checked = _name(message, "message", "log_error", max_len=MAX_ERROR_LENGTH)
        if checked is not None:
            d.log_error(uid, checked)

    _with_user_dispatcher("log_error", user_id, _do)


class KeewanoServerSDK:
    """Namespaced facade of :mod:`keewano_sdk.server_sdk`; every attribute is the module-level callable."""

    initialize = staticmethod(initialize)
    shutdown = staticmethod(shutdown)
    flush = staticmethod(flush)
    is_initialized = staticmethod(is_initialized)
    report_in_app_purchase = staticmethod(report_in_app_purchase)
    report_in_app_purchase_items_granted = staticmethod(report_in_app_purchase_items_granted)
    report_ad_offered = staticmethod(report_ad_offered)
    report_ad_revenue = staticmethod(report_ad_revenue)
    report_ad_items_granted = staticmethod(report_ad_items_granted)
    report_subscription_revenue = staticmethod(report_subscription_revenue)
    report_subscription_items_granted = staticmethod(report_subscription_items_granted)
    report_items_exchange = staticmethod(report_items_exchange)
    report_items_reset = staticmethod(report_items_reset)
    report_install_campaign = staticmethod(report_install_campaign)
    report_game_language = staticmethod(report_game_language)
    report_onboarding_milestone = staticmethod(report_onboarding_milestone)
    report_ab_test_group_assignment = staticmethod(report_ab_test_group_assignment)
    log_error = staticmethod(log_error)
