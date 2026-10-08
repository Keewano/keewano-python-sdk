"""Test-only helpers: a localhost ingress mock and a polling wait, shared by the tests that
exercise the network transport, dispatcher send loop, and public facade.

Not a test module (name does not match ``test*.py``), so unittest discovery ignores it.
"""

import http.server
import ssl
import threading
import time

try:  # dev dependency (requirements-dev.txt); TLS tests skip without it
    import trustme
except ImportError:  # pragma: no cover - only without the dev requirements
    trustme = None

#: True when ``MockIngress(tls=True)`` is available.
TLS_AVAILABLE = trustme is not None
# A throwaway CA minted in memory per test run: no certificate or private key is ever committed
# (the repo's secret scanning rejects key files). Created lazily, so importing this module is free.
_tls_ca = None
# Captured at import so trusting_context() still works while a test patches ssl.create_default_context.
_create_default_context = ssl.create_default_context


def _ca():
    global _tls_ca
    if _tls_ca is None:
        _tls_ca = trustme.CA()
    return _tls_ca


class _Handler(http.server.BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.connections += 1

    def do_GET(self):
        self.server.get_paths.append(self.path)
        self._reply(self.server.ce_get_status)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        hdrs = {k.lower(): v for k, v in self.headers.items()}
        if self.path.endswith("/custom"):
            self.server.custom_posts.append((hdrs, body))
            self._reply(self.server.custom_post_status)
        else:
            self.server.batch_posts.append((hdrs, body))
            self._reply(self.server.batch_post_status)

    def _reply(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")  # lets an HTTP/1.1 client reuse the connection
        self.end_headers()
        if self.server.drop_after_reply:
            # Close without announcing it (no "Connection: close"), like a load balancer dropping an
            # idle keep-alive connection: the client only finds out when it tries to reuse it.
            self.close_connection = True

    def log_message(self, *args):
        pass  # keep test output clean


class MockIngress:
    """A minimal in-process HTTP server standing in for the Keewano ingress.

    Records requests and lets tests set response codes for batch uploads (``/in``),
    custom-event lookups (GET ``/custom``), and custom-event registration (POST ``/custom``).

    ``keep_alive`` serves HTTP/1.1 persistent connections (the default HTTP/1.0 closes after every
    response); ``drop_after_reply`` silently closes every connection after its first response.
    ``connections`` counts the TCP connections accepted. ``tls`` serves HTTPS with a certificate for
    127.0.0.1 from a throwaway test CA (clients must trust it explicitly, see :func:`trusting_context`).
    """

    def __init__(self, keep_alive=False, drop_after_reply=False, tls=False):
        handler = type("_KeepAliveHandler", (_Handler,), {"protocol_version": "HTTP/1.1"}) if keep_alive else _Handler
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._httpd.daemon_threads = True
        self._httpd.lock = threading.Lock()
        self._httpd.connections = 0
        self._httpd.drop_after_reply = drop_after_reply
        self._httpd.batch_posts = []
        self._httpd.custom_posts = []
        self._httpd.get_paths = []
        self._httpd.batch_post_status = 200
        self._httpd.custom_post_status = 200
        self._httpd.ce_get_status = 200
        self.port = self._httpd.server_address[1]
        self._scheme = "https" if tls else "http"
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            _ca().issue_cert("127.0.0.1").configure_cert(ctx)
            self._httpd.socket = ctx.wrap_socket(self._httpd.socket, server_side=True)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def endpoint(self):
        return f"{self._scheme}://127.0.0.1:{self.port}/base"

    # --- recorded traffic ---
    @property
    def batch_posts(self):
        return self._httpd.batch_posts

    @property
    def custom_posts(self):
        return self._httpd.custom_posts

    @property
    def get_paths(self):
        return self._httpd.get_paths

    @property
    def connections(self):
        return self._httpd.connections

    # --- response controls ---
    def set_batch_status(self, status):
        self._httpd.batch_post_status = status

    def set_custom_get_status(self, status):
        self._httpd.ce_get_status = status

    def set_custom_post_status(self, status):
        self._httpd.custom_post_status = status


def trusting_context():
    """A client SSL context with normal verification that trusts only the throwaway test CA."""
    # Passing cadata skips the system trust store, so only the test CA is trusted.
    return _create_default_context(cadata=_ca().cert_pem.bytes().decode("ascii"))


def wait_until(predicate, timeout=3.0, interval=0.02):
    """Polls ``predicate`` until it is truthy or ``timeout`` elapses. Returns its final value."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()
