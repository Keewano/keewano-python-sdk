# Custom events

The SDK's [built-in events](api-reference.md) cover common monetization, economy, and progression
cases. **Custom events** let you record anything specific to your app — `enemy_killed`,
`level_started`, `chest_opened` — and call them like ordinary functions:

```python
from myapp import keewano_custom_events

keewano_custom_events.report_enemy_killed("orc")
```

You don't write these functions by hand and you don't edit the SDK. You declare your events once, run
the **Keewano custom-events codegen**, and it generates typed reporter functions into your own
project.

- [When to use a custom event](#when-to-use-a-custom-event)
- [The workflow](#the-workflow)
- [Payload types](#payload-types)
- [Calling generated reporters](#calling-generated-reporters)
- [What gets added to your project](#what-gets-added-to-your-project)
- [Rules & gotchas](#rules--gotchas)
- [Server-side SDK](#server-side-sdk)

---

## When to use a custom event

Reach for a custom event when you want to track something the built-in API doesn't model. Prefer a
built-in method when one fits — `report_in_app_purchase`, `report_items_exchange`,
`report_onboarding_milestone`, and friends carry structured meaning the Keewano analyst understands
out of the box. Use custom events for the gameplay- or product-specific signals that are unique to
you.

## The workflow

Adding a custom event is three steps:

1. **Declare the event** — its name and the type of payload it carries (if any) — in your Keewano
   custom-events definitions. The codegen maintains that file for you:

   ```bash
   keewano-codegen add EnemyKilled --type string
   ```

2. **Run the codegen.** The
   [Keewano custom-events codegen](https://github.com/Keewano/keewano-codegen) reads your
   definitions and writes **one Python file into your app** — the typed reporters plus the event-set
   definition:

   ```bash
   keewano-codegen --target python --code myapp
   ```

3. **Pass the set to `initialize()` and call the generated functions** (see below).

### Getting the codegen

It is a build-time tool and never ships inside your app. `pip install` is all it takes — the wheel
for your platform carries a self-contained executable, so there is no Node and nothing to compile:

```bash
pip install keewano-codegen
```

(The same generator is on npm as `@keewano/codegen`, for a project that already has Node.) Its
[README](https://github.com/Keewano/keewano-codegen#readme) is the reference for the definitions
file format, every flag and command, and the
[`--target python` walkthrough](https://github.com/Keewano/keewano-codegen#targets) the command
above comes from.

> **Reporting the same events from a mobile app too?** Keep **one** definitions file — in its own
> repository or one shared directory — and generate from it in every project rather than copying it.
> The event set's identity is a hash over the file, so two copies that drift report as two different
> schemas. See
> [Sharing one definitions file across SDKs](https://github.com/Keewano/keewano-codegen#sharing-one-definitions-file-across-sdks).

There is **no SDK code to change**. Unlike the mobile SDKs, there is also no asset file and no
launch-time auto-load: you hand the generated event set to `initialize()` explicitly, matching the
rest of the Python SDK's "you own the call" model.

## Payload types

A custom event carries at most one payload value. The supported types:

| Payload | Generated function parameter | Example |
|---|---|---|
| *(none)* | — | `keewano_custom_events.report_daily_bonus_claimed()` |
| Integer (signed) | `int` | `keewano_custom_events.report_score_delta(-25)` |
| Integer (unsigned) | `int` | `keewano_custom_events.report_enter_main_level(12)` |
| Boolean | `bool` | `keewano_custom_events.report_tutorial_skipped(True)` |
| Float | `float` | `keewano_custom_events.report_difficulty_multiplier(1.5)` |
| String | `str` | `keewano_custom_events.report_enemy_killed("orc")` |
| Point (x, y) | `int, int` | `keewano_custom_events.report_tap_position(240, 880)` |

Python has a single `int` type, so — unlike the mobile SDKs' distinct 32-/64-bit integer types — the
signed-vs-unsigned choice can't be recovered from the value. The codegen therefore emits the correct
reporter for how you declared the event, and the wire type is fixed by that, not by the runtime
value. String and numeric payloads are validated the same way as
[built-in events](api-reference.md#validation--drop-rules) (an empty string or an out-of-range number
drops the event with a logged warning).

## Calling generated reporters

The generated reporters are **plain module-level functions** in your app's file, so you import and
call them like any other function:

```python
from myapp import keewano_custom_events

keewano_custom_events.report_enemy_killed("orc")
```

Like every SDK call, they are safe from any thread, never throw, and are dropped with a warning if
the SDK isn't initialized.

> **Why functions, not methods on `KeewanoSDK`?** The mobile SDKs bolt generated reporters onto the
> `KeewanoSDK` type via language extensions. Python has no equivalent worth the tradeoff:
> monkey-patching a class imported from an installed package is a global side effect that defeats
> static typing and autocomplete. Standalone functions are typed, greppable, and imported explicitly.

## What gets added to your project

The codegen writes (**and overwrites**) exactly one file **inside your app**, e.g.
`myapp/keewano_custom_events.py`. It contains:

- a `CUSTOM_EVENT_SET` built with `CustomEventSet.from_gzip_base64(...)`, and
- one `def report_xxx(...)` per declared event, each delegating to the SDK's `KeewanoCodegen` bridge.

You pass the set at startup and call the reporters:

```python
import keewano_sdk
from keewano_sdk import KeewanoConfig
from myapp import keewano_custom_events

keewano_sdk.initialize(KeewanoConfig(
    api_key="YOUR_KEEWANO_API_KEY",
    custom_event_set=keewano_custom_events.CUSTOM_EVENT_SET,
))

keewano_custom_events.report_enemy_killed("orc")
```

The file lives in **your** package, never in `keewano_sdk/…`. That is deliberate: a file generated
into the installed SDK would be wiped by `pip install --upgrade`, is read-only in many environments,
and is shared across every project in the environment — but custom events are per-application. Commit
the generated file with your source (or generate it as a build step).

## Rules & gotchas

- **Don't hand-edit the generated file.** It is overwritten in full on every codegen run; changes are
  lost. To add an event, change your definitions and re-run the codegen.
- **Event ids are permanent.** Once an event has an id, that id must never change or be reused for a
  different event — the backend keys analytics off it. Adding new events is always safe; renumbering
  or repurposing an old id corrupts historical data.
- **Pass the set every run.** The SDK persists the definition blob and registers it with the backend
  on first upload; if you stop passing `custom_event_set`, batches are tagged "no custom events".
- **A missing or absent set is safe.** With `custom_event_set=None` (or `version == 0`) the SDK
  degrades to "no custom events" rather than failing — so an app that hasn't run the codegen yet still
  initializes normally.

> Implementing the codegen tool itself? The contract it implements — the encoding, the id numbering
> and the hash that identifies an event set — is
> [The frozen contract](https://github.com/Keewano/keewano-codegen#the-frozen-contract) in the codegen's own README.

## Server-side SDK

The generated module serves the [server-side SDK](server-side.md) too: next to every client reporter
`report_<name>(...)` it defines `server_report_<name>(user_id, ...)`, which takes the end user's id
first. Pass `CUSTOM_EVENT_SET` as `KeewanoServerConfig.custom_event_set` and call the `server_`
reporters:

```python
from myapp import keewano_custom_events
from keewano_sdk import server_sdk as keewano, KeewanoServerConfig

keewano.initialize(KeewanoServerConfig(api_key="…", custom_event_set=keewano_custom_events.CUSTOM_EVENT_SET))
keewano_custom_events.server_report_enemy_killed(user_id, "orc")
```

`user_id` accepts the same forms as every server call (`int`, GUID `str` or `uuid.UUID`; typed as
`keewano_sdk.UserId`). The server reporters go through `KeewanoServerCodegen`, which has the same
payload types and validation as the client bridge. The `server_` names are derived from the client
names, so an event name the codegen accepts works for both. Use the reporter that matches the SDK you
initialized: a `server_` reporter before `keewano_sdk.server_sdk.initialize()` is dropped with a
warning, exactly like a client reporter before `keewano_sdk.initialize()`.
