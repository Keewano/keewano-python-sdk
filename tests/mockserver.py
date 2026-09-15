"""Test-only helpers: a localhost ingress mock and a polling wait, shared by the tests that
exercise the network transport, dispatcher send loop, and public facade.

Not a test module (name does not match ``test*.py``), so unittest discovery ignores it.
"""

import http.server
import threading
import time


class _Handler(http.server.BaseHTTPRequestHandler):
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
        self.end_headers()

    def log_message(self, *args):
        pass  # keep test output clean


class MockIngress:
    """A minimal in-process HTTP server standing in for the Keewano ingress.

    Records requests and lets tests set response codes for batch uploads (``/in``),
    custom-event lookups (GET ``/custom``), and custom-event registration (POST ``/custom``).
    """

    def __init__(self):
        self._httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.batch_posts = []
        self._httpd.custom_posts = []
        self._httpd.get_paths = []
        self._httpd.batch_post_status = 200
        self._httpd.custom_post_status = 200
        self._httpd.ce_get_status = 200
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.port}/base"

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

    # --- response controls ---
    def set_batch_status(self, status):
        self._httpd.batch_post_status = status

    def set_custom_get_status(self, status):
        self._httpd.ce_get_status = status

    def set_custom_post_status(self, status):
        self._httpd.custom_post_status = status


def wait_until(predicate, timeout=3.0, interval=0.02):
    """Polls ``predicate`` until it is truthy or ``timeout`` elapses. Returns its final value."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()
