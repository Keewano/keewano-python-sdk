"""Runnable example for the Keewano native Python SDK.

Run from the repository root with the SDK on the path::

    PYTHONPATH=. python sample/main.py

Configuration is read from environment variables so you can exercise the SDK without editing code:

    KEEWANO_API_KEY                    project API key (default: a placeholder)
    KEEWANO_ENDPOINT                   override the ingress base URL (for local/staging testing)
    KEEWANO_REQUIRE_USER_CONSENT       "1"/"true" to buffer until consent is granted (GDPR/CCPA)
    KEEWANO_DISABLE_EXCEPTION_TRACKING "1"/"true" to not auto-report uncaught exceptions
    KEEWANO_PROXY_AUTH_BEARER          bearer token for an authenticating proxy in front of ingress
    KEEWANO_SET_USER_ID                user ID to assign during the run, e.g. 2234567890. Unset
                                       means set_user_id is never called.

Data is tagged with :func:`keewano_sdk.mark_as_test_user` so it is excluded from production analytics.
"""

import os
import time

import keewano_sdk
from keewano_sdk import DEFAULT_ENDPOINT, AdType, Item, KeewanoConfig
import keewano_custom_events


def _env_bool(name: str, default: bool = False) -> bool:
    """Parses a boolean environment variable. Truthy values: 1/true/yes/on (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def main() -> None:
    require_consent = _env_bool("KEEWANO_REQUIRE_USER_CONSENT", False)
    keewano_sdk.initialize(
        KeewanoConfig(
            api_key=os.environ.get("KEEWANO_API_KEY", None),
            app_version="1.0.0",
            endpoint=os.environ.get("KEEWANO_ENDPOINT") or DEFAULT_ENDPOINT,
            require_user_consent=require_consent,
            disable_exception_tracking=_env_bool("KEEWANO_DISABLE_EXCEPTION_TRACKING", False),
            proxy_auth_bearer=os.environ.get("KEEWANO_PROXY_AUTH_BEARER", None),
            custom_event_set=keewano_custom_events.CUSTOM_EVENT_SET,
        )
    )
    # keewano_sdk.mark_as_test_user("sample-runner")

    # With consent gating enabled, nothing uploads until consent is recorded. Ask the user on the
    # command line and only collect data if they agree.
    if require_consent:
        try:
            answer = input("Allow Keewano to collect analytics data? [y/N]: ")
        except EOFError:
            answer = ""  # non-interactive stdin: treat as no answer -> do not consent
        if answer.strip().lower() in ("y", "yes"):
            keewano_sdk.set_user_consent(True)
            print("Consent granted - events will be uploaded.")
        else:
            keewano_sdk.set_user_consent(False)
            print("Consent denied - buffered data is discarded and collection stops.")

    print("Install id:", keewano_sdk.get_install_id())

    # Identity - assign once per installation.
    user_id_to_use = os.environ.get("KEEWANO_SET_USER_ID", None)
    if user_id_to_use is not None:
        user_id_int: int = int(user_id_to_use)
        keewano_sdk.set_user_id(user_id_int)

    # Monetization (report ONLY after your server validates the receipt).
    keewano_sdk.report_in_app_purchase("gems_100", price_usd_cents=499)
    keewano_sdk.report_in_app_purchase_items_granted("gems_100", [Item("gems", 100)])

    keewano_sdk.report_ad_offered("level_complete", AdType.REWARDED)
    keewano_sdk.report_ad_revenue("level_complete", revenue_usd_cents=2)
    keewano_sdk.report_ad_items_granted("level_complete", [Item("coins", 50)])

    # Virtual economy.
    keewano_sdk.report_items_exchange("shop", from_items=[Item("coins", 100)], to_items=[Item("sword")])

    # Progression & experimentation.
    keewano_sdk.report_onboarding_milestone("finished_tutorial_step_1")
    keewano_sdk.report_ab_test_group_assignment("new_shop_layout", "B")

    # UI & misc.
    keewano_sdk.report_window_open("SettingsPopup")
    keewano_sdk.report_button_click("PlayButton")
    keewano_sdk.report_window_close("SettingsPopup")

    # Custom events.
    keewano_custom_events.report_save_world()
    keewano_custom_events.report_solve_hunger(False)

    # Request an upload, then give the background sender a moment to ship the buffered events
    # before we stop it. Uploads are asynchronous, so without this wait shutdown() could stop the
    # sender mid-flight (the events stay safely on disk and would be sent on the next run).
    keewano_sdk.flush()
    # Sleep longer than the wait timeout of the sender thread to make sure all events are sent.
    time.sleep(40)
    try:
        answer = input("Press any key for app shutdown")
    except EOFError:
        pass

    keewano_sdk.shutdown()
    print("Done - events flushed.")


if __name__ == "__main__":
    main()
