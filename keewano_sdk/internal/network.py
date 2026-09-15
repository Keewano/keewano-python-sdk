"""HTTP transport for batches and custom-event registration. Mirror of the Android SDK's
``KNetwork``, including the exact set of ``K-*`` headers the ingress expects.

Uses the standard-library ``urllib`` so the SDK stays dependency-free. Owns everything
URL/credential related: it derives the ingress and custom-event endpoints from ``base_endpoint``
and holds the ``app_secret`` and optional ``proxy_auth_bearer``. Every request is built through
``_common_headers``, the single place that applies the headers common to all requests (``K-SDK``,
the ``K-Token`` app secret, and - for test environments behind an authenticating proxy -
``Authorization: Bearer <token>``).
"""

from __future__ import annotations

import enum
import urllib.error
import urllib.request
from typing import Optional

from .batch import KBatch

_SOCKET_TIMEOUT_S = 30.0

_HTTP_OK = 200
_HTTP_CREATED = 201
_HTTP_NO_CONTENT = 204


class SendResult(enum.Enum):
    """The outcome of trying to upload one batch. Splitting these apart matters: "the ingress
    refused this batch" and "we could not reach the ingress at all" call for opposite reactions,
    and collapsing them into a bool made one refused batch look like an outage and stall the queue.
    """

    #: 2xx. The backend owns the data now; the local file can be deleted.
    ACCEPTED = 1
    #: A non-2xx HTTP response. Per the ingress contract the batch is *retryable* — permanently
    #: invalid batches are ACKed with 200 precisely so the client can delete them, and everything
    #: else (401/403 during a key rotation, a transient 400, a bad deploy) deliberately stays non-2xx.
    REJECTED = 2
    #: No HTTP response at all (DNS, connect, TLS, timeout). The server is presumed unreachable.
    UNREACHABLE = 3


class CustomEventLookup:
    """Result of a custom-event mapping lookup call (get_custom_event_ids)."""

    __slots__ = ("has_mapping", "need_to_register")

    def __init__(self, has_mapping: bool, need_to_register: bool) -> None:
        self.has_mapping = has_mapping
        self.need_to_register = need_to_register


class KNetwork:
    def __init__(self, base_endpoint: str, app_secret: str, proxy_auth_bearer: Optional[str], sdk_version: str) -> None:
        self._ingress_endpoint = f"{base_endpoint}/in"
        # Reserved for custom-event registration once codegen lands (see register_custom_events).
        self._ce_reg_endpoint = f"{base_endpoint}/custom"
        self._app_secret = app_secret
        self._proxy_auth_bearer = proxy_auth_bearer
        self._sdk_version = f"Python/{sdk_version}"

    def _common_headers(self) -> dict:
        headers = {
            "K-SDK": self._sdk_version,
            "K-Token": self._app_secret,
        }
        if self._proxy_auth_bearer:
            headers["Authorization"] = f"Bearer {self._proxy_auth_bearer}"
        return headers

    def send_batch(self, batch: KBatch, test_user: Optional[str]) -> SendResult:
        """Sends a single batch. The event bytes go in the body; metadata travels in headers.

        Returns :class:`SendResult`: ``ACCEPTED`` on 2xx, ``REJECTED`` on any other HTTP response,
        ``UNREACHABLE`` when no response was received at all.
        """
        headers = self._common_headers()
        headers.update(
            {
                "Content-Type": "application/octet-stream",
                "K-InstallId": str(batch.install_id),
                "K-Uid": str(batch.user_id),
                "K-DS": str(batch.data_session_id),
                "K-Batch": str(batch.batch_num),
                "K-BatchStartTime": str(batch.batch_start_time),
                "K-BatchEndTime": str(batch.batch_end_time),
                "K-BatchVersion": str(batch.batch_version),
                "K-CustomEventHash": str(batch.custom_events_version),
            }
        )
        if test_user is not None:
            headers["K-Tester"] = test_user

        # Slice through a memoryview so bounding the buffer to its used length is a view, not a copy;
        # only the final bytes() materializes (one copy, which urllib needs for a stable request body).
        body = bytes(memoryview(batch.data.raw_buffer())[: batch.data.length])
        req = urllib.request.Request(self._ingress_endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=_SOCKET_TIMEOUT_S) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        except Exception:
            return SendResult.UNREACHABLE
        return SendResult.ACCEPTED if 200 <= status <= 299 else SendResult.REJECTED

    def get_custom_event_ids(self, ce_version: int) -> CustomEventLookup:
        """Checks whether the backend already knows the custom-event mapping for ``ce_version``.

        Returns a lookup whose ``has_mapping`` is true if it does; otherwise ``need_to_register``
        is set when the backend reports 204 No Content.
        """
        headers = self._common_headers()
        headers["K-CustomEventHash"] = str(ce_version)
        req = urllib.request.Request(self._ce_reg_endpoint, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=_SOCKET_TIMEOUT_S) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        except Exception:
            return CustomEventLookup(has_mapping=False, need_to_register=False)

        if status == _HTTP_OK:
            return CustomEventLookup(has_mapping=True, need_to_register=False)
        if status == _HTTP_NO_CONTENT:
            return CustomEventLookup(has_mapping=False, need_to_register=True)
        return CustomEventLookup(has_mapping=False, need_to_register=False)

    def register_custom_events(self, version: int, event_count: int, gzip_data: bytes) -> bool:
        """Registers a custom-event definition set with the backend.

        ``gzip_data`` is the **already gzip-compressed** definitions payload (as stored in a
        :class:`~keewano_sdk.internal.custom_event_set.CustomEventSet`); it is sent verbatim with a
        ``Content-Encoding: gzip`` header.
        """
        headers = self._common_headers()
        headers.update(
            {
                "K-CustomEventHash": str(version),
                "K-CustomEventCount": str(event_count),
                "Content-Encoding": "gzip",
                "Content-Type": "application/octet-stream",
            }
        )
        req = urllib.request.Request(self._ce_reg_endpoint, data=gzip_data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=_SOCKET_TIMEOUT_S) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        except Exception:
            return False
        return status in (_HTTP_OK, _HTTP_CREATED)
