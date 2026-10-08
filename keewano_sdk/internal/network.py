"""HTTP transport for batches and custom-event registration. Mirror of the Android SDK's
``KNetwork``, including the exact set of ``K-*`` headers the ingress expects.

Built on the standard library's ``http.client`` so the SDK stays dependency-free, and — unlike
``urllib``, which closes the connection after every request — it keeps one HTTP/1.1 connection open
and reuses it. Without that, every batch pays a fresh TCP connect and TLS handshake, which dominates
the cost of an upload (a server reporting for thousands of users sends many small batches).

Reuse rules:

 * One connection per ``KNetwork``, used by one request at a time (``_lock``); each SDK uploads from
   a single thread, so this is never a point of contention.
 * Every response is read to the end before the next request, which is what makes reuse possible.
 * A connection idle for more than ``_IDLE_CLOSE_S`` is closed before use rather than reused: load
   balancers silently drop idle keep-alive connections, and sending into one wastes a round trip.
 * If a *reused* connection turns out to have been closed by the server (reset, broken pipe, remote
   disconnect, TLS EOF), the request is retried once on a fresh connection — the standard client
   behaviour, since the request almost certainly never reached the server. A failure on a fresh
   connection means the ingress is unreachable.
 * After ``fork()`` a child drops the connections it inherited without using or shutting them down:
   the socket is shared with the parent, which may be mid-request on it.

Redirects are not followed (a 3xx is a non-2xx answer, i.e. ``REJECTED``), and HTTP(S) proxy
environment variables are not honoured.

Owns everything URL/credential related: it derives the ingress and custom-event endpoints from
``base_endpoint`` and holds the ``app_secret`` and optional ``proxy_auth_bearer``. Every request is
built through ``_common_headers``, the single place that applies the headers common to all requests
(``K-SDK``, the ``K-Token`` app secret, and - for test environments behind an authenticating proxy -
``Authorization: Bearer <token>``).
"""

from __future__ import annotations

import enum
import http.client
import logging
import os
import ssl
import threading
import time
import urllib.parse
import weakref
from typing import Optional, Tuple

from .batch import KBatch

_log = logging.getLogger("keewano_sdk")

_SOCKET_TIMEOUT_S = 30.0
#: A kept-alive connection idle for longer than this is closed and replaced rather than reused.
#: Below the idle timeout of common load balancers (AWS ALB 60 s, GCP 600 s, nginx 75 s).
_IDLE_CLOSE_S = 30.0

#: Errors that mean "the server closed this kept-alive connection under us". Only retried when the
#: connection was reused; RemoteDisconnected is a ConnectionResetError (a ConnectionError).
_STALE_CONNECTION_ERRORS = (ConnectionError, ssl.SSLEOFError, ssl.SSLZeroReturnError)

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
        url = urllib.parse.urlsplit(base_endpoint)
        self._scheme = url.scheme.lower()
        self._host = url.hostname or ""
        try:
            self._port = url.port
        except ValueError:  # e.g. a non-numeric port: treated like any other unusable endpoint
            self._port, self._host = None, ""
        base_path = url.path.rstrip("/")
        self._ingress_path = f"{base_path}/in"
        self._ce_reg_path = f"{base_path}/custom"
        if self._scheme not in ("http", "https") or not self._host:
            _log.error("Invalid Keewano endpoint %r; nothing can be uploaded.", base_endpoint)
        self._app_secret = app_secret
        self._proxy_auth_bearer = proxy_auth_bearer
        self._sdk_version = f"Python/{sdk_version}"

        self._lock = threading.Lock()
        self._conn: Optional[http.client.HTTPConnection] = None
        self._last_used = 0.0
        self._ssl_context: Optional[ssl.SSLContext] = None
        _instances.add(self)

    def _common_headers(self) -> dict:
        headers = {
            "K-SDK": self._sdk_version,
            "K-Token": self._app_secret,
        }
        if self._proxy_auth_bearer:
            headers["Authorization"] = f"Bearer {self._proxy_auth_bearer}"
        return headers

    # --- Connection management ------------------------------------------------------------------

    def _connection(self) -> Tuple[http.client.HTTPConnection, bool]:
        """The connection to use and whether it is a reused one. Caller holds ``_lock``."""
        if self._conn is not None and time.monotonic() - self._last_used > _IDLE_CLOSE_S:
            self._close_locked()
        if self._conn is not None:
            return self._conn, True
        if self._scheme == "https":
            if self._ssl_context is None:
                self._ssl_context = ssl.create_default_context()  # same verification as urllib
            self._conn = http.client.HTTPSConnection(
                self._host, self._port, timeout=_SOCKET_TIMEOUT_S, context=self._ssl_context
            )
        else:
            self._conn = http.client.HTTPConnection(self._host, self._port, timeout=_SOCKET_TIMEOUT_S)
        return self._conn, False

    def _close_locked(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def close(self) -> None:
        """Closes the kept-alive connection, if any. Safe to call at any time; the next request
        simply opens a new one."""
        with self._lock:
            self._close_locked()

    def _after_fork_in_child(self) -> None:
        # The lock may have been copied mid-hold by a parent thread that does not exist here, and the
        # socket is shared with the parent: close only the child's descriptor (socket.close() never
        # sends a FIN or TLS close_notify while another process still holds the socket) and forget it.
        self._lock = threading.Lock()
        conn, self._conn = self._conn, None
        sock = getattr(conn, "sock", None) if conn is not None else None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _request(self, method: str, path: str, body: Optional[bytes], headers: dict) -> Optional[int]:
        """Performs one request on the kept-alive connection. Returns the HTTP status, or None when no
        response was received at all (the ingress is unreachable)."""
        if self._scheme not in ("http", "https") or not self._host:
            return None
        with self._lock:
            for _ in range(2):
                conn, reused = self._connection()
                try:
                    conn.request(method, path, body=body, headers=headers)
                    resp = conn.getresponse()
                    resp.read()  # drain: the connection can only be reused once the body is consumed
                    status = resp.status
                    if resp.will_close:
                        self._close_locked()
                    else:
                        self._last_used = time.monotonic()
                    return status
                except _STALE_CONNECTION_ERRORS:
                    self._close_locked()
                    if not reused:
                        return None
                    # The server dropped the idle connection; retry once on a fresh one.
                except Exception:
                    self._close_locked()
                    return None
            return None

    # --- Requests -------------------------------------------------------------------------------

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
        # only the final bytes() materializes (one copy, so the body cannot change while it is sent).
        body = bytes(memoryview(batch.data.raw_buffer())[: batch.data.length])
        status = self._request("POST", self._ingress_path, body, headers)
        if status is None:
            return SendResult.UNREACHABLE
        return SendResult.ACCEPTED if 200 <= status <= 299 else SendResult.REJECTED

    def get_custom_event_ids(self, ce_version: int) -> CustomEventLookup:
        """Checks whether the backend already knows the custom-event mapping for ``ce_version``.

        Returns a lookup whose ``has_mapping`` is true if it does; otherwise ``need_to_register``
        is set when the backend reports 204 No Content.
        """
        headers = self._common_headers()
        headers["K-CustomEventHash"] = str(ce_version)
        status = self._request("GET", self._ce_reg_path, None, headers)
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
        status = self._request("POST", self._ce_reg_path, gzip_data, headers)
        return status in (_HTTP_OK, _HTTP_CREATED)


#: Every live transport, so a forked child can drop the connections it inherited.
_instances: "weakref.WeakSet[KNetwork]" = weakref.WeakSet()


def _after_fork_in_child() -> None:
    for net in list(_instances):
        net._after_fork_in_child()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)
