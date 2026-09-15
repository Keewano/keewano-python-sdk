"""Best-effort host/environment introspection for the launch event burst.

A native Python application has no mobile-style platform services, so - unlike the Android
SDK's ``KAutoTracker`` - this gathers what the standard library can portably report and
degrades gracefully (returning empty/zero) when a value is unavailable. Nothing here raises.
"""

from __future__ import annotations

import locale
import os
import platform
import sys

#: Environment variables that carry the locale, in the priority order ``locale.getdefaultlocale()``
#: consulted — so reading them reproduces its result without the deprecated call.
_LOCALE_ENV_VARS = ("LC_ALL", "LC_CTYPE", "LANG", "LANGUAGE")


def platform_name() -> str:
    """A coarse OS family name, the analogue of the Android SDK's ``"Android"`` PLATFORM value."""
    system = platform.system()
    return {"Darwin": "macOS", "": "Unknown"}.get(system, system)


def os_description() -> str:
    """e.g. ``"Linux 6.8.0"``, ``"Windows 10"``, ``"macOS 14.5"`` - best effort."""
    system = platform_name()
    release = platform.release()
    return f"{system} {release}".strip()


def device_type() -> str:
    """Hardware descriptor - the machine architecture (e.g. ``"x86_64"``, ``"arm64"``)."""
    machine = platform.machine()
    return machine or "Unknown"


def system_language() -> str:
    """The host's primary language subtag (e.g. ``"en"``), or ``""`` if it cannot be determined.

    Deliberately avoids ``locale.getdefaultlocale()`` (deprecated in 3.11, removed in 3.15) and never
    calls ``locale.setlocale()`` — an analytics SDK must not mutate the host process's global locale.
    It reads the locale the way each platform exposes it: the process locale if the app configured
    one, otherwise the POSIX ``LC_*`` / ``LANG`` environment variables, otherwise the Windows API.
    """
    raw = _detect_locale_name()
    if not raw:
        return ""
    # Normalise "en_US.UTF-8" / "en-US" / "de_DE@euro" / "en_US:en" -> "en".
    primary = raw.replace("-", "_").split(":", 1)[0]
    return primary.split(".", 1)[0].split("@", 1)[0].split("_", 1)[0].lower()


def _detect_locale_name() -> str:
    """Best-effort raw locale name (e.g. ``"en_US.UTF-8"``), or ``""``. Never raises."""
    # 1) The process locale — but only if the host actually configured one. An unconfigured locale
    #    reports the "C"/"POSIX" default, which says nothing about the user's language.
    try:
        name = locale.getlocale()[0]
    except Exception:
        # getlocale() raises ValueError on an unparseable locale; stay defensive so nothing here
        # can propagate out of the launch burst.
        name = None
    if name and name.upper() not in ("C", "POSIX"):
        return name

    # 2) POSIX: the same environment variables getdefaultlocale() read.
    for var in _LOCALE_ENV_VARS:
        value = os.environ.get(var)
        if value and value.upper() not in ("C", "POSIX"):
            return value

    # 3) Windows: env vars are usually unset, so ask the OS directly.
    if sys.platform == "win32":
        return _windows_locale_name()

    return ""


def _windows_locale_name() -> str:
    """The Windows user-default locale (e.g. ``"en-US"``), or ``""``. Never raises."""
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(85)  # LOCALE_NAME_MAX_LENGTH
        if ctypes.windll.kernel32.GetUserDefaultLocaleName(buffer, len(buffer)):  # type: ignore[attr-defined]
            return buffer.value or ""
    except Exception:
        return ""
    return ""


def ram_size_mb() -> int:
    """Total physical RAM in MB, or 0 if it cannot be determined without third-party deps."""
    # POSIX (Linux, macOS): sysconf pages * page size.
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return int(pages * page_size // (1024 * 1024))
    except (ValueError, AttributeError, OSError):
        pass

    # Windows: GlobalMemoryStatusEx via ctypes.
    try:
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = _MemoryStatusEx()
        stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
            return int(stat.ullTotalPhys // (1024 * 1024))
    except Exception:
        pass

    return 0
