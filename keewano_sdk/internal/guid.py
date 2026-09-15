"""Byte-compatible reimplementation of a .NET ``System.Guid``.

The Keewano backend receives GUIDs in two forms that must agree with the other SDKs:

 * as the 16 raw bytes written into the ``.kwub`` batch file (via ``Guid.TryWriteBytes``), and
 * as the canonical dashed string in HTTP headers (via ``Guid.ToString("D")``).

.NET stores a GUID as ``{int32 a, int16 b, int16 c, byte d..k}``. ``TryWriteBytes`` emits
``a``, ``b`` and ``c`` little-endian followed by ``d..k`` verbatim. We store exactly those
16 "wire" bytes and reproduce both the file layout and the string formatting from them.
This is the Python analogue of the Android SDK's ``KGuid``.
"""

from __future__ import annotations

import os


class KGuid:
    """A .NET-compatible GUID stored as its 16 wire bytes."""

    __slots__ = ("_wire",)

    def __init__(self, wire: bytes) -> None:
        if len(wire) != 16:
            raise ValueError(f"GUID must be 16 bytes, was {len(wire)}")
        self._wire = bytes(wire)

    def to_bytes(self) -> bytes:
        """The 16 raw bytes exactly as .NET's ``Guid.TryWriteBytes`` would produce them."""
        return self._wire

    def __str__(self) -> str:
        """Matches ``Guid.ToString("D")``: ``xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx``, lower-case."""
        w = self._wire
        a = w[0] | (w[1] << 8) | (w[2] << 16) | (w[3] << 24)
        b = w[4] | (w[5] << 8)
        c = w[6] | (w[7] << 8)
        tail = w[8:16].hex()
        return f"{a:08x}-{b:04x}-{c:04x}-{tail[:4]}-{tail[4:]}"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, KGuid) and other._wire == self._wire

    def __hash__(self) -> int:
        return hash(self._wire)

    def __repr__(self) -> str:
        return f"KGuid('{self}')"


EMPTY = KGuid(b"\x00" * 16)


def new_guid() -> KGuid:
    """Equivalent to .NET ``Guid.NewGuid()``: 16 cryptographically random bytes with the RFC-4122
    version (4) and variant (10xx) bits set, so the wire bytes and dashed string match what the
    other SDKs produce."""
    b = bytearray(os.urandom(16))
    b[7] = (b[7] & 0x0F) | 0x40  # version = 4
    b[8] = (b[8] & 0x3F) | 0x80  # variant = 10xx
    return KGuid(bytes(b))


def from_uint64(uid: int) -> KGuid:
    """Mapping:
    ``new Guid(0, 0, 0, b0, b1, ..., b7)`` where ``b0`` is the least-significant byte.
    The first 8 wire bytes are therefore zero and the last 8 hold ``uid`` little-endian."""
    if uid < 0 or uid > 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"Invalid uint64 value {uid}")
    uid &= 0xFFFFFFFFFFFFFFFF
    return KGuid(b"\x00" * 8 + uid.to_bytes(8, "little"))


def from_bytes(wire: bytes) -> KGuid:
    """Reconstructs a ``KGuid`` from previously persisted wire bytes."""
    return KGuid(wire)


def from_string(value: str) -> KGuid:
    """Parses a canonical dashed GUID string into a ``KGuid`` whose wire bytes reproduce that
    exact string via ``str()``. Inverse of .NET ``Guid.ToString("D")``: the first three groups
    are little-endian, the last two verbatim."""
    hex_str = value.replace("-", "")
    if len(hex_str) != 32:
        raise ValueError(f"Invalid GUID string: {value}")
    raw = bytes.fromhex(hex_str)  # display order: a(4) b(2) c(2) d e f g h i j k
    wire = (
        bytes(
            (
                raw[3],
                raw[2],
                raw[1],
                raw[0],  # a, little-endian
                raw[5],
                raw[4],  # b, little-endian
                raw[7],
                raw[6],  # c, little-endian
            )
        )
        + raw[8:16]
    )  # d..k verbatim
    return KGuid(wire)
