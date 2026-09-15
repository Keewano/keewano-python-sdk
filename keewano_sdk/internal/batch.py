"""A batch of serialized events plus its metadata. Mirror of the Android SDK's ``KBatch``.

The event bytes live in ``data``; batch metadata (ids, timestamps, custom-event version) is
written around them by ``serializer``. ``cut_positions`` records where the collecting batch
may be sliced into self-contained sub-batches.
"""

from __future__ import annotations

from typing import List

from .buffer import KBuffer
from .guid import KGuid

CURRENT_BATCH_VERSION = 2


class CutPoint:
    """Marks where a large in-memory batch can be split into a self-contained sub-batch."""

    __slots__ = ("pos", "last_event_time")

    def __init__(self, pos: int, last_event_time: int) -> None:
        self.pos = pos
        self.last_event_time = last_event_time


class KBatch:
    def __init__(self, install_id: KGuid, user_id: KGuid, data_session_id: KGuid) -> None:
        self.install_id = install_id
        self.user_id = user_id
        self.data_session_id = data_session_id

        self.data = KBuffer()
        self.cut_positions: List[CutPoint] = []

        self.batch_num = 0
        self.custom_events_version = 0
        self.batch_start_time = 0
        self.batch_end_time = 0
        self.batch_version = CURRENT_BATCH_VERSION
