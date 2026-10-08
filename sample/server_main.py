"""Runnable example for the Keewano server-side SDK: one process reporting for many users.

Run from the repository root with the SDK on the path::

    PYTHONPATH=. python sample/server_main.py

Configuration is read from environment variables:

    KEEWANO_API_KEY             project API key (default: a placeholder)
    KEEWANO_ENDPOINT            override the ingress base URL (for local/staging testing)
    KEEWANO_PROXY_AUTH_BEARER   bearer token for an authenticating proxy in front of ingress
    KEEWANO_NO_PERSISTENT_STORAGE if set to True, do not use persistent storage (RAM only)
    else if False or not set use persistent storage
    KEEWANO_TEST_USER_NAME if set data is tagged with the name so it is excluded from
    production analytics.
"""

import os
import random
import uuid

from keewano_sdk import DEFAULT_ENDPOINT, AdType, Item, KeewanoServerConfig
from keewano_sdk import server_sdk as keewano
import keewano_custom_events


def main() -> None:
    no_persistent_storage = os.environ.get("KEEWANO_NO_PERSISTENT_STORAGE")
    persistent_storage = True
    if no_persistent_storage is not None and no_persistent_storage == "True":
        persistent_storage = False
    keewano.initialize(
        KeewanoServerConfig(
            api_key=os.environ.get("KEEWANO_API_KEY", "YOUR_KEEWANO_API_KEY"),
            endpoint=os.environ.get("KEEWANO_ENDPOINT", DEFAULT_ENDPOINT),
            proxy_auth_bearer=os.environ.get("KEEWANO_PROXY_AUTH_BEARER"),
            persistent_storage=persistent_storage,
            custom_event_set=keewano_custom_events.CUSTOM_EVENT_SET,
            test_user_name=os.environ.get("KEEWANO_TEST_USER_NAME"),
        )
    )

    # Pretend to serve a few users: numeric ids, GUID strings and uuid.UUID are all accepted.
    users = [1001, 1002, str(uuid.uuid4()), uuid.uuid4()]
    for user in users:
        keewano.report_install_campaign(user, "server_sample")
        keewano.report_onboarding_milestone(user, "created_account")
        if random.random() < 0.5:
            keewano.report_in_app_purchase(user, "gems_100", price_usd_cents=499)
            keewano.report_in_app_purchase_items_granted(user, "gems_100", [Item("gems", 100)])
        keewano.report_ad_offered(user, "daily_bonus", AdType.REWARDED)
        keewano.report_items_exchange(user, "shop", from_items=[Item("coins", 50)], to_items=[Item("sword")])
        # Custom events: the generated module has a server_ reporter, user first, for every event.
        keewano_custom_events.server_report_save_world(user)
        keewano_custom_events.server_report_solve_hunger(user, random.random() < 0.5)

    # Each user's events were aggregated into one batch. shutdown() (also run automatically at exit)
    # seals them all and uploads before returning.
    keewano.shutdown()
    print(f"Reported events for {len(users)} users.")


if __name__ == "__main__":
    main()
