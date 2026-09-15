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
