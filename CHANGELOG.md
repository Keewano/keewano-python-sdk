# Changelog

All notable changes to the Keewano Python SDK (`keewano-sdk`). The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [2.0.0] - 2026-10-07

The headline is the **server-side SDK**: one backend process reporting on behalf of many end users.
The major version is for two client-visible breaking changes in the HTTP transport and in
custom-event codegen compatibility (see *Breaking changes*).

### Breaking changes

- **HTTP proxy environment variables are no longer used.** Uploads now go through `http.client`
  instead of `urllib`, so `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` are ignored and the SDK connects
  to the endpoint directly. An app that relied on them to reach the ingress will need a direct route.
- **Custom-events files from the new codegen require SDK 2.0.0.** A file generated with server support
  imports `KeewanoServerCodegen` and `UserId`, which 1.x does not have, so it fails to import there.
  Upgrade the SDK before (or together with) regenerating.

### Added

- **Server-side SDK, `keewano_sdk.server_sdk`**, for backends (game servers, purchase-validation
  webhooks, Celery tasks) that report for many end users from one process. Every reporting call takes
  the end user's id first, e.g. `server_sdk.report_in_app_purchase(user_id, "gems_100",
  price_usd_cents=499)`. See [docs/server-side.md](docs/server-side.md).
  - Purchases, ads, subscriptions, item economy, install campaign, game language, onboarding
    milestones, A/B test assignment, `log_error` and custom events, each with the same validation as
    the client API.
  - `user_id` accepts a positive 64-bit `int`, a GUID `str` or a `uuid.UUID` (typed as
    `keewano_sdk.UserId`), mapped exactly like the client's `set_user_id`.
  - Each batch belongs to one user and carries the user id as its install id. Every user gets their
    own data session, with batch numbers starting at 0, kept while the user is active.
  - Events are aggregated per user and sealed by age (`flush_interval`), size (50 KiB), memory
    pressure, eviction, `flush(user_id)` or shutdown, so an active user produces a few full batches
    rather than one batch per event.
  - Bounded memory: `max_users` (least recently active users are evicted), `max_buffered_bytes`
    (oldest batches sealed early) and `max_pending_bytes` (oldest unsent batches replaced by a
    `BATCH_DROPPED` marker).
  - `persistent_storage`: keep unsent batches on disk across restarts, or (the default) in RAM only,
    for containers and read-only filesystems. Falls back to RAM if the data directory is unusable.
  - Several processes can share one `data_dir`: each leases its own `server/worker-N` directory, and
    batches left by a dead process are taken over and uploaded.
  - Multi-process servers: fork-safe for gunicorn (with or without `--preload`), Celery and
    `multiprocessing` — a forked child starts with an empty engine and its own connection, and the
    parent alone uploads what it had collected.
  - Shutdown (also run at exit) seals everything and makes a bounded attempt to upload it
    (`shutdown_timeout`).
  - A refused batch only holds back its own user; reporting calls never wait on disk or network I/O.
- `KeewanoServerConfig`, `KeewanoServerSDK`, `KeewanoServerCodegen` and `UserId`, exported from
  `keewano_sdk`.
- **Server-side custom events.** The codegen's Python output now has a
  `server_report_<name>(user_id, ...)` function next to every `report_<name>(...)`, backed by
  `KeewanoServerCodegen`. Contract: [docs-internal/custom-events.md](docs-internal/custom-events.md).
- `sample/server_main.py`: a runnable server-side example.

### Changed

- **Uploads reuse one kept-alive HTTPS connection** instead of opening a new connection (TCP + TLS
  handshake) for every batch. A connection idle for more than 30 s is replaced before use, and a
  reused connection the server had closed is retried once on a fresh one. Certificate verification is
  unchanged.
- After `fork()`, a child process drops the network connection it inherited, so it never interferes
  with the parent's connection (client and server SDK).
- An unusable endpoint URL (unknown scheme, invalid port) is now reported with an error at startup;
  previously every upload attempt failed silently.
- Requests identify the SDK as `K-SDK: Python/2.0.0`.

### Fixed

- **A tester name that cannot be sent as an HTTP header no longer stalls all uploads.**
  `mark_as_test_user` (and the server's `KeewanoServerConfig.test_user_name`) now rejects, with an
  error log, names outside Latin-1 or containing control characters (other than a tab), such as a line
  break. Previously such a name made every upload fail as if the ingress were unreachable, so batches
  piled up until the storage cap dropped them.

### Internal

- The byte layout of every event lives in one module (`internal/encoding.py`) shared by the client
  and server engines, which are checked byte-for-byte against each other in the tests.
- The package version has a single source, `keewano_sdk.__version__`, which `pyproject.toml` reads at
  build time.

## [1.0.0] - 2026-09-15

Initial release of the Keewano native Python SDK: a dependency-free client SDK for native Python
applications, writing the same `.kwub` batch format as every Keewano SDK.

- Single-call `initialize()` with a launch burst (platform, OS, device, RAM, language) and automatic
  reporting of uncaught exceptions on the main and worker threads.
- Monetization (in-app purchases, ad revenue, subscriptions, granted items), virtual economy,
  onboarding milestones, A/B tests, UI, lifecycle and connectivity events.
- Custom events through the Keewano codegen (`KeewanoCodegen`, `CustomEventSet`).
- GDPR/CCPA consent gating with durable, offline-first buffering (50 MB disk cap) and a background
  upload thread.
