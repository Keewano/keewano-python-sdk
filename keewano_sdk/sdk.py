"""Public entry point for the Keewano native Python SDK.

This is the Python analogue of the Keewano SDKs and exposes the same reporting API. Call
:func:`initialize` once at startup; all reporting methods are safe to call before initialization
completes — they are simply dropped with a warning. :func:`initialize` never raises: an analytics
SDK must not be able to crash its host, so any failure disables the SDK for the process instead.

Both a namespaced facade (``KeewanoSDK.report_button_click(...)``) and module-level functions
(``keewano_sdk.report_button_click(...)``) are provided; they are the same callables.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import struct
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from . import __version__
from .ad_type import AdType
from .config import KeewanoConfig
from .item import Item
from .internal import guid, paths
from .internal.consent import UserConsentState
from .internal.dispatcher import MAX_ITEMS_PER_EVENT, KEventDispatcher
from .internal.environment import (
    device_type,
    os_description,
    platform_name,
    ram_size_mb,
    system_language,
)
from .internal.events import KEvents
from .internal.guid import KGuid
from .internal.storage import KStorage, UserIdentifiers

_log = logging.getLogger("keewano_sdk")

_init_lock = threading.Lock()
_dispatcher: Optional[KEventDispatcher] = None
_storage: Optional[KStorage] = None
_user_identifiers: Optional[UserIdentifiers] = None
# Exception-hook bookkeeping, so shutdown() can cleanly restore what was there before init(). We keep
# both the previous hooks (to chain to and to restore) and references to the ones we installed (so we
# only restore when ours is still the current hook, and don't clobber a hook a third party set after).
_prev_excepthook = None
_prev_thread_excepthook = None
_installed_excepthook = None
_installed_thread_excepthook = None


# --------------------------------------------------------------------------------------------
# Initialization
# --------------------------------------------------------------------------------------------


def initialize(config: KeewanoConfig) -> None:
    """Initializes the SDK. Idempotent — subsequent calls are ignored. Never raises: any failure is
    logged and leaves the SDK disabled for this process."""
    global _dispatcher
    with _init_lock:
        if _dispatcher is not None:
            return
        try:
            _initialize_unsafe(config)
        except Exception:
            _log.exception("Initialization failed; the SDK is disabled for this process.")
            if _dispatcher:
                _dispatcher.stop()
            _dispatcher = None


def _initialize_unsafe(config: KeewanoConfig) -> None:
    global _dispatcher, _storage, _user_identifiers

    if not config.api_key or not config.api_key.strip():
        # Starting up with no key would collect, persist and upload events with an empty token
        # forever: the ingress keeps auth failures retryable, so nothing would ever be ACKed and the
        # queue would grow to the disk cap and be collapsed into drop markers. A no-op SDK is right.
        _log.error("No API key was provided; the SDK will not start.")
        return

    base_dir = config.data_dir or paths.default_data_dir(config.api_key)
    store = KStorage(base_dir)

    ids = store.load_or_init_identifiers()
    if ids is None:
        # The identifiers file exists but could not be read. Minting a replacement would
        # permanently destroy this install's identity, so sit this launch out instead.
        _log.error("Could not load the install identifiers; the SDK will not start.")
        return
    _user_identifiers = ids
    _storage = store

    consent = _resolve_consent(store, config)

    work_dir = os.path.join(base_dir, "batches")
    d = KEventDispatcher(
        working_directory=work_dir,
        endpoint=config.endpoint,
        app_secret=config.api_key,
        initial_consent=consent,
        install_id=ids.install_id,
        initial_user_id=ids.user_id,
        data_session_id=guid.new_guid(),
        sdk_version=__version__,
        proxy_auth_bearer=config.proxy_auth_bearer,
        custom_event_set=config.custom_event_set,
    )
    _dispatcher = d

    _report_launch_events(d, config)

    if not config.disable_exception_tracking:
        _install_exception_hooks()

    atexit.register(shutdown)


def _resolve_consent(store: KStorage, config: KeewanoConfig) -> UserConsentState:
    """Reconciles the persisted consent decision with what the app declares in its config. The
    persisted value wins for anything the *user* decided, but must not outrank a change of the app's
    own intent (e.g. an app that shipped with consent off and later turned it on → Pending)."""
    persisted = store.load_user_consent_state()
    if persisted is None:
        resolved = UserConsentState.PENDING if config.require_user_consent else UserConsentState.NOT_REQUIRED
    elif config.require_user_consent and persisted == UserConsentState.NOT_REQUIRED:
        # The app now requires consent but this install was never asked: close the gate.
        resolved = UserConsentState.PENDING
    elif not config.require_user_consent and persisted == UserConsentState.PENDING:
        # The app no longer requires consent, and the user was never asked either way.
        resolved = UserConsentState.NOT_REQUIRED
    else:
        resolved = persisted
    if resolved != persisted:
        store.save_user_consent_state(resolved)
    return resolved


def _report_launch_events(d: KEventDispatcher, config: KeewanoConfig) -> None:
    if config.app_version:
        d.add_event_str(KEvents.APP_LAUNCH, config.app_version)
    else:
        d.add_event_str(KEvents.APP_LAUNCH, "undefined")  # still mark the launch; version simply unknown
    d.add_event_str(KEvents.PLATFORM, platform_name())
    d.add_event_str(KEvents.DEVICE_TYPE, device_type())
    # The OS and language getters report "" when the host exposes nothing usable. Dropped rather than
    # sent: an empty dimension value uploads cleanly and becomes an unattributable bucket in the
    # breakdown it segments, which is worse than the event simply being absent.
    os_name = os_description()
    if os_name:
        d.add_event_str(KEvents.OS, os_name)
    ram = ram_size_mb()
    if ram > 0:
        d.add_event_int(KEvents.RAM_SIZE, ram)
    lang = system_language()
    if lang:
        d.add_event_str(KEvents.SYSTEM_LANG, lang)


#: Top-level package name; frames from it mark an exception as the SDK's own fault.
_SDK_PACKAGE = "keewano_sdk"


def _is_our_own_fault(exc_tb) -> bool:
    """True if the exception was raised from inside the SDK rather than the host app. The
    exception-hook is process-wide, so it also sees the SDK's own bugs — and billing those to the
    customer's analytics project would be wrong. Matches the Android SDK: attribute to the *deepest*
    frame (where it was actually raised), by module name. Under-reporting is the safe direction."""
    if exc_tb is None:
        return False
    tb = exc_tb
    while tb.tb_next is not None:  # walk to the innermost frame — where the exception was raised
        tb = tb.tb_next
    module = tb.tb_frame.f_globals.get("__name__", "")
    return module == _SDK_PACKAGE or module.startswith(_SDK_PACKAGE + ".")


def _install_exception_hooks() -> None:
    global _prev_excepthook, _prev_thread_excepthook, _installed_excepthook, _installed_thread_excepthook
    _prev_excepthook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        try:
            _handle_uncaught(exc_type, exc_value, exc_tb)
        finally:
            if _prev_excepthook is not None:
                _prev_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook
    _installed_excepthook = _hook

    # Also capture exceptions raised in non-main threads (Python 3.8+).
    if hasattr(threading, "excepthook"):
        _prev_thread_excepthook = threading.excepthook

        def _thread_hook(args):
            try:
                _handle_uncaught(args.exc_type, args.exc_value, args.exc_traceback)
            finally:
                if _prev_thread_excepthook is not None:
                    _prev_thread_excepthook(args)

        threading.excepthook = _thread_hook
        _installed_thread_excepthook = _thread_hook


def _handle_uncaught(exc_type, exc_value, exc_tb) -> None:
    """Shared body of both exception hooks: report the crash unless it is the SDK's own fault."""
    # Persist synchronously on this (dying) thread. send_now only signals the send thread, which would
    # have to win a race against process death — so the crash report was the event least likely to
    # survive. Uploading can wait for the next launch; the write can't.
    d = _dispatcher

    # KeyboardInterrupt (Ctrl-C), SystemExit and GeneratorExit are normal control-flow / termination
    # signals, not crashes — they derive from BaseException but not Exception. sys.excepthook is still
    # invoked for an uncaught KeyboardInterrupt, and our threading hook would otherwise report a
    # worker thread's SystemExit that the default hook silently ignores. Don't bill either as an
    # error; the caller still chains, so the interpreter's own handling is unaffected.
    if not issubclass(exc_type, Exception):
        if d is not None:
            d.persist_now()
        return
    if _is_our_own_fault(exc_tb):
        # Don't report our own crash as if it were an app error. There is no SDK-internal telemetry
        # route yet, so it is dropped here (still chained by the caller, so nothing is swallowed).
        _log.error("Keewano SDK crashed; not reporting it as an app error.", exc_info=(exc_type, exc_value, exc_tb))
        if d is not None:
            d.persist_now()
        return
    # The full traceback (type, message, and frames), not just the type/message — log_error clips it
    # at MAX_ERROR_LENGTH, which is sized for exactly this. Frames are what localize a crash.
    log_error("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
    if d is not None:
        d.persist_now()


def _uninstall_exception_hooks() -> None:
    """Restores the exception hooks that were in place before :func:`_install_exception_hooks`, so an
    init → shutdown → init cycle doesn't leave the SDK's hooks installed (or stack them)."""
    global _prev_excepthook, _prev_thread_excepthook, _installed_excepthook, _installed_thread_excepthook

    # Only restore if our hook is still the current one. If a third party installed a hook after us,
    # ours is no longer at the top and we can't cleanly splice out of the middle of their chain —
    # leave theirs in place rather than clobber it; we simply detach our bookkeeping below.
    if _installed_excepthook is not None and sys.excepthook is _installed_excepthook:
        sys.excepthook = _prev_excepthook if _prev_excepthook is not None else sys.__excepthook__
    if (
        _installed_thread_excepthook is not None
        and hasattr(threading, "excepthook")
        and threading.excepthook is _installed_thread_excepthook
        and _prev_thread_excepthook is not None
    ):
        threading.excepthook = _prev_thread_excepthook

    _prev_excepthook = None
    _prev_thread_excepthook = None
    _installed_excepthook = None
    _installed_thread_excepthook = None


def shutdown() -> None:
    """Flushes and stops the background sender. Called automatically at interpreter exit."""
    global _dispatcher, _user_identifiers, _storage
    with _init_lock:
        d = _dispatcher
        if d is None:
            return
        d.stop()
        # stop()'s join is bounded (2 s), but the send thread may be parked in a ~30 s upload when we
        # cancel — so its own final RAM→disk flush might not run before this daemon thread is torn
        # down at interpreter exit. Persist synchronously here so the last collected batch is never
        # lost. Cheap no-op when the send thread already flushed; serialized with it via _flush_lock.
        d.persist_now()
        # Restore the process's exception hooks so a later init() starts from a clean slate (nulling
        # _prev_excepthook alone would leave our hooks installed and make the next init stack on them).
        _uninstall_exception_hooks()
        _dispatcher = None
        _user_identifiers = None
        _storage = None
        atexit.unregister(shutdown)


def _with_dispatcher(fn) -> None:
    d = _dispatcher
    if d is None:
        _log.warning("SDK not initialized; event dropped. Call keewano_sdk.initialize() first.")
        return
    fn(d)


# --------------------------------------------------------------------------------------------
# Argument validation
#
# Everything the app hands us is checked here, once, at the boundary — not in the wire primitives,
# which are on the hot path and reached from inside locks, so a raise down there would propagate out
# of a public report_* call and crash the host. Three failure shapes are prevented: numbers outside
# the uint32 the wire carries (a negative amount silently masked into billions), blank strings (which
# upload cleanly and become an unattributable bucket in the dimension the event segments by), and
# strings the UTF-8 writer cannot encode (which raise mid-event and corrupt the batch).
# --------------------------------------------------------------------------------------------

#: Longest dimension value we put on the wire; longer values are truncated, not dropped.
MAX_STRING_LENGTH = 256
#: Error messages carry stack traces, so they get a far larger budget — and are never dropped.
MAX_ERROR_LENGTH = 8 * 1024
#: Deep links are URLs (content, not a short label), so they get the full wire budget rather than the
#: 256-char dimension limit — a truncated URL is far less useful than a long one.
MAX_DEEP_LINK_LENGTH = 8 * 1024
#: Largest value the wire format's uint32 fields can carry.
MAX_UINT32 = 0xFFFFFFFF
#: Largest value the wire format's uint16 fields (e.g. the two halves of a ushort pair) can carry.
MAX_UINT16 = 0xFFFF
#: Largest value a single wire byte (e.g. an A/B test group) can carry.
MAX_UINT8 = 0xFF
#: Bounds of the wire format's signed int32 fields (e.g. a custom-event ``int`` payload).
MIN_INT32 = -0x80000000
MAX_INT32 = 0x7FFFFFFF


#: Characters an HTTP header value cannot carry (RFC 9110 allows HTAB, SP, visible ASCII and bytes
#: 0x80-0xFF): the other C0 controls, including CR and LF, and DEL.
_HEADER_CONTROL_CHARS = re.compile("[\x00-\x08\x0a-\x1f\x7f]")


def _validate_test_user(test_user_name: Optional[str], caller: str) -> Optional[str]:
    """Validates that the test user name can travel as the K-Tester header, returning it, or None
    (logged) if it cannot. Either failure would otherwise break every upload, not just the header:
    the transport treats the error as an unreachable ingress, so batches would be retried forever.

    * It must be encodable as latin-1, since http.client encodes str header values as latin-1. So
      test_user_name="тест" raises UnicodeEncodeError.
    * It must not contain control characters other than a tab. http.client rejects a value with a
      line break (which would otherwise inject a header), and proxies may reject the other controls.

    It is assumed that this is called after test_user_name has gone through _name validation."""
    if test_user_name is None:
        return None
    try:
        test_user_name.encode("latin-1")
    except UnicodeEncodeError:
        _log.error("%s: test_user_name is not encodable as latin-1; ignoring it.", caller)
        return None
    if _HEADER_CONTROL_CHARS.search(test_user_name):
        _log.error(
            "%s: test_user_name contains a control character (e.g. a line break), which an HTTP header "
            "cannot carry; ignoring it.",
            caller,
        )
        return None
    return test_user_name


def _name(value: Optional[str], param: str, caller: str, max_len: int = MAX_STRING_LENGTH) -> Optional[str]:
    """Validates a dimension value. Blank → dropped (None); over-long → truncated to ``max_len``
    (defaults to :data:`MAX_STRING_LENGTH`); not UTF-8 encodable → dropped (None). Returns the value to
    use, or None to drop the event."""
    if not isinstance(value, str):
        _log.error("%s: '%s' must be a string, got %s; event dropped.", caller, param, type(value).__name__)
        return None
    if not value.strip():
        _log.error("%s: '%s' must not be empty or blank; event dropped.", caller, param)
        return None
    if len(value) > max_len:
        _log.warning("%s: '%s' is %d chars; truncated to %d.", caller, param, len(value), max_len)
        value = value[:max_len]
    # Checked last, on exactly the string that would reach the wire (truncation only ever removes
    # code points, so it cannot make an encodable value unencodable). KBuffer.write_string encodes
    # to UTF-8, which raises on an unpaired surrogate — as produced by surrogateescape decoding of
    # OS-supplied text (os.listdir, sys.argv, os.environ). That raise would happen inside the
    # dispatcher's lock, after the event header is already appended to the batch: it would crash the
    # host and leave a headerless, half-written event that misparses everything after it.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _log.error("%s: '%s' is not encodable as UTF-8 (unpaired surrogate?); event dropped.", caller, param)
        return None
    return value


def _count(value: int, param: str, caller: str) -> bool:
    """True if ``value`` fits the uint32 the wire format carries for this parameter. Rejects non-ints
    (and bool): a float would raise at the wire writer, and bool is not a valid count."""
    if not isinstance(value, int) or isinstance(value, bool):
        _log.error("%s: '%s' must be an int, got %s; event dropped.", caller, param, type(value).__name__)
        return False
    if value < 0:
        _log.error("%s: '%s' is negative (%s); event dropped. Amounts are unsigned.", caller, param, value)
        return False
    if value > MAX_UINT32:
        _log.error("%s: '%s' is %s, above the %s wire limit; event dropped.", caller, param, value, MAX_UINT32)
        return False
    return True


def _count16(value: int, param: str, caller: str) -> bool:
    """True if ``value`` fits the uint16 the wire format carries for this parameter."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > MAX_UINT16:
        _log.error("%s: '%s' is %s, outside the 0..%s uint16 range; event dropped.", caller, param, value, MAX_UINT16)
        return False
    return True


def _int32(value: int, param: str, caller: str) -> bool:
    """True if ``value`` fits the signed int32 the wire format carries for this parameter. Rejects
    non-ints (and bool) as well as out-of-range values: the wire writer would otherwise mask an
    out-of-range int into a different number, or raise on a float — both worse than dropping."""
    if not isinstance(value, int) or isinstance(value, bool) or value < MIN_INT32 or value > MAX_INT32:
        _log.error(
            "%s: '%s' is %s, outside the %s..%s int32 range; event dropped.", caller, param, value, MIN_INT32, MAX_INT32
        )
        return False
    return True


def _fits_float32(value: float) -> bool:
    """True if ``value`` can be written as the wire's 32-bit float. A Python ``float`` is a 64-bit
    double, so a finite value like ``1e300`` is out of float32 range and would raise ``OverflowError``
    at the wire writer (``struct.pack('<f', ...)``) — crashing the report call. Uses the exact same
    pack so it can never disagree with the writer."""
    try:
        struct.pack("<f", value)
        return True
    except Exception:
        return False


def _amount(value: float, param: str, caller: str) -> bool:
    """True if ``value`` is a finite, non-negative amount that fits a 32-bit wire float. Rejects
    non-numbers (and bool) before the finiteness check, which would otherwise raise on a str/None and
    crash the report call. An int is accepted — it serializes cleanly as a float. NaN/Infinity
    serialize to garbage, and a magnitude beyond float32 overflows the writer, so both are dropped."""
    import math

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        _log.error("%s: '%s' must be a number, got %s; event dropped.", caller, param, type(value).__name__)
        return False
    # Check float32-fit BEFORE math.isfinite: isfinite() raises OverflowError on an int too large to
    # convert to a double (e.g. 10**400), whereas _fits_float32 handles any value without raising.
    # struct.pack does not reject NaN/Infinity, so the isfinite check below still has work to do.
    if not _fits_float32(value):
        _log.error("%s: '%s' is %s, too large for a 32-bit float; event dropped.", caller, param, value)
        return False
    if not math.isfinite(value) or value < 0:
        _log.error("%s: '%s' is %s; event dropped. Expected a finite, non-negative amount.", caller, param, value)
        return False
    return True


def _items(items: Optional[Sequence[Item]], param: str, caller: str) -> Optional[List[Item]]:
    """Validates and coerces an item list, returning the list to use — or ``None`` if the event
    should be dropped. ``None`` input is a valid empty list (a one-sided transaction). One bad entry
    drops the whole event: a half-written exchange corrupts economy aggregation worse than a missing
    one does. Over-long item names drop (not truncate)."""
    if items is None:
        return []
    # Must be a real sequence of Item: a non-iterable (e.g. an int) would raise in list(), and a
    # str would iterate into characters whose .name access then raises — both crash the report call.
    if not isinstance(items, (list, tuple)):
        _log.error("%s: '%s' must be a list of Item, got %s; event dropped.", caller, param, type(items).__name__)
        return None
    if len(items) > MAX_ITEMS_PER_EVENT:
        _log.error(
            "%s: '%s' has %d entries, over the %d limit; event dropped.", caller, param, len(items), MAX_ITEMS_PER_EVENT
        )
        return None
    for item in items:
        if not isinstance(item, Item):
            _log.error("%s: '%s' contains a non-Item entry (%s); event dropped.", caller, param, type(item).__name__)
            return None
        checked = _name(item.name, f"{param}[{item.name}]", caller)
        if checked is None or len(item.name) != len(checked):
            _log.error("%s: '%s' contains an item with a long or blank name; event dropped.", caller, param)
            return None
        if not _count(item.count, f"{param}[{item.name}].count", caller):
            return None
    return list(items)


def _revenue(
    name: Optional[str],
    name_param: str,
    usd_cents: Optional[int],
    usd_param: str,
    localized: Optional[float],
    localized_param: str,
    currency_code: Optional[str],
    caller: str,
):
    """Validates the "name + (US cents | localized amount + currency)" shape shared by purchases, ad
    revenue and subscription revenue. Returns ``(name, cents)`` or ``(name, amount, currency)`` —
    the payload for the USD or local-currency encoder respectively — or None to drop the event."""
    checked = _name(name, name_param, caller)
    if checked is None:
        return None
    if usd_cents is not None:
        if not _count(usd_cents, usd_param, caller):
            return None
        return (checked, usd_cents)
    if localized is not None and currency_code is not None:
        currency = _name(currency_code, "currency_code", caller)
        if currency is None or not _amount(localized, localized_param, caller):
            return None
        return (checked, localized, currency)
    _log.error("%s: provide %s or (%s and currency_code).", caller, usd_param, localized_param)
    return None


def _ab_group(group: str, caller: str) -> bool:
    """True if ``group`` is a single character whose code point fits the one wire byte."""
    # Type-check before len()/ord(): a non-str raises in both.
    if not isinstance(group, str):
        _log.error("%s: 'group' must be a string, got %s; event dropped.", caller, type(group).__name__)
        return False
    if len(group) != 1:
        _log.error("%s: 'group' must be a single character; event dropped.", caller)
        return False
    if ord(group) > MAX_UINT8:
        # e.g. a non-Latin-1 char or a lone surrogate: it can't be represented as one wire byte.
        _log.error(
            "%s: 'group' %r has code point %d, above the %d single-byte limit; event dropped.",
            caller,
            group,
            ord(group),
            MAX_UINT8,
        )
        return False
    return True


# --------------------------------------------------------------------------------------------
# Privacy & consent
# --------------------------------------------------------------------------------------------


def set_user_consent(consent_given: bool) -> None:
    """Sets the user's consent for data collection (GDPR/CCPA). When "require consent" is enabled,
    data is buffered until this is called with ``True``; calling with ``False`` discards buffered
    data and stops collection. Withdrawal is honoured from any state."""

    def _do(d: KEventDispatcher) -> None:
        # Strict bool: the dispatcher decides on truthiness, so a truthy non-bool (e.g. the string
        # "false") would silently GRANT consent — a privacy decision must never be coerced. Reject
        # and leave the current state untouched.
        if not isinstance(consent_given, bool):
            _log.error(
                "set_user_consent: 'consent_given' must be a bool, got %s; call ignored (consent unchanged).",
                type(consent_given).__name__,
            )
            return
        state = d.set_user_consent(consent_given)
        if _storage is not None:
            _storage.save_user_consent_state(state)

    _with_dispatcher(_do)


def report_user_registered_before_sdk_integration(original_registration_time: datetime) -> None:
    """Reports the original registration date for users who existed before SDK integration, so
    veteran users are not counted as new. Only effective once per installation."""

    def _do(d: KEventDispatcher) -> None:
        # Reject non-datetimes up front: attribute access on the value below would otherwise raise
        # (e.g. AttributeError on an int/str/None) straight out of this call and crash the host.
        if not isinstance(original_registration_time, datetime):
            _log.error(
                "report_user_registered_before_sdk_integration: 'original_registration_time' must be a datetime, "
                "got %s; call ignored.",
                type(original_registration_time).__name__,
            )
            return
        store = _storage
        if store is None or store.has_pre_sdk_registration_been_reported():
            return
        dt = original_registration_time
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            seconds = int(dt.timestamp())
        except (OverflowError, OSError, ValueError) as exc:
            _log.error(
                "report_user_registered_before_sdk_integration: '%s' cannot be converted to a timestamp (%s); "
                "call ignored.",
                dt,
                exc,
            )
            return
        if seconds >= int(time.time()):
            _log.warning("report_user_registered_before_sdk_integration: date must be in the past. Call ignored.")
            return
        # Report first, persist synchronously, then latch. Latching first meant a process death in
        # between lost the event permanently while the guard refused to ever send it again; this
        # ordering can at worst repeat the event after an unlucky crash, which is harmless.
        d.report_pre_sdk_registration_date(seconds)
        d.persist_now()
        store.mark_pre_sdk_registration_as_reported()

    _with_dispatcher(_do)


# --------------------------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------------------------


def get_install_id() -> Optional[str]:
    """The unique, anonymous installation ID, or None if the SDK is not initialized (or the
    identifiers could not be read). Served from the copy loaded during :func:`initialize`, so it does
    no file I/O on the caller's thread and never re-mints on a transient error."""
    ids = _user_identifiers
    return str(ids.install_id) if ids is not None else None


def set_user_id(uid) -> None:
    """Associates an app user id with this installation. Accepts a 64-bit ``int`` or a UUID/GUID
    ``str``. Assign only once per installation."""

    def _do(d: KEventDispatcher) -> None:
        if isinstance(uid, bool) or not isinstance(uid, (int, str)):
            _log.error("set_user_id: expected int or str, got %s.", type(uid).__name__)
            return
        if isinstance(uid, int):
            if uid <= 0:
                _log.error(f"set_user_id: expected int uid > 0 but got {uid}.")
                return
            try:
                g = guid.from_uint64(uid)
            except ValueError:
                _log.error(f"set_user_id: uid ({uid}) is not a valid uint64.")
                return
        else:
            try:
                g = guid.from_string(uid)
            except ValueError:
                _log.error("set_user_id: '%s' is not a valid GUID string.", uid)
                return
        _persist_and_set_user_id(g, d)

    _with_dispatcher(_do)


def _persist_and_set_user_id(g: KGuid, d: KEventDispatcher) -> None:
    # Mutate the cached identifiers and persist; never reload from disk (which risks re-minting on a
    # transient read error). If the cache is missing, init did not succeed, so give up.
    ids = _user_identifiers
    if ids is None:
        return
    ids.user_id = g
    if _storage is not None:
        _storage.save_identifiers(ids)
    d.set_user_id(g)


# --------------------------------------------------------------------------------------------
# UI events
# --------------------------------------------------------------------------------------------


def report_button_click(button_name: str) -> None:
    """Reports a button/interactive-element click."""

    def _do(d: KEventDispatcher) -> None:
        checked = _name(button_name, "button_name", "report_button_click")
        if checked is not None:
            d.report_button_click(checked)

    _with_dispatcher(_do)


def report_window_open(window_name: str) -> None:
    """Reports a window/popup open event."""

    def _do(d: KEventDispatcher) -> None:
        checked = _name(window_name, "window_name", "report_window_open")
        if checked is not None:
            d.report_window_open(checked)

    _with_dispatcher(_do)


def report_window_close(window_name: str) -> None:
    """Reports a window/popup close event."""

    def _do(d: KEventDispatcher) -> None:
        checked = _name(window_name, "window_name", "report_window_close")
        if checked is not None:
            d.report_window_close(checked)

    _with_dispatcher(_do)


# --------------------------------------------------------------------------------------------
# Monetization: in-app purchases
# --------------------------------------------------------------------------------------------


def report_in_app_purchase(
    product_name: str,
    price_usd_cents: Optional[int] = None,
    localized_price: Optional[float] = None,
    currency_code: Optional[str] = None,
) -> None:
    """Reports a validated in-app purchase. Call ONLY after server validation. Provide either
    ``price_usd_cents`` (US cents) or both ``localized_price`` and ``currency_code`` (ISO 4217)."""

    def _do(d: KEventDispatcher) -> None:
        args = _revenue(
            product_name,
            "product_name",
            price_usd_cents,
            "price_usd_cents",
            localized_price,
            "localized_price",
            currency_code,
            "report_in_app_purchase",
        )
        if args is None:
            return
        if len(args) == 2:
            d.report_in_app_purchase_usd(*args)
        else:
            d.report_in_app_purchase_local(*args)

    _with_dispatcher(_do)


def report_in_app_purchase_items_granted(product_name: str, items: Optional[Sequence[Item]] = None) -> None:
    """Reports virtual items granted from a validated in-app purchase."""

    def _do(d: KEventDispatcher) -> None:
        product = _name(product_name, "product_name", "report_in_app_purchase_items_granted")
        granted = _items(items, "items", "report_in_app_purchase_items_granted")
        if product is None or granted is None:
            return
        d.report_in_app_purchase_items_granted(product, granted)

    _with_dispatcher(_do)


# --------------------------------------------------------------------------------------------
# Monetization: ads
# --------------------------------------------------------------------------------------------


def report_ad_offered(placement: str, ad_type: AdType) -> None:
    """Reports that an ad opportunity was offered to the user."""

    def _do(d: KEventDispatcher) -> None:
        where = _name(placement, "placement", "report_ad_offered")
        if where is None:
            return
        # Strict AdType: int(ad_type) would raise on a str/None, and a raw int outside 0..255 would
        # overflow the single wire byte. Requiring the enum keeps only defined values on the wire.
        if not isinstance(ad_type, AdType):
            _log.error("report_ad_offered: 'ad_type' must be an AdType, got %s; event dropped.", type(ad_type).__name__)
            return
        d.report_ad_offered(where, int(ad_type))

    _with_dispatcher(_do)


def report_ad_revenue(
    placement: str,
    revenue_usd_cents: Optional[int] = None,
    localized_revenue: Optional[float] = None,
    currency_code: Optional[str] = None,
) -> None:
    """Reports ad revenue. Provide either ``revenue_usd_cents`` or both ``localized_revenue`` and
    ``currency_code`` (ISO 4217)."""

    def _do(d: KEventDispatcher) -> None:
        args = _revenue(
            placement,
            "placement",
            revenue_usd_cents,
            "revenue_usd_cents",
            localized_revenue,
            "localized_revenue",
            currency_code,
            "report_ad_revenue",
        )
        if args is None:
            return
        if len(args) == 2:
            d.report_ad_revenue_usd(*args)
        else:
            d.report_ad_revenue_local(*args)

    _with_dispatcher(_do)


def report_ad_items_granted(placement: str, items: Optional[Sequence[Item]] = None) -> None:
    """Reports virtual items granted from watching an ad (e.g. rewarded video)."""

    def _do(d: KEventDispatcher) -> None:
        where = _name(placement, "placement", "report_ad_items_granted")
        granted = _items(items, "items", "report_ad_items_granted")
        if where is None or granted is None:
            return
        d.report_ad_items_granted(where, granted)

    _with_dispatcher(_do)


# --------------------------------------------------------------------------------------------
# Monetization: subscriptions
# --------------------------------------------------------------------------------------------


def report_subscription_revenue(
    package_name: str,
    revenue_usd_cents: Optional[int] = None,
    localized_revenue: Optional[float] = None,
    currency_code: Optional[str] = None,
) -> None:
    """Reports subscription billing revenue (initial, conversion, or renewal). Provide either
    ``revenue_usd_cents`` or both ``localized_revenue`` and ``currency_code`` (ISO 4217)."""

    def _do(d: KEventDispatcher) -> None:
        args = _revenue(
            package_name,
            "package_name",
            revenue_usd_cents,
            "revenue_usd_cents",
            localized_revenue,
            "localized_revenue",
            currency_code,
            "report_subscription_revenue",
        )
        if args is None:
            return
        if len(args) == 2:
            d.report_subscription_revenue_usd(*args)
        else:
            d.report_subscription_revenue_local(*args)

    _with_dispatcher(_do)


def report_subscription_items_granted(package_name: str, items: Optional[Sequence[Item]] = None) -> None:
    """Reports virtual items granted from an active subscription."""

    def _do(d: KEventDispatcher) -> None:
        pkg = _name(package_name, "package_name", "report_subscription_items_granted")
        granted = _items(items, "items", "report_subscription_items_granted")
        if pkg is None or granted is None:
            return
        d.report_subscription_items_granted(pkg, granted)

    _with_dispatcher(_do)


# --------------------------------------------------------------------------------------------
# Economy
# --------------------------------------------------------------------------------------------


def report_items_exchange(
    exchange_location: str,
    from_items: Optional[Sequence[Item]] = None,
    to_items: Optional[Sequence[Item]] = None,
) -> None:
    """Reports an item exchange at a location: ``from_items`` are deducted, ``to_items`` are added.
    Pass an empty list for one-sided transactions."""

    def _do(d: KEventDispatcher) -> None:
        where = _name(exchange_location, "exchange_location", "report_items_exchange")
        src = _items(from_items, "from_items", "report_items_exchange")
        dst = _items(to_items, "to_items", "report_items_exchange")
        if where is None or src is None or dst is None:
            return
        d.report_item_exchange(where, src, dst)

    _with_dispatcher(_do)


def report_items_reset(location: str, items: Optional[Sequence[Item]] = None) -> None:
    """Resets/initializes the user's item balances at a location."""

    def _do(d: KEventDispatcher) -> None:
        where = _name(location, "location", "report_items_reset")
        balances = _items(items, "items", "report_items_reset")
        if where is None or balances is None:
            return
        d.report_items_reset(where, balances)

    _with_dispatcher(_do)


# --------------------------------------------------------------------------------------------
# Acquisition, progression & experimentation
# --------------------------------------------------------------------------------------------


def report_install_campaign(campaign_name: str) -> None:
    """Reports the marketing campaign that acquired this user."""

    def _do(d: KEventDispatcher) -> None:
        checked = _name(campaign_name, "campaign_name", "report_install_campaign")
        if checked is not None:
            d.report_install_campaign(checked)

    _with_dispatcher(_do)


def report_game_language(language: str) -> None:
    """Reports the in-app language (useful when it differs from the system language)."""

    def _do(d: KEventDispatcher) -> None:
        checked = _name(language, "language", "report_game_language")
        if checked is not None:
            d.report_game_language(checked)

    _with_dispatcher(_do)


def report_onboarding_milestone(milestone_name: str) -> None:
    """Reports a milestone reached during onboarding/tutorial (used to build the FTUE funnel)."""

    def _do(d: KEventDispatcher) -> None:
        checked = _name(milestone_name, "milestone_name", "report_onboarding_milestone")
        if checked is not None:
            d.report_onboarding_milestone(checked)

    _with_dispatcher(_do)


def report_ab_test_group_assignment(test_name: str, group: str) -> None:
    """Assigns the user to an A/B test group. ``group`` is a single character whose code point fits in
    one byte (``0..255``) — the backend reads the group as a single byte."""

    def _do(d: KEventDispatcher) -> None:
        test = _name(test_name, "test_name", "report_ab_test_group_assignment")
        if test is None:
            return
        if not _ab_group(group, "report_ab_test_group_assignment"):
            return
        d.assign_to_ab_test_group(test, group)

    _with_dispatcher(_do)


# --------------------------------------------------------------------------------------------
# Diagnostics & testing
# --------------------------------------------------------------------------------------------


def log_error(message: str) -> None:
    """Manually logs a technical error. Uncaught exceptions are captured automatically unless
    disabled via :attr:`KeewanoConfig.disable_exception_tracking`."""

    def _do(d: KEventDispatcher) -> None:
        # Same boundary checks as every other string parameter, but truncated rather than dropped at
        # a far larger budget: these carry stack traces, and a clipped trace beats none.
        checked = _name(message, "message", "log_error", max_len=MAX_ERROR_LENGTH)
        if checked is not None:
            d.log_error(checked)

    _with_dispatcher(_do)


def mark_as_test_user(tester_name: str) -> None:
    """Marks this device as a test user so Keewano AI excludes it from production analytics."""

    def _do(d: KEventDispatcher) -> None:
        checked = _validate_test_user(_name(tester_name, "tester_name", "mark_as_test_user"), "mark_as_test_user")
        if checked is not None:
            d.set_test_user_name(checked)

    _with_dispatcher(_do)


def flush() -> None:
    """Requests an immediate upload of buffered events (best-effort; upload is asynchronous)."""
    _with_dispatcher(lambda d: d.send_now())


# --------------------------------------------------------------------------------------------
# Lifecycle, connectivity & environment
#
# The mobile SDKs capture these automatically from OS lifecycle/platform signals. A native Python
# application has no such hooks (see docs/automatic-tracking.md), so they are reported explicitly
# where the app knows about them. Payload-less events need no validation; the string ones follow the
# same non-blank / <=256 rule as every other dimension.
# --------------------------------------------------------------------------------------------


def report_app_pause() -> None:
    """Reports that the app went to the background / was paused (a session boundary)."""
    _with_dispatcher(lambda d: d.report_app_pause())


def report_app_resume() -> None:
    """Reports that the app returned to the foreground / resumed (a session boundary)."""
    _with_dispatcher(lambda d: d.report_app_resume())


def report_internet_connected() -> None:
    """Reports that network connectivity was (re)established."""
    _with_dispatcher(lambda d: d.report_internet_connected())


def report_internet_disconnected() -> None:
    """Reports that network connectivity was lost."""
    _with_dispatcher(lambda d: d.report_internet_disconnected())


def report_low_memory() -> None:
    """Reports a low-memory warning from the host."""
    _with_dispatcher(lambda d: d.report_low_memory())


def report_deep_link(link: str) -> None:
    """Reports that the app was opened or activated via a deep link. Deep links are URLs, so — unlike
    the 256-char label dimensions — they keep the full wire budget (:data:`MAX_DEEP_LINK_LENGTH`)."""

    def _do(d: KEventDispatcher) -> None:
        checked = _name(link, "link", "report_deep_link", max_len=MAX_DEEP_LINK_LENGTH)
        if checked is not None:
            d.report_deep_link(checked)

    _with_dispatcher(_do)


def report_scene_loaded(scene_name: str) -> None:
    """Reports that a scene/screen was loaded (the analogue of the mobile SDKs' automatic scene
    tracking)."""

    def _do(d: KEventDispatcher) -> None:
        checked = _name(scene_name, "scene_name", "report_scene_loaded")
        if checked is not None:
            d.report_scene_loaded(checked)

    _with_dispatcher(_do)


def report_scene_unloaded(scene_name: str) -> None:
    """Reports that a scene/screen was unloaded."""

    def _do(d: KEventDispatcher) -> None:
        checked = _name(scene_name, "scene_name", "report_scene_unloaded")
        if checked is not None:
            d.report_scene_unloaded(checked)

    _with_dispatcher(_do)


def report_user_country(country: str) -> None:
    """Reports the user's country (e.g. an ISO 3166 code or country name), when the app knows it
    independently of anything the SDK captures."""

    def _do(d: KEventDispatcher) -> None:
        checked = _name(country, "country", "report_user_country")
        if checked is not None:
            d.report_user_country(checked)

    _with_dispatcher(_do)


class KeewanoSDK:
    """Namespaced facade mirroring the other Keewano SDKs. Every attribute is the same callable
    exported at module level (e.g. ``KeewanoSDK.report_button_click is keewano_sdk.report_button_click``)."""

    initialize = staticmethod(initialize)
    shutdown = staticmethod(shutdown)
    flush = staticmethod(flush)
    set_user_consent = staticmethod(set_user_consent)
    report_user_registered_before_sdk_integration = staticmethod(report_user_registered_before_sdk_integration)
    get_install_id = staticmethod(get_install_id)
    set_user_id = staticmethod(set_user_id)
    report_button_click = staticmethod(report_button_click)
    report_window_open = staticmethod(report_window_open)
    report_window_close = staticmethod(report_window_close)
    report_in_app_purchase = staticmethod(report_in_app_purchase)
    report_in_app_purchase_items_granted = staticmethod(report_in_app_purchase_items_granted)
    report_ad_offered = staticmethod(report_ad_offered)
    report_ad_revenue = staticmethod(report_ad_revenue)
    report_ad_items_granted = staticmethod(report_ad_items_granted)
    report_subscription_revenue = staticmethod(report_subscription_revenue)
    report_subscription_items_granted = staticmethod(report_subscription_items_granted)
    report_items_exchange = staticmethod(report_items_exchange)
    report_items_reset = staticmethod(report_items_reset)
    report_install_campaign = staticmethod(report_install_campaign)
    report_game_language = staticmethod(report_game_language)
    report_onboarding_milestone = staticmethod(report_onboarding_milestone)
    report_ab_test_group_assignment = staticmethod(report_ab_test_group_assignment)
    log_error = staticmethod(log_error)
    mark_as_test_user = staticmethod(mark_as_test_user)
    report_app_pause = staticmethod(report_app_pause)
    report_app_resume = staticmethod(report_app_resume)
    report_internet_connected = staticmethod(report_internet_connected)
    report_internet_disconnected = staticmethod(report_internet_disconnected)
    report_low_memory = staticmethod(report_low_memory)
    report_deep_link = staticmethod(report_deep_link)
    report_scene_loaded = staticmethod(report_scene_loaded)
    report_scene_unloaded = staticmethod(report_scene_unloaded)
    report_user_country = staticmethod(report_user_country)
