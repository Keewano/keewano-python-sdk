# Server-side SDK

`keewano_sdk.server_sdk` reports events from a **backend** on behalf of the many end users it serves — a
game server, a purchase-validation webhook, an economy service, a Celery task. Each event names the
user it belongs to; the SDK keeps every user's events apart, aggregates them, and uploads them in the
background. It writes the same `.kwub` batches as every Keewano SDK.

Use the client API (`keewano_sdk.initialize`, `keewano_sdk.report_*`) for an application that runs on
one user's machine. Use `keewano_sdk.server_sdk` when one process acts for many users.

- [Quick start](#quick-start)
- [Identifying users](#identifying-users)
- [What you can report](#what-you-can-report)
- [How events are batched](#how-events-are-batched)
- [Storage: persistent or not](#storage-persistent-or-not)
- [Memory limits](#memory-limits)
- [Configuration](#configuration)
- [Multi-process servers: gunicorn, Celery, multiprocessing](#multi-process-servers-gunicorn-celery-multiprocessing)
- [Shutting down](#shutting-down)
- [Custom events](#custom-events)
- [How it differs from the client SDK](#how-it-differs-from-the-client-sdk)

---

## Quick start

```python
from keewano_sdk import server_sdk as keewano
from keewano_sdk import Item, KeewanoServerConfig

keewano.initialize(KeewanoServerConfig(api_key="YOUR_KEEWANO_API_KEY"))

# In a request handler — the first argument is always the end user:
keewano.report_in_app_purchase(user_id, "gems_100", price_usd_cents=499)
keewano.report_in_app_purchase_items_granted(user_id, "gems_100", [Item("gems", 100)])
```

`initialize` is idempotent and **never raises**: a blank API key or any failure leaves the SDK inert
and logs why (`keewano.is_initialized()` tells you). Every reporting call is thread-safe, does no I/O
on your thread, and is dropped with a warning before `initialize`. Invalid arguments are logged and
the event is dropped — exactly the validation rules of the [client API](api-reference.md#validation--drop-rules).

The same functions are available as `KeewanoServerSDK.report_...` if you prefer a namespace.

## Identifying users

`user_id` accepts:

| Form | Example |
|---|---|
| a positive 64-bit `int` | `1234567890` |
| a GUID/UUID `str` | `"11111111-1111-4111-8111-111111111111"` |
| a `uuid.UUID` | `uuid.UUID("11111111-...")` |

The mapping is the same as the client's `set_user_id`, so a user reported both from your game client
and from your server is one user. `0`, negative numbers, `bool`, malformed strings and the all-zero
GUID are rejected.

For the server SDK the batch's **install id is the user id** — there is no device install.

## What you can report

The event families that make sense server-side (the same set as the Node.js relay):

| Function | Notes |
|---|---|
| `report_in_app_purchase(user_id, product, price_usd_cents=… \| localized_price=…, currency_code=…)` | Only after your server validated the receipt. |
| `report_in_app_purchase_items_granted(user_id, product, items)` | |
| `report_ad_offered(user_id, placement, ad_type)` / `report_ad_revenue(...)` / `report_ad_items_granted(...)` | |
| `report_subscription_revenue(...)` / `report_subscription_items_granted(...)` | Each billing event. |
| `report_items_exchange(user_id, location, from_items, to_items)` / `report_items_reset(...)` | |
| `report_install_campaign(user_id, campaign)` / `report_game_language(user_id, lang)` | |
| `report_onboarding_milestone(user_id, milestone)` | Sent exactly as given; repeats are not numbered. |
| `report_ab_test_group_assignment(user_id, test, group)` | `group` is one character, code point 0–255. |
| `log_error(user_id, message)` | Up to 8 KiB, truncated beyond. |
| `flush(user_id=None)` | Ship a user's (or everyone's) buffered events now. |

UI, lifecycle, consent and device events are not part of the server surface: they describe one
device's session, which a server does not have. The server SDK has no consent gate — it ships only
what you hand it, so gate on consent in your own code where it applies.

## How events are batched

Every user the process is serving has their own in-memory batch. Events are **aggregated per user**,
and a user's batch is sealed for upload when the first of these happens:

1. it has been collecting for `flush_interval` seconds (60 s persistent / 15 s ephemeral by default);
2. it reaches 50 KiB;
3. the SDK needs the memory back (see [Memory limits](#memory-limits));
4. the user is forgotten (idle or pushed out of the LRU), or you call `flush()` / `shutdown()`.

So a user who does five things in a minute produces one batch, not five. Call `flush(user_id)` at
the end of a request if you prefer latency over aggregation for that user.

Each user also gets a **data session**: the first time the process sees a user it allocates a new
data-session id, and the user's batches are numbered 0, 1, 2, … within it. The state is kept while
the user is active and forgotten after `user_idle_timeout` (30 min) without events, or when the
user is the least recently active one and `max_users` is reached. A forgotten user who comes back
simply starts a new data session at batch 0 — just like a client app that was relaunched.

Uploads run on one background thread over a single kept-alive HTTPS connection, so batches after
the first skip the TCP and TLS handshake. A user's batches are always sent in order. A batch the
ingress refuses is retried (every 30 s, up to 10 times, then replaced by a drop marker) without
holding back other users; when the ingress is unreachable, all uploads back off for 30 s and nothing
is lost until max_pending_bytes is reached (see Memory limits). HTTP(S) proxy environment variables
are not used.

## Storage: persistent or not

Choose with `persistent_storage`:

**`persistent_storage=False` (default) — storage does not survive a restart.** A Kubernetes pod,
a container's writable layer, a read-only root filesystem, a serverless instance. Nothing is written
to disk: sealed batches wait for upload in RAM, the flush interval is shorter (15 s) so less is at
risk, and on shutdown the SDK spends up to `shutdown_timeout` (10 s) uploading everything it holds.
Whatever does not make it is lost (and logged). A hard kill (`SIGKILL`, OOM kill) loses what was in
memory at that moment — at most about one flush interval of events plus anything queued while the
ingress was unreachable.

**`persistent_storage=True` — storage survives a restart.** A VM, a bare-metal host, a
PersistentVolume. Sealed batches are written to `data_dir` before upload, so a crash or a restart
loses only what was still being aggregated in RAM; the next run uploads the rest. Shutdown persists
everything to disk first, then spends up to `shutdown_timeout` (2 s) uploading.

```python
keewano.initialize(KeewanoServerConfig(
    api_key="…",
    persistent_storage=True,
    data_dir="/var/lib/my-service/keewano",
))
```

Several processes may share one `data_dir`: each leases its own `data_dir/server/worker-N`
sub-directory with an OS file lock, which the kernel releases when the process dies. Leftover batches
in a directory whose lease nobody holds (a crashed worker, or after scaling down) are taken over by a
live process and uploaded. If `data_dir` turns out to be unusable, the SDK logs it and falls back to RAM.

The background threads start on the first event or `flush()`, not in `initialize()`. To upload a
previous run's leftovers immediately at startup, call `keewano.flush()` right after `initialize()`.

## Memory limits

The SDK never grows without bound:

| Limit | Default | What happens past it |
|---|---|---|
| `max_users` | 10 000 | The least recently active user's batch is sealed and the user forgotten. |
| `max_buffered_bytes` | 16 MiB | The oldest aggregating batches are sealed early. No data is dropped. |
| `max_pending_bytes` | 16 MiB ephemeral (RAM) / 50 MiB persistent (disk) | The oldest unsent batches' events are dropped and replaced by a small `BATCH_DROPPED` marker, so the backend knows a gap exists. |

In ephemeral mode the worst-case RAM for event data is therefore `max_buffered_bytes +
max_pending_bytes` (32 MiB by default) plus roughly a few hundred bytes per tracked user. The pending
cap is only reached while uploads cannot keep up — typically an ingress outage.

## Configuration

```python
@dataclass
class KeewanoServerConfig:
    api_key: str                                   # required
    persistent_storage: bool = False
    data_dir: Optional[str] = None                 # persistent only; default: per-user app-data dir
    flush_interval: Optional[float] = None         # s; None => 60 persistent / 15 ephemeral
    user_idle_timeout: float = 1800.0              # s
    max_users: int = 10_000
    max_buffered_bytes: int = 16 MiB
    max_pending_bytes: Optional[int] = None        # None => 50 MiB persistent / 16 MiB ephemeral
    shutdown_timeout: Optional[float] = None       # s; None => 2 persistent / 10 ephemeral
    endpoint: str = DEFAULT_ENDPOINT               # integration testing only
    proxy_auth_bearer: Optional[str] = None        # integration testing only
    custom_event_set: Optional[CustomEventSet] = None
    test_user_name: Optional[str] = None           # tag all data from this process as test data
```

An invalid number (zero, negative, NaN, a fraction where a count is expected) is logged and replaced
by its default. Set `test_user_name` on staging servers so their data is excluded from production
analytics.

## Multi-process servers: gunicorn, Celery, multiprocessing

The SDK detects `fork()` (via `os.register_at_fork`). A forked child starts with an **empty** engine
of its own — fresh locks, no users, no queue, a work directory of its own, no inherited network
connection — and starts it on its first event. The parent keeps, and alone uploads, whatever it had collected before the fork, so nothing is sent twice.
Because the SDK's threads only start on the first event, a parent that forks before reporting
anything (the usual pattern) forks without any SDK threads running.

**gunicorn.** Initializing at import time works with and without `--preload`. Workers stopped
gracefully (`SIGTERM`, `SIGHUP`, max-requests recycling) run `atexit`, which flushes. If you report
events in the master process itself (e.g. from `on_starting`), prefer initializing in a
`post_fork` hook instead, so the master holds no in-flight uploads when it forks.

**Celery (prefork pool).** Pool processes exit with `os._exit()`, which skips `atexit`. Flush from
Celery's signal instead:

```python
from celery.signals import worker_process_shutdown
from keewano_sdk import server_sdk as keewano

@worker_process_shutdown.connect
def _flush_keewano(**_):
    keewano.shutdown()
```

**multiprocessing.** Fork-started children exit with `os._exit()` as well; call
`keewano.shutdown()` at the end of the child's work (e.g. in a `try/finally` around the target).

**Bare scripts in containers.** Python's default `SIGTERM` action kills the process without running
`atexit`. Web servers (gunicorn, uvicorn) handle `SIGTERM` for you; a plain script should install
`signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))` so the shutdown flush runs.

## Shutting down

`shutdown(timeout=None)` seals every user's events, persists them (persistent mode), spends up to
`timeout` seconds (default `shutdown_timeout`) uploading what is queued — each batch gets one attempt,
so an unreachable ingress does not hold up the exit — and stops. It is registered with `atexit`
automatically. Keep `shutdown_timeout` below your orchestrator's grace period (Kubernetes:
`terminationGracePeriodSeconds`, 30 s by default). Events reported after `shutdown` are dropped.

## Custom events

Pass the codegen's `CUSTOM_EVENT_SET` as `custom_event_set`. The generated module has a server
reporter for every event — `server_report_<name>(user_id, ...)`, next to the client
`report_<name>(...)` — so a `BestScore` event is reported as:

```python
from myapp import keewano_custom_events
keewano_custom_events.server_report_best_score(user_id, best_score)
```

See [Custom events](custom-events.md#server-side-sdk).

## How it differs from the client SDK

|  | Client (`keewano_sdk`) | Server (`keewano_sdk.server_sdk`) |
|---|---|---|
| Model | one install, one user | one process, many users |
| Identity | durable install id + `set_user_id` | `user_id` on every call; install id = user id |
| Data session | one per launch | one per user, while the user is tracked |
| Batching | one stream for the install | aggregated per user |
| Automatic events | launch burst, uncaught exceptions | none |
| Consent gate | optional | none |
| Storage | always on disk | RAM or disk (`persistent_storage`) |
| Processes | one | any number; fork-aware, shared `data_dir` is safe |
