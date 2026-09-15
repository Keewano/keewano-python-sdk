"""Reads and writes the on-disk ``.kwub`` batch format.

The layout below is fixed by the Keewano backend, which parses the data part of these files, so it is
byte-compatible with all of Keewano's SDKs. The rest of the fields are used to populate the parameters
sent to the backend when sending a batch. The entire file format is also compatible across all SDKs
so it can be diffable.

::

    int32    fourCC  = 0x57554242
    uint32   version = 2
    byte[16] userId          (.NET Guid wire bytes)
    byte[16] dataSessionId   (.NET Guid wire bytes)
    int32    batchNum
    uint32   batchStartTime
    uint32   batchEndTime
    int32    dataLength
    byte[]   data             (serialized events)
    uint32   customEventsVersion
"""

from __future__ import annotations

import struct

from .batch import CURRENT_BATCH_VERSION, KBatch
from .files import atomic_write
from . import guid

_BATCH_FOURCC = 0x57554242  # "KWUB"


def save_to_file(batch: KBatch, filename: str) -> int:
    """Serializes ``batch`` to ``filename``. Returns the number of bytes written, or 0 on failure."""
    try:
        from .buffer import KBuffer

        out = KBuffer()
        out.write_int32(_BATCH_FOURCC)
        out.write_uint32(CURRENT_BATCH_VERSION)
        out.write_bytes(batch.user_id.to_bytes())
        out.write_bytes(batch.data_session_id.to_bytes())
        out.write_int32(batch.batch_num)
        out.write_uint32(batch.batch_start_time)
        out.write_uint32(batch.batch_end_time)
        out.write_int32(batch.data.length)
        out.write_bytes(batch.data.raw_buffer(), 0, batch.data.length)
        out.write_uint32(batch.custom_events_version)

        payload = out.to_bytes()
        atomic_write(filename, payload)
        return len(payload)
    except Exception:
        # Nothing we can do if the write fails; drop the batch.
        return 0


def load_from_file(filename: str, dst: KBatch) -> int:
    """Loads a batch file into ``dst``. Returns the number of bytes read, or -1 on failure."""
    try:
        with open(filename, "rb") as f:
            data = f.read()
        r = _LEReader(data)

        if r.read_int32() != _BATCH_FOURCC:
            return -1
        version = r.read_uint32()
        # Only version 2 has ever been written to disk: save_to_file always stamps the current one.
        if version != CURRENT_BATCH_VERSION:
            return -1

        dst.batch_version = version
        dst.user_id = guid.from_bytes(r.read_bytes(16))
        dst.data_session_id = guid.from_bytes(r.read_bytes(16))
        dst.batch_num = r.read_int32()
        dst.batch_start_time = r.read_uint32()
        dst.batch_end_time = r.read_uint32()

        data_size = r.read_int32()
        dst.data.set_length(0)
        dst.data.write_bytes(r.read_bytes(data_size))

        dst.custom_events_version = r.read_uint32()
        return r.position
    except Exception:
        return -1


class _LEReader:
    """Little-endian reader mirroring C#'s BinaryReader for the fixed-width fields we persist."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.position = 0

    def read_bytes(self, count: int) -> bytes:
        # ``count`` reaches here from the file's own ``dataLength`` field, a signed int32 — so a
        # corrupt file can ask for a negative read. Check that FIRST: a negative count makes
        # ``position + count`` smaller than ``len``, so the EOF test would wave it through and rewind
        # the reader instead of failing the load.
        if count < 0:
            raise ValueError("negative count")
        end = self.position + count
        if end > len(self._data):
            raise EOFError()
        out = self._data[self.position : end]
        self.position = end
        return out

    def read_int32(self) -> int:
        return struct.unpack_from("<i", self._data, self._advance(4))[0]

    def read_uint32(self) -> int:
        return struct.unpack_from("<I", self._data, self._advance(4))[0]

    def _advance(self, count: int) -> int:
        start = self.position
        if start + count > len(self._data):
            raise EOFError()
        self.position += count
        return start
