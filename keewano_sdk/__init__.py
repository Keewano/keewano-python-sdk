"""Keewano native Python SDK.

A lightweight, dependency-free analytics SDK for native Python applications. It is the Python
counterpart of the Keewano SDKs and produces the **exact same binary batch format** (see
``docs-internal/data-format.md``), so the backend processes data from any SDK identically.

Quick start::

    import keewano_sdk
    from keewano_sdk import KeewanoConfig, Item, AdType

    keewano_sdk.initialize(KeewanoConfig(api_key="YOUR_KEEWANO_API_KEY", app_version="1.0.0"))

    keewano_sdk.set_user_id(1234567890)
    keewano_sdk.report_in_app_purchase("gems_100", price_usd_cents=499)
    keewano_sdk.report_in_app_purchase_items_granted("gems_100", [Item("gems", 100)])

For a backend that reports on behalf of many end users, use :mod:`keewano_sdk.server_sdk` instead —
every call there takes the user's id (see ``docs/server-side.md``)::

    from keewano_sdk import server_sdk as keewano, KeewanoServerConfig

    keewano.initialize(KeewanoServerConfig(api_key="YOUR_KEEWANO_API_KEY"))
    keewano.report_in_app_purchase(user_id, "gems_100", price_usd_cents=499)
"""

from __future__ import annotations

__version__ = "2.0.0"

from .ad_type import AdType
from . import server_sdk
from .codegen import KeewanoCodegen, KeewanoServerCodegen
from .config import DEFAULT_ENDPOINT, KeewanoConfig, KeewanoServerConfig
from .internal.custom_event_set import CustomEventSet
from .item import Item
from .sdk import (
    KeewanoSDK,
    flush,
    get_install_id,
    initialize,
    log_error,
    mark_as_test_user,
    report_ab_test_group_assignment,
    report_ad_items_granted,
    report_ad_offered,
    report_ad_revenue,
    report_button_click,
    report_game_language,
    report_in_app_purchase,
    report_in_app_purchase_items_granted,
    report_install_campaign,
    report_items_exchange,
    report_items_reset,
    report_app_pause,
    report_app_resume,
    report_deep_link,
    report_internet_connected,
    report_internet_disconnected,
    report_low_memory,
    report_onboarding_milestone,
    report_scene_loaded,
    report_scene_unloaded,
    report_subscription_items_granted,
    report_subscription_revenue,
    report_user_country,
    report_user_registered_before_sdk_integration,
    report_window_close,
    report_window_open,
    set_user_consent,
    set_user_id,
    shutdown,
)
from .server_sdk import KeewanoServerSDK, UserId

__all__ = [
    "__version__",
    "AdType",
    "CustomEventSet",
    "KeewanoCodegen",
    "Item",
    "KeewanoConfig",
    "KeewanoSDK",
    "KeewanoServerCodegen",
    "KeewanoServerConfig",
    "KeewanoServerSDK",
    "server_sdk",
    "UserId",
    "DEFAULT_ENDPOINT",
    "initialize",
    "shutdown",
    "flush",
    "set_user_consent",
    "report_user_registered_before_sdk_integration",
    "get_install_id",
    "set_user_id",
    "report_button_click",
    "report_window_open",
    "report_window_close",
    "report_in_app_purchase",
    "report_in_app_purchase_items_granted",
    "report_ad_offered",
    "report_ad_revenue",
    "report_ad_items_granted",
    "report_subscription_revenue",
    "report_subscription_items_granted",
    "report_items_exchange",
    "report_items_reset",
    "report_install_campaign",
    "report_game_language",
    "report_onboarding_milestone",
    "report_ab_test_group_assignment",
    "log_error",
    "mark_as_test_user",
    "report_app_pause",
    "report_app_resume",
    "report_internet_connected",
    "report_internet_disconnected",
    "report_low_memory",
    "report_deep_link",
    "report_scene_loaded",
    "report_scene_unloaded",
    "report_user_country",
]
