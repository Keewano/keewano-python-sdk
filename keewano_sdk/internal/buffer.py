"""A growable little-endian byte buffer reproducing C# ``System.IO.BinaryWriter``.

Every event parameter in a Keewano batch is serialized through these primitives, so the
encoding must match byte-for-byte or the backend cannot parse the data.
"""

from __future__ import annotations

import struct
from typing import Optional


class KBuffer:
    """A little-endian byte buffer with BinaryWriter-compatible primitive writers."""

    __slots__ = ("_data",)

    def __init__(self) -> None:
        self._data = bytearray()

    def __len__(self) -> int:
        return len(self._data)

    @property
    def length(self) -> int:
        return len(self._data)

    def raw_buffer(self) -> bytearray:
        """Direct access to the backing array; avoids copies on the hot path."""
        return self._data

    def set_length(self, new_length: int) -> None:
        """Shrink the buffer to ``new_length`` bytes (only shrinking is supported)."""
        if not 0 <= new_length <= len(self._data):
            raise ValueError("set_length only supports shrinking within bounds")
        del self._data[new_length:]

    def to_bytes(self) -> bytes:
        return bytes(self._data)

    # --- Primitive writers, all little-endian to match BinaryWriter. ---

    def write_raw_byte(self, value: int) -> None:
        self._data.append(value & 0xFF)

    def write_bytes(self, src: bytes, offset: int = 0, count: Optional[int] = None) -> None:
        if count is None:
            count = len(src) - offset
        self._data += src[offset : offset + count]

    def write_int32(self, value: int) -> None:
        """BinaryWriter.Write(int) - 4 bytes little-endian."""
        self._data += struct.pack("<i", _to_signed32(value))

    def write_uint32(self, value: int) -> None:
        """BinaryWriter.Write(uint) - 4 bytes little-endian."""
        self._data += struct.pack("<I", value & 0xFFFFFFFF)

    def write_uint16(self, value: int) -> None:
        """BinaryWriter.Write(ushort) - 2 bytes little-endian."""
        self._data += struct.pack("<H", value & 0xFFFF)

    def write_float(self, value: float) -> None:
        """BinaryWriter.Write(float) - 4 bytes IEEE-754 little-endian."""
        self._data += struct.pack("<f", value)

    def write_string(self, value: str) -> None:
        """BinaryWriter.Write(string): a 7-bit-encoded (LEB128) UTF-8 byte-length prefix
        followed by the UTF-8 bytes."""
        utf8 = value.encode("utf-8")
        self._write_7bit_encoded_int(len(utf8))
        self._data += utf8

    def write_char(self, value: str) -> None:
        """BinaryWriter.Write(char) with a UTF-8 encoding: the char's UTF-8 bytes, no prefix."""
        if len(value) != 1:
            raise ValueError("write_char expects a single character")
        self._data += value.encode("utf-8")

    def _write_7bit_encoded_int(self, value: int) -> None:
        v = value & 0xFFFFFFFF
        while v >= 0x80:
            self._data.append((v & 0x7F) | 0x80)
            v >>= 7
        self._data.append(v)


def _to_signed32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value
