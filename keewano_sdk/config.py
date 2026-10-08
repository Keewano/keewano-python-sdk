"""SDK configuration. Analogue of the Android SDK's ``KeewanoConfig``.

Pass an instance to :func:`keewano_sdk.initialize`. Only ``api_key`` is required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .internal.custom_event_set import CustomEventSet

#: Production Keewano ingress base URL.
DEFAULT_ENDPOINT = "https://api.keewano.com/event/ingress/v1/data"


@dataclass
class KeewanoConfig:
    """Configuration for the native Python SDK.

    :param api_key: The Keewano project API key (JWT). Required.
    :param require_user_consent: If true, data is buffered locally but not sent until
        :func:`keewano_sdk.set_user_consent` is called (GDPR/CCPA). Defaults to False.
    :param app_version: Your application's version string, reported with the launch burst.
    :param data_dir: Directory for durable identifiers, consent, and pending batches. Defaults
        to a per-user application-data directory (see :func:`keewano_sdk.internal.paths`).
    :param disable_exception_tracking: If true, the SDK will not install a hook to auto-report
        uncaught exceptions. NOT recommended - crash context is valuable to the AI analyst.
    :param endpoint: The Keewano ingress base URL. Override only for integration testing.
    :param proxy_auth_bearer: Optional bearer token sent as ``Authorization: Bearer <value>`` on
        every request. Intended for test environments where the ingress sits behind an
        authenticating proxy. Leave None in production.
    :param custom_event_set: The codegen-produced custom-event definitions (None for none). See
        ``docs/custom-events.md``.
    """

    api_key: str
    require_user_consent: bool = False
    app_version: str = ""
    data_dir: Optional[str] = None
    disable_exception_tracking: bool = False
    endpoint: str = DEFAULT_ENDPOINT
    proxy_auth_bearer: Optional[str] = None
    #: The custom-event definition set produced by the Keewano codegen (see docs/custom-events.md).
    #: The generated file, which lives in your app, builds it with
    #: :meth:`CustomEventSet.from_gzip_base64` and passes it here. None → no custom events.
    custom_event_set: Optional[CustomEventSet] = None


@dataclass
class KeewanoServerConfig:
    """Configuration for the server-side SDK (:mod:`keewano_sdk.server_sdk`): one process reporting
    events on behalf of many end users. See ``docs/server-side.md``.

    :param api_key: The Keewano project API key (JWT). Required.
    :param persistent_storage: True if ``data_dir`` survives a restart of this process (a VM, a
        bare-metal host, a Kubernetes PersistentVolume). Unsent batches are then written to disk and
        uploaded by the next run. False (the default) for storage that does not survive (a pod's
        filesystem, a read-only root): batches stay in RAM, and shutdown makes a bounded attempt to
        upload everything before the process exits.
    :param data_dir: Where batches are kept when ``persistent_storage`` is True. Several processes may
        share it (each leases its own locked sub-directory). Defaults to a per-user application-data
        directory. Ignored when ``persistent_storage`` is False.
    :param flush_interval: Longest time (seconds) an event waits in a user's batch before the batch is
        sealed for upload. Longer means fewer, fuller batches; shorter means less data at risk if the
        process is killed. Defaults to 60 s with persistent storage and 15 s without.
    :param user_idle_timeout: Seconds without events after which a user is forgotten; their next
        event starts a new data session.
    :param max_users: Most users tracked at once. Beyond it the least recently active user's batch is
        sealed and the user forgotten.
    :param max_buffered_bytes: RAM budget for events still being aggregated per user. Beyond it the
        oldest batches are sealed early (no data is dropped).
    :param max_pending_bytes: Budget for sealed batches waiting for upload (on disk when persistent,
        in RAM otherwise). Beyond it the oldest batches' events are dropped and replaced by a marker.
        Defaults to 50 MiB with persistent storage and 16 MiB without.
    :param shutdown_timeout: Longest time :func:`keewano_sdk.server_sdk.shutdown` spends uploading what is
        queued. Defaults to 2 s with persistent storage (the rest stays on disk) and 10 s without (the
        rest is lost) — keep it below your orchestrator's termination grace period.
    :param endpoint: The Keewano ingress base URL. Override only for integration testing.
    :param proxy_auth_bearer: Optional bearer token for a test ingress behind an authenticating proxy.
    :param custom_event_set: The codegen-produced custom-event definitions (None for none).
    :param test_user_name: Tags every batch from this process as test data (e.g. a staging server).
    """

    api_key: str
    persistent_storage: bool = False
    data_dir: Optional[str] = None
    flush_interval: Optional[float] = None
    user_idle_timeout: float = 30 * 60.0
    max_users: int = 10_000
    max_buffered_bytes: int = 16 * 1024 * 1024
    max_pending_bytes: Optional[int] = None
    shutdown_timeout: Optional[float] = None
    endpoint: str = DEFAULT_ENDPOINT
    proxy_auth_bearer: Optional[str] = None
    custom_event_set: Optional[CustomEventSet] = None
    test_user_name: Optional[str] = None
