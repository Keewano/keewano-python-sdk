"""Public reporting bridge for the Keewano custom-events codegen.

App developers do not call this directly. The codegen tool emits a file **into the app** whose typed
``report_*`` wrappers forward here (see ``docs/custom-events.md``). It is parked on its own namespace
to stay out of ``keewano_sdk.*`` autocomplete.

Python has no method overloading, so — unlike Android's ``Int``/``Long`` or iOS's ``Int32``/``Int64``
overloads — there is one entry point per payload type; the wire type is fixed by which one the
generated wrapper calls, not by the Python value. Every entry point validates that ``event_id`` is in
the custom range (``2500..65535``) and drops the event otherwise, and applies the same argument checks
as the built-in ``report_*`` API.
"""

from __future__ import annotations

import math

from . import sdk, server_sdk
from .server_sdk import UserId
from .internal.events import KEvents

_CUSTOM_ID_MIN = KEvents.CUSTOM_EVENT_ID_MIN
_CUSTOM_ID_MAX = 0xFFFF


def _is_custom_id(event_id: int, caller: str) -> bool:
    if (
        not isinstance(event_id, int)
        or isinstance(event_id, bool)
        or not (_CUSTOM_ID_MIN <= event_id <= _CUSTOM_ID_MAX)
    ):
        sdk._log.error(
            "%s: id %s is outside the custom-event range (%d..%d); ignored.",
            caller,
            event_id,
            _CUSTOM_ID_MIN,
            _CUSTOM_ID_MAX,
        )
        return False
    return True


def _float_ok(value: float, caller: str) -> bool:
    """A custom-event float payload: any finite number that fits a 32-bit float (negative allowed)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        sdk._log.error("%s: 'value' must be a number, got %s; event dropped.", caller, type(value).__name__)
        return False
    # float32-fit first: math.isfinite() raises OverflowError on an int too large to convert to a
    # double (e.g. 10**400); _fits_float32 handles any value without raising. NaN/Infinity pack
    # without error, so the isfinite check below still rejects those.
    if not sdk._fits_float32(value):
        sdk._log.error("%s: 'value' %s is too large for a 32-bit float; event dropped.", caller, value)
        return False
    if not math.isfinite(value):
        sdk._log.error("%s: 'value' is %s; event dropped.", caller, value)
        return False
    return True


class KeewanoCodegen:
    """Reporting bridge the generated custom-event wrappers forward to."""

    @staticmethod
    def report_custom_event(event_id: int) -> None:
        """A custom event with no payload."""
        if _is_custom_id(event_id, "report_custom_event"):
            sdk._with_dispatcher(lambda d: d.add_event(event_id))

    @staticmethod
    def report_custom_event_int(event_id: int, value: int) -> None:
        """A custom event carrying a signed 32-bit int (``int32`` on the wire)."""
        if not _is_custom_id(event_id, "report_custom_event_int"):
            return
        if not sdk._int32(value, "value", "report_custom_event_int"):
            return
        sdk._with_dispatcher(lambda d: d.add_event_int(event_id, value))

    @staticmethod
    def report_custom_event_uint(event_id: int, value: int) -> None:
        """A custom event carrying an unsigned 32-bit int (``uint32`` on the wire)."""
        if not _is_custom_id(event_id, "report_custom_event_uint"):
            return
        if not sdk._count(value, "value", "report_custom_event_uint"):
            return
        sdk._with_dispatcher(lambda d: d.add_event_uint(event_id, value))

    @staticmethod
    def report_custom_event_bool(event_id: int, value: bool) -> None:
        """A custom event carrying a bool."""
        if _is_custom_id(event_id, "report_custom_event_bool"):
            if isinstance(value, bool):
                sdk._with_dispatcher(lambda d: d.add_event_bool(event_id, value))
            else:
                sdk._log.error(
                    "report_custom_event_bool: 'value' must be a bool, got %s; event dropped.", type(value).__name__
                )

    @staticmethod
    def report_custom_event_float(event_id: int, value: float) -> None:
        """A custom event carrying a 32-bit float. NaN/Infinity are dropped (they serialize to
        garbage); negative values are allowed. An int is accepted (it serializes cleanly as a
        float); a non-number (or bool) is dropped rather than crashing math.isfinite below. A finite
        value out of 32-bit float range (a Python float is a 64-bit double) is dropped too — it would
        overflow the wire writer."""
        if not _is_custom_id(event_id, "report_custom_event_float"):
            return
        if _float_ok(value, "report_custom_event_float"):
            sdk._with_dispatcher(lambda d: d.add_event_float(event_id, value))

    @staticmethod
    def report_custom_event_str(event_id: int, value: str) -> None:
        """A custom event carrying a string (blank → dropped, over-long → truncated)."""
        if not _is_custom_id(event_id, "report_custom_event_str"):
            return
        checked = sdk._name(value, "value", "report_custom_event_str")
        if checked is not None:
            sdk._with_dispatcher(lambda d: d.add_event_str(event_id, checked))

    @staticmethod
    def report_custom_event_ushort_pair(event_id: int, x: int, y: int) -> None:
        """A custom event carrying two unsigned 16-bit ints (two ``uint16`` on the wire). Either half
        outside ``0..65535`` drops the event."""
        if not _is_custom_id(event_id, "report_custom_event_ushort_pair"):
            return
        if not sdk._count16(x, "x", "report_custom_event_ushort_pair"):
            return
        if not sdk._count16(y, "y", "report_custom_event_ushort_pair"):
            return
        sdk._with_dispatcher(lambda d: d.add_event_ushort_pair(event_id, x, y))


class KeewanoServerCodegen:
    """Server-side counterpart of :class:`KeewanoCodegen` (see :mod:`keewano_sdk.server_sdk`): the same
    payload types and checks, with the end user's id first. The generated module's
    ``server_report_<name>(user_id, …)`` wrappers forward here; apps call those, not this class."""

    @staticmethod
    def report_custom_event(user_id: UserId, event_id: int) -> None:
        if _is_custom_id(event_id, "report_custom_event"):
            server_sdk._with_user_dispatcher("report_custom_event", user_id, lambda d, u: d.add_event(u, event_id))

    @staticmethod
    def report_custom_event_int(user_id: UserId, event_id: int, value: int) -> None:
        if _is_custom_id(event_id, "report_custom_event_int") and sdk._int32(value, "value", "report_custom_event_int"):
            server_sdk._with_user_dispatcher(
                "report_custom_event_int", user_id, lambda d, u: d.add_event_int(u, event_id, value)
            )

    @staticmethod
    def report_custom_event_uint(user_id: UserId, event_id: int, value: int) -> None:
        if _is_custom_id(event_id, "report_custom_event_uint") and sdk._count(
            value, "value", "report_custom_event_uint"
        ):
            server_sdk._with_user_dispatcher(
                "report_custom_event_uint", user_id, lambda d, u: d.add_event_uint(u, event_id, value)
            )

    @staticmethod
    def report_custom_event_bool(user_id: UserId, event_id: int, value: bool) -> None:
        if not _is_custom_id(event_id, "report_custom_event_bool"):
            return
        if not isinstance(value, bool):
            sdk._log.error(
                "report_custom_event_bool: 'value' must be a bool, got %s; event dropped.", type(value).__name__
            )
            return
        server_sdk._with_user_dispatcher(
            "report_custom_event_bool", user_id, lambda d, u: d.add_event_bool(u, event_id, value)
        )

    @staticmethod
    def report_custom_event_float(user_id: UserId, event_id: int, value: float) -> None:
        if _is_custom_id(event_id, "report_custom_event_float") and _float_ok(value, "report_custom_event_float"):
            server_sdk._with_user_dispatcher(
                "report_custom_event_float", user_id, lambda d, u: d.add_event_float(u, event_id, value)
            )

    @staticmethod
    def report_custom_event_str(user_id: UserId, event_id: int, value: str) -> None:
        if not _is_custom_id(event_id, "report_custom_event_str"):
            return
        checked = sdk._name(value, "value", "report_custom_event_str")
        if checked is not None:
            server_sdk._with_user_dispatcher(
                "report_custom_event_str", user_id, lambda d, u: d.add_event_str(u, event_id, checked)
            )

    @staticmethod
    def report_custom_event_ushort_pair(user_id: UserId, event_id: int, x: int, y: int) -> None:
        caller = "report_custom_event_ushort_pair"
        if _is_custom_id(event_id, caller) and sdk._count16(x, "x", caller) and sdk._count16(y, "y", caller):
            server_sdk._with_user_dispatcher(caller, user_id, lambda d, u: d.add_event_ushort_pair(u, event_id, x, y))
