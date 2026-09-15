# Automatic tracking

The mobile Keewano SDKs hook deep into the OS and UI framework — capturing button clicks, screen
transitions, connectivity, lifecycle, and more with no code from you. A **native Python
application** has none of that platform machinery: no manifest, no UI framework the SDK can observe,
no OS lifecycle callbacks. So the Python SDK deliberately captures **very little** automatically, and
you report the rest [explicitly](api-reference.md).

This page is the honest, short list of what *is* automatic — and what you must report yourself.

- [Launch burst](#launch-burst)
- [Uncaught exceptions](#uncaught-exceptions)
- [Exit flush](#exit-flush)
- [What is not automatic](#what-is-not-automatic)

---

## Launch burst

Once, during `initialize()`, the SDK reports a device/app context preamble gathered from the standard
library (`platform`, `locale`, `os`):

| Event | Contents |
|---|---|
| `APP_LAUNCH` | The `app_version` you passed in `KeewanoConfig` (marked even if you left it blank). |
| `PLATFORM` | A coarse OS family, e.g. `"Linux"`, `"Windows"`, `"macOS"`. |
| `DEVICE_TYPE` | The machine architecture, e.g. `"x86_64"`, `"arm64"`. |
| `OS` | OS family and release, e.g. `"Linux 6.8.0"`. |
| `RAM_SIZE` | Total physical RAM in MB (omitted if it cannot be read without third-party packages). |
| `SYSTEM_LANG` | The host's primary language subtag, e.g. `"en"`. |

> Values that cannot be genuinely determined (RAM on an exotic platform, an unset locale) are
> **omitted rather than sent as zero/empty** — an empty value would otherwise read downstream as a
> real "unknown" cohort and absorb every affected session in a primary segmentation dimension. This
> is why the burst may carry fewer rows on some hosts.

Everything in the burst is best-effort and never raises; a value the standard library can't report is
simply left out.

## Uncaught exceptions

The SDK installs two exception hooks at `initialize()` so crashes are captured as `ERROR_MSG` events:

- **`sys.excepthook`** — uncaught exceptions on the main thread.
- **`threading.excepthook`** (Python 3.8+) — uncaught exceptions in worker threads.

Both **chain to any previously-installed handler**, so the SDK does not swallow your crashes — it
records the exception type, message, and **full stack trace** (truncated to 8,192 characters), then
calls the handler that was there before. On this path the event is **persisted to disk
synchronously** (not just queued), because a crashing process may die before the background sender
runs; the report is uploaded on the next launch.

A crash raised **inside the SDK itself** is deliberately **not** reported as an app error (it is
logged locally and still chained onward), so the SDK's own bugs never pollute your analytics. The
check attributes fault to the frame where the exception was raised.

`KeyboardInterrupt` (Ctrl-C), `SystemExit`, and `GeneratorExit` are **not** reported either — they
are normal termination/control-flow signals, not crashes. They are still chained to the previous
handler, so the interpreter's usual shutdown behavior is unaffected.

Turn this off with `KeewanoConfig(disable_exception_tracking=True)` — **not recommended**, since
crash context is some of the most valuable signal the analyst has. For *handled* errors you want
visibility into, call [`log_error`](api-reference.md#log_error) yourself.

## Exit flush

`initialize()` registers an `atexit` hook, so a **normal** interpreter exit flushes buffered events.
It is not an automatic *event* — just a courtesy so a clean shutdown ships what it has. Uploads are
asynchronous, so for short-lived scripts call [`flush`](api-reference.md#flush) and give the sender a
moment, or [`shutdown`](api-reference.md#shutdown) to stop at a known point. Anything not yet uploaded
stays on disk for the next run.

## What is not automatic

Unlike the mobile SDKs, the Python SDK does **not** capture any of the following — report them
yourself with the matching call:

| Not captured automatically | Report it with |
|---|---|
| Button / interactive-element clicks | [`report_button_click`](api-reference.md#report_button_click--report_window_open--report_window_close) |
| Window / screen / popup transitions | [`report_window_open` / `report_window_close`](api-reference.md#report_button_click--report_window_open--report_window_close) |
| Foreground / background, session boundaries | [`report_app_pause` / `report_app_resume`](api-reference.md#lifecycle-connectivity--environment) |
| Connectivity changes | [`report_internet_connected` / `report_internet_disconnected`](api-reference.md#lifecycle-connectivity--environment) |
| Deep links | [`report_deep_link`](api-reference.md#lifecycle-connectivity--environment) |
| Low-memory warnings | [`report_low_memory`](api-reference.md#lifecycle-connectivity--environment) |
| Scene / screen load & unload | [`report_scene_loaded` / `report_scene_unloaded`](api-reference.md#lifecycle-connectivity--environment) |
| User country | [`report_user_country`](api-reference.md#lifecycle-connectivity--environment) |
| In-game language (vs system language) | [`report_game_language`](api-reference.md#report_game_language) |

These are plain analytics events: reporting `report_app_pause`/`report_app_resume` records a session
boundary but does **not** change the SDK's behavior. There is no OS foreground/background signal, so
the background sender never enters a "paused" wait — it simply uploads whenever there is a network and
data to send.
