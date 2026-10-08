"""Unit tests for the public facade in keewano_sdk.sdk.

The facade uses module-level singletons, so each test resets them and restores the interpreter
exception hooks in tearDown to stay isolated.
"""

import os
import struct
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mockserver import MockIngress, wait_until  # noqa: E402

import keewano_sdk  # noqa: E402
from keewano_sdk import AdType, KeewanoConfig, KeewanoSDK  # noqa: E402
from keewano_sdk import sdk as sdkmod  # noqa: E402
from keewano_sdk.internal import guid  # noqa: E402
from keewano_sdk.internal.consent import UserConsentState  # noqa: E402
from keewano_sdk.internal.events import KEvents  # noqa: E402
from keewano_sdk.internal.storage import KStorage  # noqa: E402


class FacadeTestBase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._orig_excepthook = sys.excepthook
        self._orig_thread_excepthook = threading.excepthook
        self._reset_singletons()

    def tearDown(self):
        keewano_sdk.shutdown()
        self._reset_singletons()
        sys.excepthook = self._orig_excepthook
        threading.excepthook = self._orig_thread_excepthook

    @staticmethod
    def _reset_singletons():
        sdkmod._dispatcher = None
        sdkmod._storage = None
        sdkmod._user_identifiers = None

    def _init(self, srv=None, **overrides):
        cfg = dict(
            api_key="demo",
            app_version="1.0.0",
            data_dir=self._dir,
            disable_exception_tracking=True,
            endpoint=srv.endpoint if srv is not None else "http://127.0.0.1:1/base",
        )
        cfg.update(overrides)
        keewano_sdk.initialize(KeewanoConfig(**cfg))


class InitializationTest(FacadeTestBase):
    def test_reports_dropped_before_initialize(self):
        with self.assertLogs("keewano_sdk", level="WARNING") as cm:
            keewano_sdk.report_button_click("Play")
        self.assertTrue(any("not initialized" in m for m in cm.output))
        self.assertIsNone(keewano_sdk.get_install_id())

    def test_initialize_sets_up_storage_and_install_id(self):
        self._init()
        install_id = keewano_sdk.get_install_id()
        self.assertIsNotNone(install_id)
        self.assertEqual(len(install_id), 36)  # canonical GUID string

    def test_initialize_is_idempotent(self):
        self._init()
        first = sdkmod._dispatcher
        self._init()  # second call should be ignored
        self.assertIs(sdkmod._dispatcher, first)

    def test_shutdown_persists_synchronously_then_stops(self):
        self._init()
        d = sdkmod._dispatcher
        order = []
        d.stop = lambda: order.append("stop")  # type: ignore[assignment]
        d.persist_now = lambda: order.append("persist")  # type: ignore[assignment]
        keewano_sdk.shutdown()
        # Both run, and the synchronous persist comes after stop() has wound the sender down.
        self.assertEqual(order, ["stop", "persist"])
        self.assertIsNone(sdkmod._dispatcher)

    def test_launch_burst_uploaded_with_app_launch_first(self):
        with MockIngress() as srv:
            self._init(srv)
            keewano_sdk.flush()
            self.assertTrue(wait_until(lambda: len(srv.batch_posts) >= 1))
            _hdrs, body = srv.batch_posts[0]
            _ts, event_id = struct.unpack_from("<IH", body, 0)
            self.assertEqual(event_id, KEvents.APP_LAUNCH)

    def test_exception_hook_installed_when_enabled(self):
        self._init(disable_exception_tracking=False)
        self.assertIsNot(sys.excepthook, self._orig_excepthook)

    def test_exception_hook_not_installed_when_disabled(self):
        self._init(disable_exception_tracking=True)
        self.assertIs(sys.excepthook, self._orig_excepthook)

    def test_excepthook_persists_synchronously(self):
        import contextlib
        import io

        self._init(disable_exception_tracking=False)
        d = sdkmod._dispatcher
        called = []
        orig_persist = d.persist_now
        d.persist_now = lambda: called.append(True)  # spy on the synchronous crash-path persist
        try:
            try:
                raise ValueError("boom")
            except ValueError:
                exc = sys.exc_info()
            with contextlib.redirect_stderr(io.StringIO()):  # swallow the chained traceback print
                sys.excepthook(*exc)
        finally:
            d.persist_now = orig_persist
        self.assertTrue(called, "the installed excepthook must persist synchronously")

    def test_excepthook_logs_full_traceback(self):
        import contextlib
        import io

        self._init(disable_exception_tracking=False)
        d = sdkmod._dispatcher
        logged = []
        d.log_error = lambda msg: logged.append(msg)  # capture the message that reaches the wire

        def _raiser():
            raise ValueError("boom-with-trace")

        try:
            _raiser()
        except ValueError:
            exc = sys.exc_info()
        with contextlib.redirect_stderr(io.StringIO()):  # swallow the chained traceback print
            sys.excepthook(*exc)

        self.assertTrue(logged)
        msg = logged[0]
        self.assertIn("Traceback (most recent call last)", msg)  # not just "Type: message"
        self.assertIn("ValueError: boom-with-trace", msg)
        self.assertIn("_raiser", msg)  # a stack frame from where it was raised

    def test_is_our_own_fault_detects_sdk_frames(self):
        # App-origin: deepest frame is this test module -> not ours.
        try:
            raise ValueError("app boom")
        except ValueError:
            app_tb = sys.exc_info()[2]
        self.assertFalse(sdkmod._is_our_own_fault(app_tb))
        self.assertFalse(sdkmod._is_our_own_fault(None))

        # SDK-origin: deepest frame lives in a module named under keewano_sdk.*
        ns = {"__name__": "keewano_sdk._faketest"}
        try:
            exec("def f():\n raise ValueError('sdk boom')\nf()", ns)
        except ValueError:
            sdk_tb = sys.exc_info()[2]
        self.assertTrue(sdkmod._is_our_own_fault(sdk_tb))

    def test_excepthook_does_not_report_sdk_own_faults(self):
        chained = []
        sys.excepthook = lambda *a: chained.append(a)  # becomes the "previous" hook init chains to
        self._init(disable_exception_tracking=False)
        d = sdkmod._dispatcher
        logged = []
        d.log_error = lambda msg: logged.append(msg)  # the "report" — must stay empty for our own fault
        d.persist_now = lambda: None  # flushing the already-buffered events to disk is fine/expected

        ns = {"__name__": "keewano_sdk._faketest"}
        try:
            exec("def f():\n raise ValueError('sdk boom')\nf()", ns)
        except ValueError:
            exc = sys.exc_info()
        with self.assertLogs("keewano_sdk", level="ERROR") as cm:
            sys.excepthook(*exc)  # our installed hook

        self.assertTrue(any("not reporting it as an app error" in m for m in cm.output))
        self.assertEqual(logged, [])  # the SDK's own crash is not reported as an app-error event
        self.assertEqual(len(chained), 1)  # but still chained to the previous handler

    def test_excepthook_ignores_keyboardinterrupt_and_systemexit(self):
        chained = []
        sys.excepthook = lambda *a: chained.append(a[0])  # the "previous" hook init chains to
        self._init(disable_exception_tracking=False)
        d = sdkmod._dispatcher
        logged = []
        d.log_error = lambda msg: logged.append(msg)  # the "report" — must stay empty for KI/SystemExit
        d.persist_now = lambda: None  # flushing the already-buffered events to disk is fine/expected

        for exc in (KeyboardInterrupt(), SystemExit(2)):
            sys.excepthook(type(exc), exc, None)

        self.assertEqual(logged, [])  # neither Ctrl-C nor exit is reported as an error event
        # Still chained to the previous handler for both, so normal termination is unaffected.
        self.assertEqual(chained, [KeyboardInterrupt, SystemExit])

    def test_shutdown_restores_exception_hooks(self):
        self._init(disable_exception_tracking=False)
        self.assertIsNot(sys.excepthook, self._orig_excepthook)  # installed
        self.assertIsNot(threading.excepthook, self._orig_thread_excepthook)
        keewano_sdk.shutdown()
        # Both process hooks are put back exactly as they were before init.
        self.assertIs(sys.excepthook, self._orig_excepthook)
        self.assertIs(threading.excepthook, self._orig_thread_excepthook)

    def test_init_shutdown_init_does_not_stack_hooks(self):
        # The scenario this guards: without a proper uninstall, the first cycle leaves our hook as
        # sys.excepthook, so the second init saves OUR hook as "previous" and stacks on it.
        self._init(disable_exception_tracking=False)
        keewano_sdk.shutdown()

        self._init(disable_exception_tracking=False)  # second cycle must start from a clean slate
        self.assertIsNotNone(sdkmod._dispatcher)  # re-init works
        self.assertIs(sdkmod._prev_excepthook, self._orig_excepthook)  # previous == the real original
        self.assertIs(sdkmod._prev_thread_excepthook, self._orig_thread_excepthook)

        keewano_sdk.shutdown()
        self.assertIs(sys.excepthook, self._orig_excepthook)  # cleanly restored again, no residue
        self.assertIs(threading.excepthook, self._orig_thread_excepthook)

    def test_blank_api_key_does_not_start(self):
        with self.assertLogs("keewano_sdk", level="ERROR"):
            self._init(api_key="   ")
        self.assertIsNone(sdkmod._dispatcher)

    def test_initialize_never_raises_and_disables_on_failure(self):
        from unittest import mock

        # Force the dispatcher construction to blow up; initialize must swallow it and disable.
        with mock.patch.object(sdkmod, "KEventDispatcher", side_effect=RuntimeError("boom")):
            self._init()  # must not raise
        self.assertIsNone(sdkmod._dispatcher)

    def test_unreadable_identifiers_disable_the_sdk(self):
        from unittest import mock

        with mock.patch.object(KStorage, "load_or_init_identifiers", return_value=None):
            with self.assertLogs("keewano_sdk", level="ERROR"):
                self._init()
        self.assertIsNone(sdkmod._dispatcher)

    def test_consent_reconciled_when_app_turns_consent_on(self):
        # A prior run persisted NOT_REQUIRED; the app now requires consent -> resolves to PENDING.
        store = KStorage(self._dir)
        store.save_user_consent_state(UserConsentState.NOT_REQUIRED)
        self._init(require_user_consent=True)
        self.assertEqual(sdkmod._storage.load_user_consent_state(), UserConsentState.PENDING)


class IdentifierStabilityTest(FacadeTestBase):
    def test_install_id_served_from_cache_without_reminting(self):
        from unittest import mock

        self._init()
        first = keewano_sdk.get_install_id()
        # After init, get_install_id must never touch storage again (so a transient read error can't
        # mint a new identity).
        with mock.patch.object(
            KStorage, "load_or_init_identifiers", side_effect=AssertionError("must not reload identifiers")
        ):
            self.assertEqual(keewano_sdk.get_install_id(), first)

    def test_set_user_id_does_not_reload_identifiers(self):
        from unittest import mock

        self._init()
        with mock.patch.object(
            KStorage, "load_or_init_identifiers", side_effect=AssertionError("must not reload identifiers")
        ):
            keewano_sdk.set_user_id(999)
        self.assertEqual(sdkmod._user_identifiers.user_id, guid.from_uint64(999))


class IdentityTest(FacadeTestBase):
    def test_set_user_id_numeric_persists(self):
        self._init()
        keewano_sdk.set_user_id(1234567890)
        stored = sdkmod._storage.load_or_init_identifiers().user_id
        self.assertEqual(stored, guid.from_uint64(1234567890))

    def test_set_user_id_string_persists(self):
        self._init()
        uid = "12345678-9abc-def0-1122-334455667788"
        keewano_sdk.set_user_id(uid)
        stored = sdkmod._storage.load_or_init_identifiers().user_id
        self.assertEqual(str(stored), uid)

    def test_set_user_id_invalid_string_logs_and_keeps_empty(self):
        self._init()
        with self.assertLogs("keewano_sdk", level="ERROR"):
            keewano_sdk.set_user_id("not-a-guid")
        self.assertEqual(sdkmod._storage.load_or_init_identifiers().user_id, guid.EMPTY)

    def test_set_user_id_wrong_type_logs(self):
        self._init()
        with self.assertLogs("keewano_sdk", level="ERROR"):
            keewano_sdk.set_user_id(3.14)  # type: ignore[arg-type]

    def test_set_user_id_rejects_bool(self):
        # bool is an int subclass, so it must be excluded explicitly rather than treated as 0/1.
        self._init()
        with self.assertLogs("keewano_sdk", level="ERROR"):
            keewano_sdk.set_user_id(True)  # type: ignore[arg-type]
        self.assertEqual(sdkmod._storage.load_or_init_identifiers().user_id, guid.EMPTY)

    def test_set_user_id_rejects_non_positive(self):
        self._init()
        for bad in (0, -1):
            with self.assertLogs("keewano_sdk", level="ERROR"):
                keewano_sdk.set_user_id(bad)
            self.assertEqual(sdkmod._storage.load_or_init_identifiers().user_id, guid.EMPTY)

    def test_set_user_id_rejects_value_above_uint64(self):
        # The illegal-uint64 case: a positive int that overflows the 64-bit id must be dropped, not
        # silently masked, and must not change the stored identity.
        self._init()
        for bad in (0x1_0000_0000_0000_0000, 0x1_0000_0000_0000_0000 + 5, 10**30):
            with self.assertLogs("keewano_sdk", level="ERROR"):
                keewano_sdk.set_user_id(bad)
            self.assertEqual(sdkmod._storage.load_or_init_identifiers().user_id, guid.EMPTY)

    def test_set_user_id_accepts_max_uint64(self):
        # The boundary: 2**64 - 1 is the largest valid value and must be accepted.
        self._init()
        top = 0xFFFFFFFFFFFFFFFF
        keewano_sdk.set_user_id(top)
        self.assertEqual(sdkmod._storage.load_or_init_identifiers().user_id, guid.from_uint64(top))


class ConsentTest(FacadeTestBase):
    def test_consent_gating_persists_and_flushes_on_grant(self):
        with MockIngress() as srv:
            self._init(srv, require_user_consent=True)
            keewano_sdk.flush()
            # Nothing sent while pending.
            self.assertTrue(
                wait_until(
                    lambda: os.path.exists(os.path.join(self._dir, "batches"))
                    and any(f.endswith(".kwub") for f in os.listdir(os.path.join(self._dir, "batches")))
                )
            )
            self.assertEqual(len(srv.batch_posts), 0)

            keewano_sdk.set_user_consent(True)
            self.assertTrue(wait_until(lambda: len(srv.batch_posts) >= 1))
            # Decision is persisted.
            self.assertEqual(sdkmod._storage.load_user_consent_state(), UserConsentState.GRANTED)

    def test_pre_sdk_registration_marker_set_once_and_ignores_future(self):
        self._init()
        past = datetime.now(timezone.utc) - timedelta(days=365)
        keewano_sdk.report_user_registered_before_sdk_integration(past)
        self.assertTrue(sdkmod._storage.has_pre_sdk_registration_been_reported())

    def test_pre_sdk_registration_future_date_ignored(self):
        self._init()
        future = datetime.now(timezone.utc) + timedelta(days=365)
        with self.assertLogs("keewano_sdk", level="WARNING"):
            keewano_sdk.report_user_registered_before_sdk_integration(future)
        self.assertFalse(sdkmod._storage.has_pre_sdk_registration_been_reported())

    def test_set_user_consent_non_bool_is_ignored_and_leaves_state_unchanged(self):
        self._init(require_user_consent=True)
        before = sdkmod._dispatcher._user_consent_state
        for bad in ("true", 1, None):  # a truthy non-bool must NOT be coerced to a grant
            with self.assertLogs("keewano_sdk", level="ERROR"):
                keewano_sdk.set_user_consent(bad)
        self.assertEqual(sdkmod._dispatcher._user_consent_state, before)  # still pending
        self.assertNotEqual(sdkmod._storage.load_user_consent_state(), UserConsentState.GRANTED)

    def test_pre_sdk_registration_non_datetime_is_ignored(self):
        self._init()
        for bad in (1699999999, "2021-01-01", None):  # would crash on attribute access without the guard
            with self.assertLogs("keewano_sdk", level="ERROR"):
                keewano_sdk.report_user_registered_before_sdk_integration(bad)
        self.assertFalse(sdkmod._storage.has_pre_sdk_registration_been_reported())


class ValidationTest(FacadeTestBase):
    def test_report_in_app_purchase_requires_a_price(self):
        self._init()
        with self.assertLogs("keewano_sdk", level="ERROR"):
            keewano_sdk.report_in_app_purchase("gems_100")  # neither price form supplied

    def test_report_in_app_purchase_valid_forms_do_not_error(self):
        self._init()
        # Should not raise or log at ERROR level.
        keewano_sdk.report_in_app_purchase("gems_100", price_usd_cents=499)
        keewano_sdk.report_in_app_purchase("gems_100", localized_price=4.99, currency_code="EUR")

    def test_ab_test_group_accepts_single_byte_chars(self):
        self._init()
        d = sdkmod._dispatcher
        got = []
        # The validated single character is forwarded to the dispatcher, which serializes it to a byte.
        d.assign_to_ab_test_group = lambda test, group: got.append((test, group))
        keewano_sdk.report_ab_test_group_assignment("exp", "A")
        keewano_sdk.report_ab_test_group_assignment("exp", "B")
        keewano_sdk.report_ab_test_group_assignment("exp", chr(0))
        keewano_sdk.report_ab_test_group_assignment("exp", chr(255))  # top of the byte range
        self.assertEqual(got, [("exp", "A"), ("exp", "B"), ("exp", chr(0)), ("exp", chr(255))])

    def test_ab_test_group_rejects_bad_group(self):
        self._init()
        d = sdkmod._dispatcher
        called = []
        d.assign_to_ab_test_group = lambda test, group: called.append(group)
        bad_values = (
            "AB",  # more than one character
            "",  # empty
            chr(256),  # code point above the single-byte limit
            "€",  # non-Latin-1 char (code point 8364)
            "\ud800",  # a lone surrogate (code point 55296)
            5,
            None,
            b"A",  # not a str at all
        )
        for bad in bad_values:
            with self.assertLogs("keewano_sdk", level="ERROR"):
                keewano_sdk.report_ab_test_group_assignment("exp", bad)
        self.assertEqual(called, [])  # nothing forwarded to the dispatcher

    def test_ad_offered_rejects_non_adtype(self):
        self._init()
        # int(ad_type) would raise on str/None; a raw int > 255 would overflow the wire byte.
        for bad in ("rewarded", None, 1, 999):
            with self.assertLogs("keewano_sdk", level="ERROR"):
                keewano_sdk.report_ad_offered("level_complete", bad)
        # The real enum works and does not error.
        keewano_sdk.report_ad_offered("level_complete", AdType.REWARDED)

    def test_log_error_rejects_non_string(self):
        self._init()
        for bad in (5, None, ["oops"]):  # .strip() would raise on a non-str
            with self.assertLogs("keewano_sdk", level="ERROR"):
                keewano_sdk.log_error(bad)


class TestUserNameTest(FacadeTestBase):
    """mark_as_test_user only accepts names that can travel as the Latin-1 K-Tester header."""

    def test_latin1_name_reaches_the_dispatcher_and_a_non_latin1_one_does_not(self):
        self._init()
        got = []
        sdkmod._dispatcher.set_test_user_name = got.append
        keewano_sdk.mark_as_test_user("qa-café")
        with self.assertLogs("keewano_sdk", level="ERROR"):
            keewano_sdk.mark_as_test_user("тест")
        self.assertEqual(got, ["qa-café"])

    def test_latin1_name_is_sent_as_the_k_tester_header(self):
        with MockIngress() as srv:
            self._init(srv)
            keewano_sdk.mark_as_test_user("qa-café")
            keewano_sdk.report_button_click("Play")
            keewano_sdk.flush()
            self.assertTrue(wait_until(lambda: any("k-tester" in h for h, _ in srv.batch_posts)))
            self.assertEqual(next(h for h, _ in srv.batch_posts if "k-tester" in h)["k-tester"], "qa-café")

    def test_rejected_name_does_not_stall_uploads(self):
        with MockIngress() as srv:
            self._init(srv)
            with self.assertLogs("keewano_sdk", level="ERROR"):
                keewano_sdk.mark_as_test_user("тест")
            keewano_sdk.report_button_click("Play")
            keewano_sdk.flush()
            self.assertTrue(wait_until(lambda: len(srv.batch_posts) >= 1))
            self.assertTrue(all("k-tester" not in h for h, _ in srv.batch_posts))


class TestUserNameControlCharsTest(FacadeTestBase):
    def test_name_with_a_line_break_is_rejected_and_uploads_still_work(self):
        with MockIngress() as srv:
            self._init(srv)
            with self.assertLogs("keewano_sdk", level="ERROR"):
                keewano_sdk.mark_as_test_user("qa\nbob")
            keewano_sdk.report_button_click("Play")
            keewano_sdk.flush()
            self.assertTrue(wait_until(lambda: len(srv.batch_posts) >= 1))
            self.assertTrue(all("k-tester" not in h for h, _ in srv.batch_posts))


class FacadeSurfaceTest(FacadeTestBase):
    def test_namespace_attributes_are_the_module_functions(self):
        self.assertIs(KeewanoSDK.report_button_click, keewano_sdk.report_button_click)
        self.assertIs(KeewanoSDK.initialize, keewano_sdk.initialize)
        self.assertIs(KeewanoSDK.set_user_consent, keewano_sdk.set_user_consent)

    def test_all_exports_are_present(self):
        for name in keewano_sdk.__all__:
            self.assertTrue(hasattr(keewano_sdk, name), f"missing export: {name}")

    def test_facade_mirrors_every_public_function(self):
        import types

        for name in keewano_sdk.__all__:
            obj = getattr(keewano_sdk, name)
            if isinstance(obj, types.FunctionType):  # the SDK's module-level report_*/set_*/etc.
                self.assertTrue(hasattr(KeewanoSDK, name), f"KeewanoSDK is missing {name}")
                self.assertIs(getattr(KeewanoSDK, name), obj, f"KeewanoSDK.{name} is not the module function")


class LifecycleAndEnvironmentTest(FacadeTestBase):
    """The lifecycle/connectivity/environment reporters that the mobile SDKs capture automatically."""

    def test_no_payload_events_forward_to_dispatcher(self):
        self._init()
        d = sdkmod._dispatcher
        calls = []
        d.report_app_pause = lambda: calls.append("pause")
        d.report_app_resume = lambda: calls.append("resume")
        d.report_internet_connected = lambda: calls.append("conn")
        d.report_internet_disconnected = lambda: calls.append("disc")
        d.report_low_memory = lambda: calls.append("lowmem")

        keewano_sdk.report_app_pause()
        keewano_sdk.report_app_resume()
        keewano_sdk.report_internet_connected()
        keewano_sdk.report_internet_disconnected()
        keewano_sdk.report_low_memory()
        self.assertEqual(calls, ["pause", "resume", "conn", "disc", "lowmem"])

    def test_string_events_validate_and_forward(self):
        self._init()
        d = sdkmod._dispatcher
        got = {}
        d.report_deep_link = lambda v: got.__setitem__("deep", v)
        d.report_scene_loaded = lambda v: got.__setitem__("load", v)
        d.report_scene_unloaded = lambda v: got.__setitem__("unload", v)
        d.report_user_country = lambda v: got.__setitem__("country", v)

        keewano_sdk.report_deep_link("myapp://open")
        keewano_sdk.report_scene_loaded("MainMenu")
        keewano_sdk.report_scene_unloaded("MainMenu")
        keewano_sdk.report_user_country("US")
        self.assertEqual(got, {"deep": "myapp://open", "load": "MainMenu", "unload": "MainMenu", "country": "US"})

    def test_deep_link_keeps_long_urls_and_caps_at_the_wire_budget(self):
        self._init()
        d = sdkmod._dispatcher
        got = []
        d.report_deep_link = lambda v: got.append(v)

        long_url = "myapp://open?" + "a" * 1000  # > the 256 label limit, but a legitimate URL
        keewano_sdk.report_deep_link(long_url)
        self.assertEqual(got, [long_url])  # kept in full, not truncated to 256

        got.clear()
        over = "myapp://" + "b" * (sdkmod.MAX_DEEP_LINK_LENGTH + 50)
        with self.assertLogs("keewano_sdk", level="WARNING"):
            keewano_sdk.report_deep_link(over)
        self.assertEqual(len(got[0]), sdkmod.MAX_DEEP_LINK_LENGTH)  # capped at the wire budget

    def test_string_events_drop_blank_and_non_string(self):
        self._init()
        d = sdkmod._dispatcher
        called = []
        for name in ("report_deep_link", "report_scene_loaded", "report_scene_unloaded", "report_user_country"):
            setattr(d, name, lambda v: called.append(v))
        for fn in (
            keewano_sdk.report_deep_link,
            keewano_sdk.report_scene_loaded,
            keewano_sdk.report_scene_unloaded,
            keewano_sdk.report_user_country,
        ):
            for bad in ("", "   ", None, 5):
                with self.assertLogs("keewano_sdk", level="ERROR"):
                    fn(bad)
        self.assertEqual(called, [])  # nothing forwarded to the dispatcher

    def test_log_error_shares_the_string_boundary_checks(self):
        self._init()
        d = sdkmod._dispatcher
        got = []
        d.log_error = lambda m: got.append(m)
        for bad in ("", "   ", None, 5, "\ud800"):
            with self.assertLogs("keewano_sdk", level="ERROR"):
                keewano_sdk.log_error(bad)
        self.assertEqual(got, [])  # nothing forwarded to the dispatcher
        # Over-long traces are clipped at the error budget, not dropped: a clipped trace beats none.
        with self.assertLogs("keewano_sdk", level="WARNING"):
            keewano_sdk.log_error("t" * (sdkmod.MAX_ERROR_LENGTH + 50))
        self.assertEqual(len(got[0]), sdkmod.MAX_ERROR_LENGTH)

    def test_unencodable_string_does_not_raise_or_touch_the_batch(self):
        # Regression: a lone surrogate used to reach KBuffer.write_string, raising UnicodeEncodeError
        # out of the public call and leaving a header-only event behind in the batch.
        self._init()
        d = sdkmod._dispatcher
        before = d._in_batch.data.length
        with self.assertLogs("keewano_sdk", level="ERROR"):
            keewano_sdk.report_button_click("\ud800")
        self.assertEqual(d._in_batch.data.length, before)  # not a single byte written

    def test_no_payload_events_emit_expected_wire_ids(self):
        # Dispatcher-level, so there is no launch burst ahead of the event: assert the first (only)
        # event's id. Pins report_* -> KEvents mapping for the newly exposed connectivity/memory events.
        import struct

        from keewano_sdk.internal import guid
        from keewano_sdk.internal.consent import UserConsentState
        from keewano_sdk.internal.dispatcher import KEventDispatcher
        from keewano_sdk.internal.events import KEvents

        def _fresh():
            return KEventDispatcher(
                working_directory=os.path.join(self._dir, "b"),
                endpoint="http://127.0.0.1:1/x",
                app_secret="tok",
                initial_consent=UserConsentState.NOT_REQUIRED,
                install_id=guid.from_uint64(1),
                initial_user_id=guid.EMPTY,
                data_session_id=guid.from_uint64(2),
                sdk_version="1.0.0",
            )

        for method, eid in (
            ("report_internet_connected", KEvents.INTERNET_CONNECTED),
            ("report_internet_disconnected", KEvents.INTERNET_DISCONNECTED),
            ("report_low_memory", KEvents.LOW_MEM_WARNING),
        ):
            d = _fresh()
            try:
                getattr(d, method)()
                body = d._in_batch.data.to_bytes()
                self.assertEqual(struct.unpack_from("<IH", body, 0)[1], eid, method)
            finally:
                d.stop()


class LaunchBurstTest(unittest.TestCase):
    """Exercises the launch burst directly, with a stub dispatcher, so the host's real environment
    does not decide which events the assertions see."""

    class _StubDispatcher:
        def __init__(self):
            self.events = []

        def add_event_str(self, event_type, s):
            self.events.append((event_type, s))

        def add_event_int(self, event_type, value):
            self.events.append((event_type, value))

    def _burst(self, **env):
        """Runs the launch burst with the named environment getters stubbed out."""
        d = self._StubDispatcher()
        originals = {name: getattr(sdkmod, name) for name in env}
        for name, value in env.items():
            setattr(sdkmod, name, lambda v=value: v)
        try:
            sdkmod._report_launch_events(d, KeewanoConfig(api_key="k", app_version="1.0.0"))
        finally:
            for name, orig in originals.items():
                setattr(sdkmod, name, orig)
        return d.events

    def test_environment_values_reported_when_detected(self):
        events = self._burst(system_language="en", os_description="Linux 6.1")
        self.assertIn((KEvents.SYSTEM_LANG, "en"), events)
        self.assertIn((KEvents.OS, "Linux 6.1"), events)

    def test_undetectable_environment_values_are_dropped(self):
        # "" is what both getters document as "could not determine"; an empty dimension value would
        # upload cleanly and become an unattributable bucket, so the event is omitted instead.
        ids = [eid for eid, _ in self._burst(system_language="", os_description="")]
        self.assertNotIn(KEvents.SYSTEM_LANG, ids)
        self.assertNotIn(KEvents.OS, ids)
        self.assertIn(KEvents.APP_LAUNCH, ids)  # the rest of the burst is unaffected


class ValidationHelpersTest(unittest.TestCase):
    """Directly exercises the boundary validators the report_* functions delegate to."""

    def test_name_rejects_blank_and_truncates_long(self):
        self.assertIsNone(sdkmod._name("", "p", "c"))
        self.assertIsNone(sdkmod._name("   ", "p", "c"))
        self.assertIsNone(sdkmod._name(None, "p", "c"))
        self.assertEqual(sdkmod._name("ok", "p", "c"), "ok")
        long = "x" * (sdkmod.MAX_STRING_LENGTH + 50)
        self.assertEqual(len(sdkmod._name(long, "p", "c")), sdkmod.MAX_STRING_LENGTH)

    def test_name_rejects_unencodable_strings(self):
        # An unpaired surrogate (as surrogateescape produces from OS-supplied text) is non-blank and
        # short, so only the UTF-8 check catches it; KBuffer.write_string would otherwise raise after
        # the event header was already written into the batch.
        self.assertIsNone(sdkmod._name("\ud800", "p", "c"))
        self.assertIsNone(sdkmod._name("ok\udfff", "p", "c"))
        # Valid non-BMP text is a single code point per char and must still pass.
        self.assertEqual(sdkmod._name("\U0001f600", "p", "c"), "\U0001f600")
        # A surrogate past the truncation point is cut away, so the value is usable.
        self.assertEqual(
            sdkmod._name("y" * sdkmod.MAX_STRING_LENGTH + "\ud800", "p", "c"),
            "y" * sdkmod.MAX_STRING_LENGTH,
        )

    def test_validate_test_user_passes_none_and_latin1_names(self):
        self.assertIsNone(sdkmod._validate_test_user(None, "c"))  # "no test user" stays that way
        for name in ("qa-alice", "café", "Ünïcödé", chr(0xFF)):  # Latin-1 covers U+0000..U+00FF
            with self.subTest(name=name):
                self.assertEqual(sdkmod._validate_test_user(name, "c"), name)

    def test_validate_test_user_rejects_names_outside_latin1(self):
        # It travels as the K-Tester header, which http.client encodes as Latin-1: one of these would
        # make every upload fail with UnicodeEncodeError.
        for name in ("тест", "qa€", "测试", "\U0001f600"):
            with self.subTest(name=name):
                with self.assertLogs("keewano_sdk", level="ERROR") as cm:
                    self.assertIsNone(sdkmod._validate_test_user(name, "my_caller"))
                self.assertTrue(any("my_caller" in m and "latin-1" in m for m in cm.output))

    def test_validate_test_user_rejects_control_characters(self):
        # http.client refuses a header value with a line break (it would inject a header), and the
        # transport would report that as an unreachable ingress, stalling every upload.
        for name in ("qa\nbob", "qa\rbob", "qa\r\nX-Evil: 1", "qa\x00", "qa\x7f", "\x1b[31mqa"):
            with self.subTest(name=name):
                with self.assertLogs("keewano_sdk", level="ERROR") as cm:
                    self.assertIsNone(sdkmod._validate_test_user(name, "my_caller"))
                self.assertTrue(any("my_caller" in m and "control character" in m for m in cm.output))

    def test_validate_test_user_allows_a_tab(self):
        # RFC 9110 allows HTAB in a header value, and http.client sends it.
        self.assertEqual(sdkmod._validate_test_user("qa\tbob", "c"), "qa\tbob")

    def test_count_rejects_negative_and_over_uint32(self):
        self.assertTrue(sdkmod._count(0, "p", "c"))
        self.assertTrue(sdkmod._count(sdkmod.MAX_UINT32, "p", "c"))
        self.assertFalse(sdkmod._count(-1, "p", "c"))
        self.assertFalse(sdkmod._count(sdkmod.MAX_UINT32 + 1, "p", "c"))
        self.assertFalse(sdkmod._count(1.5, "p", "c"))  # float would raise at the wire writer
        self.assertFalse(sdkmod._count(True, "p", "c"))  # bool is not a valid count
        self.assertFalse(sdkmod._count("3", "p", "c"))

    def test_count16_rejects_negative_and_over_uint16(self):
        self.assertTrue(sdkmod._count16(0, "p", "c"))
        self.assertTrue(sdkmod._count16(sdkmod.MAX_UINT16, "p", "c"))
        self.assertFalse(sdkmod._count16(-1, "p", "c"))
        self.assertFalse(sdkmod._count16(sdkmod.MAX_UINT16 + 1, "p", "c"))

    def test_int32_accepts_range_and_rejects_out_of_range_and_non_ints(self):
        self.assertTrue(sdkmod._int32(0, "p", "c"))
        self.assertTrue(sdkmod._int32(sdkmod.MIN_INT32, "p", "c"))
        self.assertTrue(sdkmod._int32(sdkmod.MAX_INT32, "p", "c"))
        self.assertTrue(sdkmod._int32(-5, "p", "c"))
        self.assertFalse(sdkmod._int32(sdkmod.MIN_INT32 - 1, "p", "c"))
        self.assertFalse(sdkmod._int32(sdkmod.MAX_INT32 + 1, "p", "c"))
        self.assertFalse(sdkmod._int32(1.5, "p", "c"))  # float would raise at the wire writer
        self.assertFalse(sdkmod._int32(True, "p", "c"))  # bool is not a valid int payload
        self.assertFalse(sdkmod._int32("3", "p", "c"))

    def test_amount_rejects_nan_inf_negative(self):
        self.assertTrue(sdkmod._amount(0.0, "p", "c"))
        self.assertTrue(sdkmod._amount(4.99, "p", "c"))
        self.assertTrue(sdkmod._amount(5, "p", "c"))  # int serializes cleanly as a float
        self.assertFalse(sdkmod._amount(-1.0, "p", "c"))
        self.assertFalse(sdkmod._amount(float("nan"), "p", "c"))
        self.assertFalse(sdkmod._amount(float("inf"), "p", "c"))

    def test_amount_rejects_non_numbers_and_bool(self):
        # A non-number would raise inside math.isfinite and crash the report call; bool must not
        # slip through as 1.0 (bool is an int subclass, so the isinstance(int) check can't exclude it).
        self.assertFalse(sdkmod._amount("4.99", "p", "c"))
        self.assertFalse(sdkmod._amount(None, "p", "c"))
        self.assertFalse(sdkmod._amount(True, "p", "c"))

    def test_amount_rejects_values_beyond_float32(self):
        # A Python float is a 64-bit double; a finite value out of 32-bit float range passes isfinite
        # but overflows struct.pack('<f', ...). It must be dropped, not reach the wire writer.
        self.assertFalse(sdkmod._fits_float32(1e300))
        self.assertFalse(sdkmod._fits_float32(3.5e38))
        self.assertTrue(sdkmod._fits_float32(3.0e38))  # inside float32 range
        self.assertFalse(sdkmod._amount(1e300, "p", "c"))
        self.assertFalse(sdkmod._amount(10**40, "p", "c"))  # an int too large to pack as float32
        # An int beyond double range makes math.isfinite() itself raise; the float32 check must run
        # first so _amount drops it rather than crashing.
        self.assertFalse(sdkmod._fits_float32(10**400))
        self.assertFalse(sdkmod._amount(10**400, "p", "c"))

    def test_items_returns_validated_list_or_none(self):
        from keewano_sdk import Item
        from keewano_sdk.internal.dispatcher import MAX_ITEMS_PER_EVENT

        # Valid: returns the list to use. None input is a valid empty list (one-sided transaction).
        self.assertEqual(sdkmod._items([Item("gold", 3)], "items", "c"), [Item("gold", 3)])
        self.assertEqual(sdkmod._items(None, "items", "c"), [])
        self.assertEqual(sdkmod._items([], "items", "c"), [])
        # Dropped (returns None):
        self.assertIsNone(sdkmod._items([Item("", 1)], "items", "c"))  # blank name
        self.assertIsNone(sdkmod._items([Item("gold", -1)], "items", "c"))  # bad count
        self.assertIsNone(sdkmod._items([Item("x" * 300, 1)], "items", "c"))  # over-long name
        too_many = [Item(f"i{i}", 1) for i in range(MAX_ITEMS_PER_EVENT + 1)]
        self.assertIsNone(sdkmod._items(too_many, "items", "c"))

    def test_items_rejects_non_iterable_and_non_item_entries(self):
        from keewano_sdk import Item

        self.assertIsNone(sdkmod._items(5, "items", "c"))  # non-iterable: list() would raise
        self.assertIsNone(sdkmod._items("gold", "items", "c"))  # str iterates to chars, not Items
        self.assertIsNone(sdkmod._items([Item("gold", 1), "x"], "items", "c"))  # a non-Item entry


if __name__ == "__main__":
    unittest.main()
