"""Tests for the server-side facade (keewano_sdk.server_sdk) end to end against a mock ingress."""

import importlib.util
import os
import struct
import sys
import tempfile
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mockserver import MockIngress, wait_until  # noqa: E402

import keewano_sdk  # noqa: E402
from keewano_sdk import AdType, Item, KeewanoServerCodegen, KeewanoServerConfig, KeewanoServerSDK  # noqa: E402
from keewano_sdk import server_sdk  # noqa: E402
from keewano_sdk.internal import guid  # noqa: E402
from keewano_sdk.internal.events import KEvents  # noqa: E402

USER = "11111111-1111-4111-8111-111111111111"


class ServerFacadeBase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()

    def tearDown(self):
        server_sdk.shutdown(0)

    def _init(self, srv=None, **overrides):
        cfg = dict(
            api_key="demo",
            endpoint=srv.endpoint if srv is not None else "http://127.0.0.1:1/base",
            flush_interval=60.0,
            # The default, spelled out: a persistent run without data_dir would write into the real
            # per-user data directory and leave batches there for later runs to pick up.
            persistent_storage=False,
        )
        cfg.update(overrides)
        server_sdk.initialize(KeewanoServerConfig(**cfg))


class LifecycleTest(ServerFacadeBase):
    def test_reports_dropped_before_initialize(self):
        with self.assertLogs("keewano_sdk", level="WARNING") as cm:
            server_sdk.report_install_campaign(USER, "promo")
        self.assertTrue(any("not initialized" in m for m in cm.output))
        self.assertFalse(server_sdk.is_initialized())

    def test_blank_api_key_leaves_sdk_inert(self):
        with self.assertLogs("keewano_sdk", level="ERROR"):
            server_sdk.initialize(KeewanoServerConfig(api_key="  "))
        self.assertFalse(server_sdk.is_initialized())

    def test_initialize_is_idempotent(self):
        self._init()
        first = server_sdk._dispatcher
        self._init()
        self.assertIs(server_sdk._dispatcher, first)

    def test_invalid_numbers_fall_back_to_defaults(self):
        with self.assertLogs("keewano_sdk", level="ERROR") as cm:
            self._init(max_users=0, flush_interval=float("nan"), max_buffered_bytes=2.5)
        self.assertTrue(server_sdk.is_initialized())
        d = server_sdk._dispatcher
        self.assertEqual(d._max_users, 10_000)
        self.assertEqual(d._flush_interval, 15.0)
        self.assertEqual(d._max_buffered_bytes, 16 * 1024 * 1024)
        self.assertEqual(len(cm.output), 3)

    def test_persistent_storage_uses_data_dir(self):
        self._init(persistent_storage=True, data_dir=self._dir)
        self.assertTrue(server_sdk._dispatcher.persistent)
        self.assertFalse(server_sdk._dispatcher._started)  # lazy: nothing runs until an event or flush
        server_sdk.flush()
        self.assertTrue(wait_until(lambda: os.path.isdir(os.path.join(self._dir, "server", "worker-0"))))

    def test_config_defaults_to_ephemeral_storage(self):
        self.assertFalse(KeewanoServerConfig(api_key="k").persistent_storage)
        # Only api_key (+ a test endpoint): RAM mode with the ephemeral tuning, nothing written to disk.
        server_sdk.initialize(KeewanoServerConfig(api_key="demo", endpoint="http://127.0.0.1:1/base"))
        d = server_sdk._dispatcher
        self.assertFalse(d.persistent)
        self.assertEqual(d._flush_interval, 15.0)
        self.assertEqual(d._max_pending_bytes, 16 * 1024 * 1024)
        self.assertEqual(server_sdk._shutdown_timeout, 10.0)

    def test_persistent_mode_uses_its_own_defaults(self):
        self._init(persistent_storage=True, data_dir=self._dir, flush_interval=None)
        d = server_sdk._dispatcher
        self.assertEqual(d._work_root, os.path.join(self._dir, "server"))
        self.assertEqual(d._flush_interval, 60.0)
        self.assertEqual(d._max_pending_bytes, 50 * 1024 * 1024)
        self.assertEqual(server_sdk._shutdown_timeout, 2.0)

    def test_invalid_shutdown_timeout_falls_back_to_default(self):
        for bad in (-1, float("inf"), "5", True):
            with self.subTest(bad=bad):
                with self.assertLogs("keewano_sdk", level="ERROR"):
                    self._init(shutdown_timeout=bad)
                self.assertEqual(server_sdk._shutdown_timeout, 10.0)
                server_sdk.shutdown(0)

    def test_valid_shutdown_timeout_is_used(self):
        self._init(shutdown_timeout=3)
        self.assertEqual(server_sdk._shutdown_timeout, 3.0)

    def test_initialize_never_raises(self):
        with self.assertLogs("keewano_sdk", level="ERROR") as cm:
            server_sdk.initialize(None)  # not a config at all
        self.assertTrue(any("initialization failed" in m for m in cm.output))
        self.assertFalse(server_sdk.is_initialized())

    def test_failing_engine_shutdown_is_logged_and_disables_the_sdk(self):
        self._init()
        with mock.patch.object(server_sdk._dispatcher, "shutdown", side_effect=RuntimeError("boom")):
            with self.assertLogs("keewano_sdk", level="ERROR") as cm:
                server_sdk.shutdown()
        self.assertTrue(any("shutdown failed" in m for m in cm.output))
        self.assertFalse(server_sdk.is_initialized())

    def test_flush_before_initialize_and_with_a_bad_user(self):
        server_sdk.flush()  # no-op, no error
        server_sdk.flush(USER)
        self._init()
        with self.assertLogs("keewano_sdk", level="ERROR"):
            server_sdk.flush("not-a-guid")
        self.assertFalse(server_sdk._dispatcher._started)  # a rejected user id starts nothing

    def test_facade_class_exposes_the_same_callables(self):
        self.assertIs(KeewanoServerSDK.report_ad_revenue, server_sdk.report_ad_revenue)
        self.assertIs(keewano_sdk.server_sdk, server_sdk)


class UserIdTest(ServerFacadeBase):
    def test_accepted_forms_map_like_the_client_set_user_id(self):
        self.assertEqual(server_sdk._user(42, "t"), guid.from_uint64(42))
        self.assertEqual(server_sdk._user(USER, "t"), guid.from_string(USER))
        self.assertEqual(server_sdk._user(uuid.UUID(USER), "t"), guid.from_string(USER))

    def test_rejected_forms(self):
        for bad in (0, -1, 2**64, True, None, 1.5, "not-a-guid", "00000000-0000-0000-0000-000000000000"):
            with self.subTest(bad=bad):
                with self.assertLogs("keewano_sdk", level="ERROR"):
                    self.assertIsNone(server_sdk._user(bad, "t"))

    def test_bad_user_drops_the_event(self):
        self._init()
        with self.assertLogs("keewano_sdk", level="ERROR"):
            server_sdk.report_install_campaign("nope", "promo")
        self.assertEqual(server_sdk._dispatcher.stats()["buffered_bytes"], 0)


class EndToEndTest(ServerFacadeBase):
    def test_batches_carry_the_users_identity_in_headers(self):
        with MockIngress(keep_alive=True) as srv:
            self._init(srv)
            server_sdk.report_in_app_purchase(USER, "gems", price_usd_cents=499)
            server_sdk.report_in_app_purchase_items_granted(USER, "gems", [Item("gems", 100)])
            server_sdk.report_ad_offered(7, "level_end", AdType.REWARDED)
            server_sdk.report_ad_revenue(7, "level_end", localized_revenue=0.02, currency_code="EUR")
            server_sdk.flush()
            self.assertTrue(wait_until(lambda: len(srv.batch_posts) == 2))
            by_uid = {h["k-uid"]: (h, body) for h, body in srv.batch_posts}
            self.assertEqual(set(by_uid), {USER, str(guid.from_uint64(7))})
            for uid, (h, _) in by_uid.items():
                self.assertEqual(h["k-installid"], uid)
                self.assertEqual(h["k-batch"], "0")
            self.assertNotEqual(by_uid[USER][0]["k-ds"], by_uid[str(guid.from_uint64(7))][0]["k-ds"])
            self.assertEqual(srv.connections, 1)  # both users' batches went over one kept-alive connection

    def test_every_report_function_emits(self):
        with MockIngress() as srv:
            self._init(srv)
            u = 99
            server_sdk.report_in_app_purchase(u, "p", localized_price=1.5, currency_code="USD")
            server_sdk.report_subscription_revenue(u, "vip", revenue_usd_cents=999)
            server_sdk.report_subscription_items_granted(u, "vip", [Item("chest")])
            server_sdk.report_ad_items_granted(u, "ad", [Item("coins", 5)])
            server_sdk.report_items_exchange(u, "shop", [Item("coins", 1)], [Item("sword")])
            server_sdk.report_items_reset(u, "init", [Item("coins", 500)])
            server_sdk.report_install_campaign(u, "summer")
            server_sdk.report_game_language(u, "fr")
            server_sdk.report_onboarding_milestone(u, "step1")
            server_sdk.report_ab_test_group_assignment(u, "layout", "B")
            server_sdk.log_error(u, "boom")
            server_sdk.report_ad_revenue(u, "ad", revenue_usd_cents=12)
            server_sdk.report_subscription_revenue(u, "vip", localized_revenue=4.5, currency_code="EUR")
            KeewanoServerCodegen.report_custom_event_uint(u, 2500, 3)
            KeewanoServerCodegen.report_custom_event_int(u, 2500, -3)
            KeewanoServerCodegen.report_custom_event_float(u, 2500, -1.5)
            KeewanoServerCodegen.report_custom_event_str(u, 2500, "custom-str")
            KeewanoServerCodegen.report_custom_event_ushort_pair(u, 2500, 1, 2)
            server_sdk.flush(u)
            self.assertTrue(wait_until(lambda: len(srv.batch_posts) == 1))
            body = srv.batch_posts[0][1]
            for needle in (b"vip", b"chest", b"shop", b"summer", b"step1", b"layout", b"boom", b"EUR", b"custom-str"):
                self.assertIn(needle, body)

    def test_invalid_payloads_are_dropped(self):
        self._init()
        cases = [
            lambda: server_sdk.report_in_app_purchase(USER, "p"),
            lambda: server_sdk.report_in_app_purchase(USER, " ", price_usd_cents=1),
            lambda: server_sdk.report_ad_offered(USER, "ad", 3),
            lambda: server_sdk.report_ab_test_group_assignment(USER, "t", "AB"),
            lambda: server_sdk.report_items_reset(USER, "x", "notalist"),
            lambda: KeewanoServerCodegen.report_custom_event_int(USER, 12, 1),
            lambda: KeewanoServerCodegen.report_custom_event_float(USER, 2500, float("inf")),
            # Every facade function's own payload checks.
            lambda: server_sdk.report_in_app_purchase(USER, "p", localized_price=1.0, currency_code=" "),
            lambda: server_sdk.report_in_app_purchase(USER, "p", price_usd_cents=-1),
            lambda: server_sdk.report_in_app_purchase_items_granted(USER, "p", [("gems", 1)]),
            lambda: server_sdk.report_ad_offered(USER, " ", AdType.REWARDED),
            lambda: server_sdk.report_ad_revenue(USER, "ad"),
            lambda: server_sdk.report_ad_revenue(USER, "ad", localized_revenue=float("nan"), currency_code="EUR"),
            lambda: server_sdk.report_ad_items_granted(USER, " ", [Item("coins")]),
            lambda: server_sdk.report_subscription_revenue(USER, "vip"),
            lambda: server_sdk.report_subscription_revenue(USER, "vip", revenue_usd_cents=2**32),
            lambda: server_sdk.report_subscription_items_granted(USER, "vip", "notalist"),
            lambda: server_sdk.report_items_exchange(USER, "shop", [Item("a")], "notalist"),
            lambda: server_sdk.report_install_campaign(USER, " "),
            lambda: server_sdk.report_game_language(USER, ""),
            lambda: server_sdk.report_onboarding_milestone(USER, None),
            lambda: server_sdk.report_ab_test_group_assignment(USER, "t", 1),
            lambda: server_sdk.report_ab_test_group_assignment(USER, "t", "\u0100"),
            lambda: server_sdk.log_error(USER, " "),
            # Server codegen bridge: one bad payload per type.
            lambda: KeewanoServerCodegen.report_custom_event(USER, 1),
            lambda: KeewanoServerCodegen.report_custom_event_int(USER, 2500, 2**31),
            lambda: KeewanoServerCodegen.report_custom_event_uint(USER, 2500, -1),
            lambda: KeewanoServerCodegen.report_custom_event_bool(USER, 2500, 1),
            lambda: KeewanoServerCodegen.report_custom_event_bool(USER, 1, True),
            lambda: KeewanoServerCodegen.report_custom_event_float(USER, 2500, "1.0"),
            lambda: KeewanoServerCodegen.report_custom_event_float(USER, 2500, 1e300),
            lambda: KeewanoServerCodegen.report_custom_event_str(USER, 2500, " "),
            lambda: KeewanoServerCodegen.report_custom_event_str(USER, 1, "x"),
            lambda: KeewanoServerCodegen.report_custom_event_ushort_pair(USER, 2500, 1, 70000),
            lambda: KeewanoServerCodegen.report_custom_event_ushort_pair(USER, 2500, -1, 1),
        ]
        for case in cases:
            with self.assertLogs("keewano_sdk", level="ERROR"):
                case()
        self.assertEqual(server_sdk._dispatcher.stats()["buffered_bytes"], 0)

    def test_shutdown_uploads_buffered_events_and_disables(self):
        with MockIngress() as srv:
            self._init(srv)
            for i in range(1, 6):
                server_sdk.report_game_language(i, "en")
            server_sdk.shutdown(5)
            self.assertEqual(len(srv.batch_posts), 5)
            self.assertFalse(server_sdk.is_initialized())

    def test_persistent_batches_resend_after_restart(self):
        with MockIngress() as srv:
            srv.set_batch_status(500)
            self._init(srv, persistent_storage=True, data_dir=self._dir)
            server_sdk.report_game_language(USER, "de")
            server_sdk.shutdown(0.5)
            self.assertTrue(wait_until(lambda: len(srv.batch_posts) >= 1))
            srv.set_batch_status(200)
            self._init(srv, persistent_storage=True, data_dir=self._dir)
            server_sdk.flush()
            self.assertTrue(wait_until(lambda: len(srv.batch_posts) >= 2))
            h, body = srv.batch_posts[-1]
            self.assertEqual(h["k-uid"], USER)
            self.assertIn(b"de", body)

    def test_test_user_name_header(self):
        with MockIngress() as srv:
            self._init(srv, test_user_name="staging")
            server_sdk.report_game_language(USER, "en")
            server_sdk.flush()
            self.assertTrue(wait_until(lambda: len(srv.batch_posts) == 1))
            self.assertEqual(srv.batch_posts[0][0]["k-tester"], "staging")

    def test_latin1_test_user_name_is_sent_as_is(self):
        with MockIngress() as srv:
            self._init(srv, test_user_name="staging-café")
            server_sdk.report_game_language(USER, "en")
            server_sdk.flush()
            self.assertTrue(wait_until(lambda: len(srv.batch_posts) == 1))
            self.assertEqual(srv.batch_posts[0][0]["k-tester"], "staging-café")

    def test_non_latin1_test_user_name_is_ignored_and_uploads_still_work(self):
        with MockIngress() as srv:
            with self.assertLogs("keewano_sdk", level="ERROR") as cm:
                self._init(srv, test_user_name="тест")
            self.assertTrue(any("KeewanoServerConfig" in m and "latin-1" in m for m in cm.output))
            self.assertTrue(server_sdk.is_initialized())  # the SDK still starts, just untagged
            server_sdk.report_game_language(USER, "en")
            server_sdk.flush()
            self.assertTrue(wait_until(lambda: len(srv.batch_posts) == 1))
            self.assertNotIn("k-tester", srv.batch_posts[0][0])

    def test_test_user_name_with_a_line_break_is_ignored_and_uploads_still_work(self):
        with MockIngress() as srv:
            with self.assertLogs("keewano_sdk", level="ERROR") as cm:
                self._init(srv, test_user_name="staging\r\nX-Evil: 1")  # would inject a header
            self.assertTrue(any("control character" in m for m in cm.output))
            server_sdk.report_game_language(USER, "en")
            server_sdk.flush()
            self.assertTrue(wait_until(lambda: len(srv.batch_posts) == 1))
            headers = srv.batch_posts[0][0]
            self.assertNotIn("k-tester", headers)
            self.assertNotIn("x-evil", headers)


@unittest.skipUnless(hasattr(os, "fork"), "needs os.fork")
class FacadeForkTest(ServerFacadeBase):
    def test_register_at_fork_resets_the_child(self):
        with MockIngress() as srv:
            self._init(srv)
            server_sdk.report_game_language(1, "parent")
            pid = os.fork()
            if pid == 0:
                code = 1
                try:
                    st = server_sdk._dispatcher.stats()
                    if st["users"] == 0 and st["buffered_bytes"] == 0:
                        server_sdk.report_game_language(2, "child")
                        server_sdk.shutdown(5)
                        code = 0
                finally:
                    os._exit(code)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(status, 0)
            self.assertTrue(wait_until(lambda: len(srv.batch_posts) == 1))
            server_sdk.shutdown(5)
            self.assertEqual(len(srv.batch_posts), 2)
            uids = sorted(h["k-uid"] for h, _ in srv.batch_posts)
            self.assertEqual(uids, sorted([str(guid.from_uint64(1)), str(guid.from_uint64(2))]))
            self.assertEqual(sum(b"parent" in b for _, b in srv.batch_posts), 1)  # never sent twice


class GeneratedServerReportersTest(ServerFacadeBase):
    """The codegen's real output (sample/keewano_custom_events.py) against the real server SDK."""

    def _generated(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample", "keewano_custom_events.py"
        )
        spec = importlib.util.spec_from_file_location("keewano_custom_events_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_server_reporters_land_in_the_users_batch(self):
        generated = self._generated()
        with MockIngress() as srv:
            self._init(srv, custom_event_set=generated.CUSTOM_EVENT_SET)
            generated.server_report_save_world(USER)
            generated.server_report_solve_hunger(USER, True)
            server_sdk.flush()
            self.assertTrue(wait_until(lambda: len(srv.batch_posts) == 1))
            headers, body = srv.batch_posts[0]
            self.assertEqual(headers["k-uid"], USER)
            self.assertEqual(headers["k-customeventhash"], str(generated.CUSTOM_EVENT_SET.version))
            # 2500 (no payload) then 2501 with the bool encoded as 2 (= true).
            self.assertEqual(struct.unpack_from("<H", body, 4)[0], 2500)
            self.assertEqual(struct.unpack_from("<HB", body, 10), (2501, 2))

    def test_server_reporter_validates_the_user(self):
        generated = self._generated()
        self._init()
        with self.assertLogs("keewano_sdk", level="ERROR"):
            generated.server_report_save_world("not-a-guid")
        self.assertEqual(server_sdk._dispatcher.stats()["buffered_bytes"], 0)


class OnboardingMilestoneTest(ServerFacadeBase):
    def test_repeat_is_sent_verbatim(self):
        with MockIngress() as srv:
            self._init(srv)
            server_sdk.report_onboarding_milestone(USER, "tutorial")
            server_sdk.report_onboarding_milestone(USER, "tutorial")
            server_sdk.flush()
            self.assertTrue(wait_until(lambda: len(srv.batch_posts) == 1))
            self.assertEqual(srv.batch_posts[0][1].count(b"tutorial"), 2)
            self.assertNotIn(b"(#", srv.batch_posts[0][1])
            self.assertIn(KEvents.ONBOARDING_MILESTONE.to_bytes(2, "little"), srv.batch_posts[0][1])


if __name__ == "__main__":
    unittest.main()
