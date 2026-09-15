# Keewano Python SDK

A lightweight, **dependency-free** analytics SDK for **native Python applications** (desktop apps,
tools, games, and long-running clients) that sends player/user-behavior data to the Keewano
platform. It produces the **exact same binary batch format** as all Keewano SDKs so the backend
processes data from any SDK identically.

Designed performance-first: a compact binary event format, a background upload thread,
double-buffered batching, and offline-first disk persistence — using only the Python standard
library (no third-party packages).

**Requirements:** Python **3.8+**. No runtime dependencies.

---

## Documentation

| Guide | What's in it |
|---|---|
| **[Getting started](docs/getting-started.md)** | Install, initialize, run, and verify — in about five minutes. |
| **[Configuration reference](docs/configuration.md)** | Every `KeewanoConfig` option, the data directory, endpoint override, and test mode. |
| **[API reference](docs/api-reference.md)** | Every reporting method, its validation rules, and examples; the `Item` and `AdType` value types. |
| **[Automatic tracking](docs/automatic-tracking.md)** | The little the SDK captures on its own (launch burst, uncaught exceptions), and what you report yourself. |
| **[Privacy & consent](docs/privacy-and-consent.md)** | The GDPR/CCPA consent model, recording choices, and withdrawal. |
| **[Custom events](docs/custom-events.md)** | Adding your own app-specific events with the codegen. |

New here? Start with **[Getting started](docs/getting-started.md)**.

---

## Features

- **Single-call init** with automated environment reporting on launch (platform, OS, device, RAM, language).
- **Automatic diagnostics**: uncaught exceptions (main and worker threads) are captured and reported.
- **Monetization**: in-app purchases, ad revenue, and subscription revenue (US-cents or local
  currency), plus granted-items tracking for each.
- **Virtual economy**: item exchange and item reset events.
- **Progression & experimentation**: onboarding milestones and A/B test group assignment.
- **Privacy**: GDPR/CCPA consent gating with durable, offline-safe local buffering.
- **Resilient**: batches persist to disk (capped at 50 MB), survive restarts, and upload when a
  network is available.

---

## Installation

```bash
pip install keewano-sdk
```

Or, while working from this monorepo, put the package on your path:

```bash
PYTHONPATH=<path to this directory> python your_app.py
```

---

## Quick start

```python
import keewano_sdk
from keewano_sdk import KeewanoConfig, Item, AdType

keewano_sdk.initialize(KeewanoConfig(
    api_key="YOUR_KEEWANO_API_KEY",
    app_version="1.0.0",          # there's no manifest to read this from — supply it here
))
```

That's it. The SDK reports a launch burst immediately and uploads on a background thread. It also
registers an `atexit` flush, so events are sent when your process exits normally. For a hard-stop
flush at a known point, call `keewano_sdk.shutdown()`.

Both call styles are equivalent — module-level functions or the `KeewanoSDK` facade:

```python
keewano_sdk.report_button_click("Play")
# is the same callable as
from keewano_sdk import KeewanoSDK
KeewanoSDK.report_button_click("Play")
```

---

## Reporting events

All reporting methods are safe to call from any thread; work is handed off to a background thread.
Calls made before `initialize()` are dropped with a warning.

```python
# Identity — assign once per installation.
keewano_sdk.set_user_id(1234567890)                         # numeric id (up to 64-bit)
keewano_sdk.set_user_id("12345678-9abc-def0-1122-334455667788")  # GUID/UUID string

# In-app purchases (ONLY after your server validates the receipt).
keewano_sdk.report_in_app_purchase("gems_100", price_usd_cents=499)
keewano_sdk.report_in_app_purchase("gems_100", localized_price=4.99, currency_code="EUR")
keewano_sdk.report_in_app_purchase_items_granted("gems_100", [Item("gems", 100)])

# Ads.
keewano_sdk.report_ad_offered("level_complete", AdType.REWARDED)
keewano_sdk.report_ad_revenue("level_complete", revenue_usd_cents=2)
keewano_sdk.report_ad_items_granted("level_complete", [Item("coins", 50)])

# Subscriptions (report each billing event: purchase, trial conversion, renewal).
keewano_sdk.report_subscription_revenue("vip_monthly", revenue_usd_cents=999)
keewano_sdk.report_subscription_items_granted("vip_monthly", [Item("vip_chest", 1)])

# Virtual economy.
keewano_sdk.report_items_exchange("shop", from_items=[Item("coins", 100)], to_items=[Item("sword")])
keewano_sdk.report_items_reset("inventory_init", [Item("coins", 500)])

# Progression & experimentation.
keewano_sdk.report_onboarding_milestone("finished_tutorial_step_1")
keewano_sdk.report_ab_test_group_assignment("new_shop_layout", "B")

# UI.
keewano_sdk.report_button_click("PlayButton")
keewano_sdk.report_window_open("SettingsPopup")
keewano_sdk.report_window_close("SettingsPopup")

# Acquisition & misc.
keewano_sdk.report_install_campaign("summer_promo")
keewano_sdk.report_game_language("fr")
keewano_sdk.log_error("Custom diagnostic message")

# Lifecycle, connectivity & environment (captured automatically on mobile; reported explicitly here).
keewano_sdk.report_app_pause()
keewano_sdk.report_app_resume()
keewano_sdk.report_internet_connected()
keewano_sdk.report_internet_disconnected()
keewano_sdk.report_low_memory()
keewano_sdk.report_deep_link("myapp://open/item/42")
keewano_sdk.report_scene_loaded("MainMenu")
keewano_sdk.report_scene_unloaded("MainMenu")
keewano_sdk.report_user_country("US")
```

### Custom events

The built-in methods cover common monetization, economy and progression cases. For events specific
to your app, use the [codegen](https://github.com/Keewano/keewano-codegen): declare your events once
and it generates typed `report_enemy_killed("orc")`-style functions into your project, which you
hand to `initialize()` as a `custom_event_set`.

The workflow and payload types are in **[Custom events](docs/custom-events.md)**; the generator's
own flags, commands and definitions format are in its
[README](https://github.com/Keewano/keewano-codegen#readme).

---

## Privacy & consent

When `require_user_consent=True`, the SDK buffers data locally but sends nothing until you record
the user's choice:

```python
keewano_sdk.set_user_consent(True)   # flush buffered data and keep collecting
keewano_sdk.set_user_consent(False)  # discard buffered data and stop
```

The decision is persisted across launches.

### Existing applications

If you integrate into an app with an established user base, report veteran users' original
registration date so they aren't miscounted as new users (effective once per install):

```python
from datetime import datetime, timezone
keewano_sdk.report_user_registered_before_sdk_integration(
    datetime(2021, 6, 1, tzinfo=timezone.utc)
)
```

---

## Where data is stored

Durable identifiers, the consent decision, and pending batches live in a per-user application-data
directory, chosen with the standard library and to each such prefix a unique (based on the API key) sub directory is added:

| OS      | Default location                                   |
|---------|----------------------------------------------------|
| Linux   | `$XDG_DATA_HOME/keewano` (else `~/.local/share/keewano`) |
| macOS   | `~/Library/Application Support/Keewano`             |
| Windows | `%LOCALAPPDATA%\Keewano`                            |

Override it with `KeewanoConfig(data_dir=...)`.

Note: if running multiple processes on the same file system with the same API key then there will be a colision on the
data directory. If this is the case, override the directory setting so that each process has its own directory.

---

## Project layout

```
<repo directory>/
├── pyproject.toml
├── keewano_sdk/
│   ├── __init__.py               # public API exports
│   ├── sdk.py                    # public API + init + exception hooks + atexit flush
│   ├── config.py                 # KeewanoConfig
│   ├── item.py, ad_type.py       # public value types
│   └── internal/                 # engine (not part of the public API)
│       ├── dispatcher.py         # double-buffered batching + upload thread
│       ├── batch.py / serializer.py     # .kwub batch format
│       ├── buffer.py / guid.py          # byte-exact wire encoding
│       ├── network.py            # HTTP transport (K-* headers, urllib)
│       ├── storage.py            # durable identifiers / consent
│       ├── environment.py        # host/device introspection for the launch burst
│       ├── paths.py              # default per-user data directory
│       ├── events.py             # permanent predefined event IDs
│       └── consent.py            # consent state enum
├── sample/main.py                # runnable example
├── tests/test_binary_format.py   # wire-format regression tests
└── docs/                         # user-facing guides (getting started, API reference, …)
```

---

## Testing

For integration testing, tag your data to isolate it from production analytics:

```python
keewano_sdk.mark_as_test_user("your-name")
```

(note that a preferable method is to create a separate project on the Keewano platform for testing)

--

## Building & testing

```bash
python -m unittest discover -s tests    # run wire-format regression tests
python -m build                         # build the wheel/sdist (needs the `build` package)
```

## License

[Apache License 2.0](LICENSE.md).
