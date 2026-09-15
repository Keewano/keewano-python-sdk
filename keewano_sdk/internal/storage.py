"""Small persistence layer for durable, cross-launch SDK state: the install/user identifiers, the
consent decision, the one-shot "reported pre-SDK registration" marker, and the state files that live
in the dispatcher's work folder (the custom-event set, the onboarding counters and the QA test-user
marker).

All writes are atomic (write-temp-then-rename via :func:`~keewano_sdk.internal.files.atomic_write`) so
a crash mid-write can never corrupt state, and a write that fails is logged rather than silently
discarded.

Nothing here is uploaded and nothing here is read by another platform, so each file uses whichever
Python-native encoding suits its shape: JSON for structured state, raw bytes or UTF-8 text where a
container would only add ceremony. Only ``.kwub`` batches have to satisfy an external parser, and
that format lives alone in :mod:`keewano_sdk.internal.serializer`.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import struct
import threading
from typing import Dict, Optional

from . import guid
from .consent import UserConsentState
from .custom_event_set import CustomEventSet
from .files import atomic_write
from .guid import KGuid

_log = logging.getLogger("keewano_sdk")

#: On-disk format version for the custom-event-set file. Bumped only if the record shape changes; an
#: unrecognized version reads as "nothing saved" and the set is simply rebuilt next launch.
_CE_SET_FORMAT_VERSION = 1


class UserIdentifiers:
    __slots__ = ("install_id", "user_id")

    def __init__(self, install_id: KGuid, user_id: KGuid) -> None:
        self.install_id = install_id
        self.user_id = user_id


class KStorage:
    def __init__(self, base_dir: str) -> None:
        self._base_dir = base_dir
        # One lock per file, so concurrent read/write of the same file is serialized. atomic_write
        # uses a fixed "<file>.tmp" name, so two threads writing the same target would otherwise
        # clobber each other's temp file and race the rename. Different files never contend.
        self._ids_lock = threading.Lock()
        self._consent_lock = threading.Lock()
        self._pre_sdk_reg_lock = threading.Lock()
        try:
            os.makedirs(base_dir, exist_ok=True)
        except OSError:
            pass

    @property
    def _ids_file(self) -> str:
        return os.path.join(self._base_dir, "Keewano_Ids")

    @property
    def _consent_file(self) -> str:
        return os.path.join(self._base_dir, "Keewano_UserConsent")

    @property
    def _pre_sdk_reg_file(self) -> str:
        return os.path.join(self._base_dir, "Keewano_PreSDKReg")

    def load_or_init_identifiers(self) -> Optional[UserIdentifiers]:
        """Returns the durable install/user identifiers, minting them on first run.

        Returns **None** when the file exists but cannot be read. That case must not mint a new
        install id: a transient I/O error would then permanently replace the install identity (and
        the user id with it), which downstream reads as a returning user vanishing and a phantom new
        install appearing, with nothing to correlate them. A None return costs us this launch's
        data; overwriting costs us the install's entire history. The caller decides what to do.
        """
        with self._ids_lock:
            f = self._ids_file
            if os.path.exists(f):
                try:
                    with open(f, "rb") as fh:
                        data = fh.read()
                except OSError:
                    _log.error("Could not read the identifiers file; refusing to replace it.")
                    return None
                if len(data) >= 32:
                    return UserIdentifiers(guid.from_bytes(data[0:16]), guid.from_bytes(data[16:32]))
                # Readable but too short to hold an identity. atomic_write makes this impossible in
                # theory; if it happens anyway there is nothing to preserve, so start over.
                _log.warning("Identifiers file is %d bytes; treating this install as new.", len(data))

            minted = UserIdentifiers(guid.new_guid(), guid.EMPTY)
            # Do not check for failure since server applications are long running and it is better to
            # have an active SDK although the installId might change on the next run. Also server apps
            # mostly do not have issues with writting to disk.
            self._save_identifiers_no_lock(minted)
            return minted

    def save_identifiers(self, ids: UserIdentifiers) -> None:
        with self._ids_lock:
            self._save_identifiers_no_lock(ids)

    def _save_identifiers_no_lock(self, ids: UserIdentifiers) -> None:
        """Body of :meth:`save_identifiers`; call only while holding ``_ids_lock``."""
        try:
            atomic_write(self._ids_file, ids.install_id.to_bytes() + ids.user_id.to_bytes())
        except OSError:
            _log.error("Failed to persist the identifiers.")

    def load_user_consent_state(self) -> Optional[UserConsentState]:
        """Returns the persisted consent state, or None if nothing has been persisted yet."""
        with self._consent_lock:
            try:
                f = self._consent_file
                if os.path.exists(f) and os.path.getsize(f) >= 4:
                    with open(f, "rb") as fh:
                        ordinal = struct.unpack("<i", fh.read(4))[0]
                    return UserConsentState.from_ordinal(ordinal)
            except Exception:
                pass
            return None

    def save_user_consent_state(self, state: UserConsentState) -> None:
        with self._consent_lock:
            try:
                atomic_write(self._consent_file, struct.pack("<i", int(state)))
            except OSError:
                # Logged, not swallowed: a silent failure here means we hold Granted in memory and
                # Pending on disk (or the reverse) with nothing recording the divergence.
                _log.error("Failed to persist the consent state.")

    def has_pre_sdk_registration_been_reported(self) -> bool:
        with self._pre_sdk_reg_lock:
            try:
                return os.path.exists(self._pre_sdk_reg_file)
            except OSError:
                return False

    def mark_pre_sdk_registration_as_reported(self) -> None:
        with self._pre_sdk_reg_lock:
            try:
                atomic_write(self._pre_sdk_reg_file, b"\x01")
            except OSError:
                _log.error("Failed to persist the pre-SDK marker.")

    # --- Work-folder state files ----------------------------------------------------------------
    #
    # The helpers below are addressed by full path and are ``@staticmethod`` for that reason: they
    # live in the dispatcher's work folder (``<base_dir>/batches``), not in this instance's base_dir,
    # and the dispatcher already serializes access to them (init writes the set once before the send
    # thread runs; onboarding/test-user are touched under its swap_lock), so the per-file locks above
    # would guard nothing. Every load degrades to "nothing saved" rather than failing — the cost is at
    # worst one re-registration of the custom-event set, a milestone reported as ``x`` instead of
    # ``x (#2)``, or a QA device's uploads rejoining the production stream. No user data rides on them.

    @staticmethod
    def save_custom_event_set_to_file(filename: str, ce_set: CustomEventSet) -> None:
        """Persists a custom-event definition set as JSON. Best-effort — if it fails, the set is
        simply rebuilt and re-persisted from the app's own definitions next launch. Local-only, so it
        is JSON (with the gzip base64-encoded) rather than a ``.kwub`` frame."""
        record = {
            "format_version": _CE_SET_FORMAT_VERSION,
            "version": ce_set.version,
            "event_count": ce_set.event_count,
            "gzip_base64": base64.b64encode(ce_set.gzip_data).decode("ascii"),
        }
        try:
            atomic_write(filename, json.dumps(record).encode("utf-8"))
        except OSError:
            _log.error("Failed to persist the custom-event set.")

    @staticmethod
    def load_custom_event_set_from_file(filename: str) -> Optional[CustomEventSet]:
        """Loads a custom-event set, or None if it is missing, unreadable, truncated, or a format
        version we no longer recognize."""
        try:
            with open(filename, "rb") as f:
                record = json.loads(f.read().decode("utf-8"))
            if not isinstance(record, dict):
                return None
            if record.get("format_version") != _CE_SET_FORMAT_VERSION:
                return None
            return CustomEventSet(
                version=int(record["version"]),
                event_count=int(record["event_count"]),
                gzip_data=base64.b64decode(record["gzip_base64"]),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def load_onboarding_counters(filename: str) -> Dict[str, int]:
        """Loads the per-milestone occurrence counts, or an empty map when nothing readable is on
        disk. JSON decodes all-or-nothing, so a truncated file can never yield a half-populated map,
        and a value of the wrong type rejects the whole file rather than being skipped."""
        try:
            with open(filename, "rb") as f:
                data = json.loads(f.read().decode("utf-8"))
            if not isinstance(data, dict):
                return {}
            out: Dict[str, int] = {}
            for key, value in data.items():
                if not isinstance(value, int) or isinstance(value, bool):
                    return {}  # a non-int value rejects the whole file
                out[str(key)] = value
            return out
        except (OSError, ValueError):
            return {}

    @staticmethod
    def save_onboarding_counters(filename: str, counters: Dict[str, int]) -> None:
        """Persists the onboarding occurrence counts as JSON. Best-effort."""
        try:
            atomic_write(filename, json.dumps(counters).encode("utf-8"))
        except OSError:
            _log.error("Failed to persist the onboarding counters.")

    @staticmethod
    def load_test_user_name(filename: str) -> Optional[str]:
        """Loads the QA tester name, or None when this install is not marked as a test user.

        Plain UTF-8 text, not a container: it is one string, and being able to ``cat`` the file is
        genuinely useful. Surrounding whitespace is trimmed and a blank file reads as None — the name
        becomes the ``K-Tester`` header on every upload, so a zero-byte file left by a full disk (or a
        stray newline from hand-editing this very inspectable file) must not turn into an empty header
        that rides along forever."""
        try:
            with open(filename, "r", encoding="utf-8") as f:
                name = f.read().strip()
        except (OSError, ValueError):
            # ValueError covers UnicodeDecodeError: a corrupt/non-UTF-8 file (it is not an OSError).
            # Like the other loaders here, a bad file reads as "no test user", never a raise.
            return None
        return name or None

    @staticmethod
    def save_test_user_name(filename: str, name: str) -> None:
        """Persists the QA tester name as plain UTF-8 text. Best-effort."""
        try:
            atomic_write(filename, name.encode("utf-8"))
        except OSError:
            _log.error("Failed to persist the test-user name.")
