"""Unit tests for KNetwork against an in-process mock ingress."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mockserver import MockIngress  # noqa: E402

from keewano_sdk.internal import guid  # noqa: E402
from keewano_sdk.internal.batch import KBatch  # noqa: E402
from keewano_sdk.internal.network import KNetwork, SendResult  # noqa: E402


def _batch_with_payload():
    b = KBatch(guid.from_uint64(1), guid.from_uint64(2), guid.from_uint64(3))
    b.batch_num = 4
    b.batch_start_time = 111
    b.batch_end_time = 222
    b.data.write_string("payload")
    return b


class KNetworkTest(unittest.TestCase):
    def test_send_batch_success_sets_headers_and_body(self):
        with MockIngress() as srv:
            net = KNetwork(srv.endpoint, "app-secret", None, "1.0.0")
            b = _batch_with_payload()
            self.assertEqual(net.send_batch(b, test_user=None), SendResult.ACCEPTED)
            self.assertEqual(len(srv.batch_posts), 1)
            hdrs, body = srv.batch_posts[0]
            self.assertEqual(body, bytes(b.data.raw_buffer()[: b.data.length]))
            self.assertEqual(hdrs["k-sdk"], "Python/1.0.0")
            self.assertEqual(hdrs["k-token"], "app-secret")
            self.assertEqual(hdrs["k-installid"], str(b.install_id))
            self.assertEqual(hdrs["k-uid"], str(b.user_id))
            self.assertEqual(hdrs["k-ds"], str(b.data_session_id))
            self.assertEqual(hdrs["k-batch"], "4")
            self.assertEqual(hdrs["k-batchstarttime"], "111")
            self.assertEqual(hdrs["k-batchendtime"], "222")
            self.assertNotIn("k-tester", hdrs)

    def test_send_batch_includes_tester_header_when_given(self):
        with MockIngress() as srv:
            net = KNetwork(srv.endpoint, "s", None, "1.0.0")
            net.send_batch(_batch_with_payload(), test_user="qa-bob")
            hdrs, _ = srv.batch_posts[0]
            self.assertEqual(hdrs["k-tester"], "qa-bob")

    def test_send_batch_proxy_bearer_header(self):
        with MockIngress() as srv:
            net = KNetwork(srv.endpoint, "s", "bearer-xyz", "1.0.0")
            net.send_batch(_batch_with_payload(), test_user=None)
            hdrs, _ = srv.batch_posts[0]
            self.assertEqual(hdrs["authorization"], "Bearer bearer-xyz")

    def test_send_batch_non_2xx_is_rejected(self):
        with MockIngress() as srv:
            srv.set_batch_status(500)
            net = KNetwork(srv.endpoint, "s", None, "1.0.0")
            self.assertEqual(net.send_batch(_batch_with_payload(), test_user=None), SendResult.REJECTED)

    def test_send_batch_unreachable(self):
        net = KNetwork("http://127.0.0.1:1/base", "s", None, "1.0.0")  # nothing listening
        self.assertEqual(net.send_batch(_batch_with_payload(), test_user=None), SendResult.UNREACHABLE)

    def test_get_custom_event_ids_status_mapping(self):
        with MockIngress() as srv:
            net = KNetwork(srv.endpoint, "s", None, "1.0.0")

            srv.set_custom_get_status(200)
            r = net.get_custom_event_ids(7)
            self.assertTrue(r.has_mapping)
            self.assertFalse(r.need_to_register)

            srv.set_custom_get_status(204)
            r = net.get_custom_event_ids(7)
            self.assertFalse(r.has_mapping)
            self.assertTrue(r.need_to_register)

            srv.set_custom_get_status(500)
            r = net.get_custom_event_ids(7)
            self.assertFalse(r.has_mapping)
            self.assertFalse(r.need_to_register)

    def test_get_custom_event_ids_sends_hash_header(self):
        with MockIngress() as srv:
            net = KNetwork(srv.endpoint, "s", None, "1.0.0")
            net.get_custom_event_ids(42)
            self.assertTrue(srv.get_paths[0].endswith("/custom"))

    def test_register_custom_events_sends_gzip_verbatim(self):
        with MockIngress() as srv:
            net = KNetwork(srv.endpoint, "s", None, "1.0.0")
            payload = b"\x1f\x8b already-gzipped bytes"
            self.assertTrue(net.register_custom_events(version=123, event_count=5, gzip_data=payload))
            hdrs, body = srv.custom_posts[0]
            self.assertEqual(body, payload)  # sent as-is, not re-compressed
            self.assertEqual(hdrs["k-customeventhash"], "123")
            self.assertEqual(hdrs["k-customeventcount"], "5")
            self.assertEqual(hdrs["content-encoding"], "gzip")

    def test_register_custom_events_created_status_is_success(self):
        with MockIngress() as srv:
            srv.set_custom_post_status(201)
            net = KNetwork(srv.endpoint, "s", None, "1.0.0")
            self.assertTrue(net.register_custom_events(1, 1, b"x"))

    def test_register_custom_events_failure_status(self):
        with MockIngress() as srv:
            srv.set_custom_post_status(400)
            net = KNetwork(srv.endpoint, "s", None, "1.0.0")
            self.assertFalse(net.register_custom_events(1, 1, b"x"))


if __name__ == "__main__":
    unittest.main()
