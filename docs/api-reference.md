# API reference

Every public entry point is available in two equivalent ways:

- as a **module-level function** — `keewano_sdk.report_button_click("Play")`, and
- as a method on the **`KeewanoSDK` facade** — `KeewanoSDK.report_button_click("Play")`.

They are the *same* callables (`KeewanoSDK.report_button_click is keewano_sdk.report_button_click`).
All of them are:

- **Thread-safe** — safe to call from any thread; work is handed to a background thread.
- **Non-throwing** — an invalid argument is **dropped with a logged warning**, never raised, and a
  call made before initialization is dropped too. Reporting can never crash your app or block your
  thread.

Contents:

- [Validation & drop rules](#validation--drop-rules)
- [Initialization & lifecycle](#initialization--lifecycle)
- [Identity](#identity)
- [Privacy & consent](#privacy--consent)
- [UI events](#ui-events)
- [Monetization — in-app purchases](#monetization--in-app-purchases)
- [Monetization — ads](#monetization--ads)
- [Monetization — subscriptions](#monetization--subscriptions)
- [Virtual economy](#virtual-economy)
- [Acquisition, progression & experimentation](#acquisition-progression--experimentation)
- [Lifecycle, connectivity & environment](#lifecycle-connectivity--environment)
- [Diagnostics & testing](#diagnostics--testing)
- [Custom events](#custom-events)
- [Value types](#value-types)

---

## Validation & drop rules

Reporting methods validate at the boundary so a bad value never lands on the wire silently. The rules
below are referenced throughout the reference.

| Rule | Applies to | Behavior |
|---|---|---|
| **Non-blank name** | every label/name string (button, product, placement, package, location, campaign, language, milestone, currency code, test/tester name) | Empty or whitespace-only ⇒ the whole event is **dropped**. |
| **Name length ≤ 256** | the same label/name strings | Longer values are **truncated** to 256 characters, not dropped. |
| **Unsigned integer (`uint32`)** | US-cent amounts, item counts | Must be `0 … 4,294,967,295`. Negative or larger ⇒ **dropped** (never wrapped). |
| **Amount (float)** | `localized_price` / `localized_revenue` | Must be finite and `≥ 0`. `NaN`, `Infinity`, or negative ⇒ **dropped**. |
| **Item list** | every `list[Item]` | Each `Item.name` must be non-blank and ≤ 256, and each `Item.count` a valid `uint32`, else the whole event is dropped. At most **512** items per event. |
| **Error message** | `log_error` | Non-blank; truncated to **8,192** characters (traces are worth keeping over dropping). |
| **A/B group** | `report_ab_test_group_assignment` | A single character with a code point in `0…255` (one wire byte). Non-string, not one character, or code point > 255 ⇒ dropped. |

Dropped events are logged through the standard `logging` module under the `keewano_sdk` logger, with
the offending parameter named.

---

## Initialization & lifecycle

### initialize

```python
keewano_sdk.initialize(config: KeewanoConfig) -> None
```

Starts the SDK. Call once at startup. Idempotent, safe from any thread, and never throws. See the
[Configuration reference](configuration.md).

### flush

```python
keewano_sdk.flush() -> None
```

Requests an immediate upload of buffered events. Best-effort — the upload runs asynchronously on the
background thread.

### shutdown

```python
keewano_sdk.shutdown() -> None
```

Flushes and stops the background sender. Registered as an `atexit` hook, so a normal interpreter exit
calls it for you; call it explicitly to stop at a known point. Anything not yet uploaded remains on
disk for the next run.

---

## Identity

### get_install_id

```python
keewano_sdk.get_install_id() -> Optional[str]
```

The unique, anonymous installation ID (a GUID string), or `None` if the SDK is not initialized or the
identifiers could not be read. Served from the copy loaded during `initialize()` — no file I/O on
your thread. Also a convenient health check: `None` means the SDK did not start.

### set_user_id

```python
keewano_sdk.set_user_id(uid: int)   # numeric id (up to 64-bit)
keewano_sdk.set_user_id(uid: str)   # GUID/UUID string
```

Associates *your* user id with this installation. **Assign once** per installation (typically right
after login). The string overload must be a valid GUID/UUID; an invalid string (or a non-`int`,
non-`str` value) is logged and dropped. This links the SDK's anonymous install to your backend's
user identity.

---

## Privacy & consent

See the dedicated [Privacy & consent](privacy-and-consent.md) guide for the full lifecycle.

### set_user_consent

```python
keewano_sdk.set_user_consent(consent_given: bool) -> None
```

Records the user's data-collection choice (GDPR/CCPA). When consent is required, data is buffered
locally until this is called with `True`. Calling with `False` **discards buffered data on disk and
in memory and stops collection** — honored from any prior state, including after a previous `True`.
The decision is persisted across launches.

### report_user_registered_before_sdk_integration

```python
keewano_sdk.report_user_registered_before_sdk_integration(original_registration_time: datetime) -> None
```

For apps with an existing user base: reports a veteran user's original registration date so they are
not miscounted as new. **Effective once per installation.** The datetime must be in the past; a
future-dated call is ignored.

```python
from datetime import datetime, timezone
keewano_sdk.report_user_registered_before_sdk_integration(datetime(2021, 6, 1, tzinfo=timezone.utc))
```

---

## UI events

There is no automatic UI tracking in a native Python application (see
[Automatic tracking](automatic-tracking.md)), so report interactions explicitly where they happen.

### report_button_click / report_window_open / report_window_close

```python
keewano_sdk.report_button_click(button_name: str) -> None
keewano_sdk.report_window_open(window_name: str) -> None
keewano_sdk.report_window_close(window_name: str) -> None
```

Report an interactive-element click, and a window/popup opening or closing. Names follow the
[non-blank / ≤256](#validation--drop-rules) rule.

---

## Monetization — in-app purchases

> **Report purchases only after your server validates the receipt.** These events feed revenue
> analytics; reporting unvalidated purchases pollutes them.

### report_in_app_purchase

```python
# US cents (integer):
keewano_sdk.report_in_app_purchase(product_name: str, price_usd_cents: int)

# Local currency (ISO 4217 code):
keewano_sdk.report_in_app_purchase(product_name: str, localized_price: float, currency_code: str)
```

Reports a validated purchase. Pass `price_usd_cents` when you have a USD-normalized price, or
`localized_price` with `currency_code` to preserve the currency the user actually paid in.

```python
keewano_sdk.report_in_app_purchase("gems_100", price_usd_cents=499)
keewano_sdk.report_in_app_purchase("gems_100", localized_price=4.99, currency_code="EUR")
```

### report_in_app_purchase_items_granted

```python
keewano_sdk.report_in_app_purchase_items_granted(product_name: str, items: Optional[Sequence[Item]] = None) -> None
```

Reports the virtual items a validated purchase granted, linking spend to what the player received.

```python
keewano_sdk.report_in_app_purchase_items_granted("gems_100", [Item("gems", 100)])
```

---

## Monetization — ads

### report_ad_offered

```python
keewano_sdk.report_ad_offered(placement: str, ad_type: AdType) -> None
```

Reports that an ad opportunity was presented (whether or not it was taken). `placement` is a label
you choose (e.g. `"level_complete"`); `ad_type` is an [`AdType`](#adtype).

### report_ad_revenue

```python
keewano_sdk.report_ad_revenue(placement: str, revenue_usd_cents: int)
keewano_sdk.report_ad_revenue(placement: str, localized_revenue: float, currency_code: str)
```

Reports revenue attributed to an ad impression, in US cents or local currency.

### report_ad_items_granted

```python
keewano_sdk.report_ad_items_granted(placement: str, items: Optional[Sequence[Item]] = None) -> None
```

Reports items granted for watching an ad (e.g. a rewarded video).

```python
keewano_sdk.report_ad_offered("level_complete", AdType.REWARDED)
keewano_sdk.report_ad_revenue("level_complete", revenue_usd_cents=2)
keewano_sdk.report_ad_items_granted("level_complete", [Item("coins", 50)])
```

---

## Monetization — subscriptions

### report_subscription_revenue

```python
keewano_sdk.report_subscription_revenue(package_name: str, revenue_usd_cents: int)
keewano_sdk.report_subscription_revenue(package_name: str, localized_revenue: float, currency_code: str)
```

Reports subscription billing revenue. Call it for **each** billing event — initial purchase, trial
conversion, and every renewal — so lifetime value tracks correctly.

### report_subscription_items_granted

```python
keewano_sdk.report_subscription_items_granted(package_name: str, items: Optional[Sequence[Item]] = None) -> None
```

Reports items granted by an active subscription (e.g. a monthly VIP chest).

```python
keewano_sdk.report_subscription_revenue("vip_monthly", revenue_usd_cents=999)
keewano_sdk.report_subscription_items_granted("vip_monthly", [Item("vip_chest", 1)])
```

---

## Virtual economy

### report_items_exchange

```python
keewano_sdk.report_items_exchange(
    exchange_location: str,
    from_items: Optional[Sequence[Item]] = None,
    to_items: Optional[Sequence[Item]] = None,
) -> None
```

Reports an exchange at a location: `from_items` are deducted, `to_items` are added. Pass nothing for
one side to model a pure grant or a pure sink.

```python
keewano_sdk.report_items_exchange(
    "blacksmith",
    from_items=[Item("coins", 100)],
    to_items=[Item("sword")],          # Item count defaults to 1
)
```

### report_items_reset

```python
keewano_sdk.report_items_reset(location: str, items: Optional[Sequence[Item]] = None) -> None
```

Resets/initializes the user's item balances at a location — for example seeding a new player's
starting inventory, or reconciling balances after a server correction.

```python
keewano_sdk.report_items_reset("inventory_init", [Item("coins", 500)])
```

---

## Acquisition, progression & experimentation

### report_install_campaign

```python
keewano_sdk.report_install_campaign(campaign_name: str) -> None
```

Reports the marketing campaign that acquired this user.

### report_game_language

```python
keewano_sdk.report_game_language(language: str) -> None
```

Reports the in-game language, useful when it differs from the host's system language (which the SDK
already captures automatically at launch).

### report_onboarding_milestone

```python
keewano_sdk.report_onboarding_milestone(milestone_name: str) -> None
```

Reports a milestone reached during onboarding/tutorial. These build the first-time-user-experience
(FTUE) funnel.

### report_ab_test_group_assignment

```python
keewano_sdk.report_ab_test_group_assignment(test_name: str, group: str) -> None
```

Assigns the user to an A/B test group. `group` is a **single character** whose code point fits in one
byte (`0…255`) — the backend reads the group as a single byte. A non-string, a string that is not
exactly one character, or a character above code point 255 (e.g. `"€"`) drops the event.

```python
keewano_sdk.report_ab_test_group_assignment("new_shop_layout", "B")
```

---

## Lifecycle, connectivity & environment

The mobile SDKs capture these from OS lifecycle and platform signals. A native Python application has
none of that machinery (see [Automatic tracking](automatic-tracking.md)), so report them explicitly
where your app knows about them. The payload-less events take no arguments; the string events follow
the [non-blank / ≤256](#validation--drop-rules) rule — except `report_deep_link`, whose URL keeps a
larger 8,192-character budget (a truncated URL is far less useful than a long one).

### report_app_pause / report_app_resume

```python
keewano_sdk.report_app_pause() -> None
keewano_sdk.report_app_resume() -> None
```

Report a session boundary — the app going to the background/pausing and returning to the
foreground/resuming. These are analytics events only; they do **not** change the SDK's upload
behavior.

### report_internet_connected / report_internet_disconnected

```python
keewano_sdk.report_internet_connected() -> None
keewano_sdk.report_internet_disconnected() -> None
```

Report that network connectivity was (re)established or lost.

### report_low_memory

```python
keewano_sdk.report_low_memory() -> None
```

Reports a low-memory warning from the host.

### report_deep_link

```python
keewano_sdk.report_deep_link(link: str) -> None
```

Reports that the app was opened or activated via a deep link. Because deep links are URLs rather than
short labels, `link` is truncated at **8,192** characters instead of the 256-char dimension limit
(blank still drops the event).

### report_scene_loaded / report_scene_unloaded

```python
keewano_sdk.report_scene_loaded(scene_name: str) -> None
keewano_sdk.report_scene_unloaded(scene_name: str) -> None
```

Report a scene/screen being loaded or unloaded — the manual analogue of the mobile SDKs' automatic
scene tracking.

### report_user_country

```python
keewano_sdk.report_user_country(country: str) -> None
```

Reports the user's country (e.g. an ISO 3166 code or country name) when your app determines it
independently of what the SDK captures at launch.

---

## Diagnostics & testing

### log_error

```python
keewano_sdk.log_error(message: str) -> None
```

Manually logs a technical error. Uncaught exceptions are captured
[automatically](automatic-tracking.md) (unless disabled), so use this for handled errors you still
want visibility into. The message is truncated to 8,192 characters rather than dropped, so stack
traces survive.

### mark_as_test_user

```python
keewano_sdk.mark_as_test_user(tester_name: str) -> None
```

Marks this install as a test user so Keewano's AI excludes it from production analytics. Call it on
QA/dev builds.

---

## Custom events

Beyond the built-in events above, you can define your own app-specific events. You declare them once
and the Keewano **[codegen](https://github.com/Keewano/keewano-codegen)** generates typed reporter
functions into your project, so your app calls read like `report_enemy_killed("orc")`. It installs
with `pip install keewano-codegen`; see its
[README](https://github.com/Keewano/keewano-codegen#readme).

The full contract, supported payload types, and a worked example are in
**[Custom events](custom-events.md)**. App code does not call the low-level `KeewanoCodegen` bridge
directly — the generated file does.

---

## Value types

### Item

```python
Item(name: str, count: int = 1)
```

A game/app item and a quantity. `count` defaults to `1`. Used by every `…_items_granted`,
`report_items_exchange`, and `report_items_reset` call. `name` must be non-blank and ≤ 256 chars, and
`count` a valid `uint32` (see [rules](#validation--drop-rules)).

### AdType

```python
class AdType(IntEnum):
    REWARDED = 1
    INTERSTITIAL = 2
    BANNER = 3
    PLAYABLE = 4
    OFFERWALL = 5
```

| Value | Meaning |
|---|---|
| `REWARDED` | Rewarded video — the user opts in to watch for an in-game reward. |
| `INTERSTITIAL` | Full-screen ad at a transition point. |
| `BANNER` | Small persistent on-screen ad. |
| `PLAYABLE` | Interactive mini-game ad. |
| `OFFERWALL` | A list of tasks/offers for rewards. |
