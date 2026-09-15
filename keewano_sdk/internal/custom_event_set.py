"""The gzipped custom-event definition set the SDK registers with the backend.

It is persisted locally by ``serializer`` so the SDK need not rebuild it every launch, and uploaded by
``network.register_custom_events``. It is *not* part of the cross-SDK ``.kwub`` wire format.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field


@dataclass
class CustomEventSet:
    """The custom-event definition set a Keewano-codegen file passes to
    :func:`keewano_sdk.initialize` via :class:`~keewano_sdk.KeewanoConfig`. The SDK persists it,
    stamps its ``version`` on every batch, and registers it with the backend on first upload."""

    #: Backend-known version/hash of this mapping (sent as ``K-CustomEventHash``). ``0`` = none.
    version: int = 0
    #: Number of custom events defined in the set (sent as ``K-CustomEventCount``).
    event_count: int = 0
    #: The already-gzipped binary definitions, uploaded to the backend verbatim.
    gzip_data: bytes = field(default=b"")

    @classmethod
    def from_gzip_base64(cls, version: int, event_count: int, gzip_base64: str) -> "CustomEventSet":
        """Builds a set from base64-encoded gzip bytes — the form the generated file embeds. Base64
        (not a byte literal) keeps the generated source compact; the SDK decodes it here and keeps the
        gzip as-is for upload (it must never be re-compressed)."""
        return cls(version=version, event_count=event_count, gzip_data=base64.b64decode(gzip_base64))
