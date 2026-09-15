# Getting started

This guide takes you from an empty project to verified events flowing into Keewano. It should take
about five minutes.

- [1. Install the package](#1-install-the-package)
- [2. Initialize with your API key](#2-initialize-with-your-api-key)
- [3. Run and verify](#3-run-and-verify)
- [4. Report your first event](#4-report-your-first-event)
- [Flushing before exit](#flushing-before-exit)
- [Try the sample app](#try-the-sample-app)
- [Troubleshooting setup](#troubleshooting-setup)

Prerequisites: a Keewano project **API key** (a JWT issued in the Keewano console) and **Python
3.8+**. The SDK has **no third-party runtime dependencies**.

---

## 1. Install the package

```bash
pip install keewano-sdk
```

That is the only dependency change required — the SDK uses the standard library only.

Working from this monorepo instead of a published wheel? See
[Building from source](#building-from-source) at the end of this page.

## 2. Initialize with your API key

Unlike the mobile SDKs, there is **no auto-initialization** — a native Python application has no
manifest or process-launch hook to read a key from, so you call `initialize()` yourself, once, at
startup:

```python
import keewano_sdk
from keewano_sdk import KeewanoConfig

keewano_sdk.initialize(KeewanoConfig(
    api_key="YOUR_KEEWANO_API_KEY",
    app_version="1.0.0",   # there is no manifest to read this from — supply it here
))
```

That is the whole setup. `initialize()` mints an anonymous install ID on first run, reports a launch
burst (platform, OS, device, RAM, language), and starts a background upload thread. It is
**idempotent** (later calls are ignored), safe to call from any thread, and **never raises** — a
misconfiguration disables the SDK and logs, rather than propagating into your app. A **blank API
key** leaves the SDK inert.

> The full list of `KeewanoConfig` options — consent gating, data directory, endpoint override, test
> mode — is in the [Configuration reference](configuration.md).

## 3. Run and verify

Run your app. A healthy startup is **quiet**: the SDK logs only warnings and errors through the
standard `logging` module, so no output means it started cleanly.

A quick programmatic health check is `get_install_id()` — it returns the install GUID once the SDK
is running, or `None` if initialization was skipped or failed:

```python
print(keewano_sdk.get_install_id())   # a GUID string, or None if the SDK did not start
```

To confirm positively that events flow, report one yourself:

```python
keewano_sdk.report_button_click("Play")
```

Events are batched and uploaded on a background thread; they are also **persisted to disk first**, so
they survive process exit and offline periods and upload when a network is available. You will see
the data appear in your Keewano console rather than in your logs — the SDK does not log event
contents.

If you instead see a warning like `SDK not initialized; event dropped` or a message about a blank
key, jump to [Troubleshooting setup](#troubleshooting-setup).

## 4. Report your first event

Every reporting call is available both as a module-level function and as a method on the `KeewanoSDK`
facade (they are the same callables). Each is safe to call from any thread and **never throws** —
invalid arguments are dropped with a logged warning rather than raised. A tiny example:

```python
import keewano_sdk

# Associate your own user id with this install (assign once, after the user logs in).
keewano_sdk.set_user_id(1234567890)

# A progression milestone.
keewano_sdk.report_onboarding_milestone("finished_tutorial")
```

From here, the [API reference](api-reference.md) documents every reporting method — monetization,
virtual economy, progression, A/B tests, and more — with its validation rules and examples.

For **game- or app-specific events** beyond the built-in set, use the
[codegen](https://github.com/Keewano/keewano-codegen): you declare your events once and get typed
`report_enemy_killed("orc")`-style functions generated into your project. See
[Custom events](custom-events.md) for the workflow, and the
[codegen README](https://github.com/Keewano/keewano-codegen#readme) for the tool itself.

## Flushing before exit

The SDK registers an `atexit` hook, so a **normal** interpreter exit flushes buffered events. Uploads
are asynchronous, though, so for a short-lived script give the sender a moment to ship what it has, or
stop it explicitly:

```python
keewano_sdk.flush()      # request an immediate upload (best-effort, asynchronous)
keewano_sdk.shutdown()   # flush and stop the background sender at a known point
```

Anything not yet uploaded stays on disk and is sent on the next run — nothing is lost either way.

---

## Try the sample app

The repository ships a runnable `sample/main.py` that exercises the public API end to end. It reads
its configuration from environment variables, so you can point it at a staging ingress without
editing code:

```bash
KEEWANO_API_KEY=your-key PYTHONPATH=. python sample/main.py
```

Recognized variables include `KEEWANO_API_KEY`, `KEEWANO_ENDPOINT`, `KEEWANO_REQUIRE_USER_CONSENT`,
`KEEWANO_DISABLE_EXCEPTION_TRACKING`, `KEEWANO_PROXY_AUTH_BEARER`, and `KEEWANO_SET_USER_ID` — see the
header of [`sample/main.py`](../sample/main.py) for the full list.

## Troubleshooting setup

| Symptom | Likely cause | Fix |
|---|---|---|
| A log line about a **blank/empty API key** and no data | `api_key` is empty, `None`, or whitespace. | Pass a non-blank key to `KeewanoConfig`. |
| `SDK not initialized; event dropped` on every call | You are reporting before `initialize()` ran, or init was skipped (blank key / caught error). | Call `initialize()` once at startup; calls made before it are safe but dropped. |
| `get_install_id()` returns `None` | The SDK did not start (blank key, or a failure disabled it — check the logs). | Fix the configuration surfaced in the logs. |
| The key is set but nothing arrives | The endpoint was overridden to something unreachable. | Leave `endpoint` unset in production; see [Endpoint override](configuration.md#endpoint-override). |

A failure inside initialization **disables the SDK and logs** rather than propagating — an analytics
SDK must never be able to crash its host — so a misconfiguration shows up as no data, never as an app
crash.

To see the SDK's diagnostics, make sure Python logging is configured (e.g. `logging.basicConfig(
level=logging.WARNING)`); the SDK logs under the `keewano_sdk` logger hierarchy.

---

## Building from source

To run against the source in this monorepo instead of a published wheel, put the package directory on
your path:

```bash
PYTHONPATH=/path/to/python-sdk python your_app.py
```

Or install it in editable mode so imports resolve everywhere in the environment:

```bash
pip install -e /path/to/python-sdk
```

Run the wire-format regression tests with:

```bash
python -m unittest discover -s tests
```
