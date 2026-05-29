"""Wire protocol for pulseUDP.

This module implements the framing described in ``spec/RFC-pulseUDP.md``:
the 12-byte message header, the merged type/sequence word, and the rules for
laying telemetry values into frames. Payload semantics are driven at runtime by
the JSON descriptor (see ``spec/Schema.json``).

Everything here is little-endian, matching the RFC.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

# --- Constants ---------------------------------------------------------------

#: Magic byte sequence at the start of every message: ASCII "PU".
MAGIC = b"\x50\x55"

#: Protocol version this implementation speaks (major, minor).
VERSION = (0, 1)

#: Header layout: magic(2) major(1) minor(1) type_seq(uint32) payload_len(uint32).
_HEADER = struct.Struct("<2sBBII")
HEADER_SIZE = _HEADER.size  # 12
TRAILER_SIZE = 4            # Reserved(uint16) + CRC-16(uint16), present in every version


class MessageType(IntEnum):
    """Message-type constants (high 16 bits of the type/sequence word).

    A request and the response it triggers share the same constant; direction
    and payload distinguish them.
    """

    DESCRIPTION = 0x0001  # request descriptor / reply with JSON descriptor
    TELEMETRY = 0x0002    # request to start streaming / streamed telemetry frames
    STOP = 0x0003         # request to stop streaming / optional ack


#: On-wire footprint of each descriptor type, in 32-bit words.
#: Each value occupies an integer number of words (RFC 5.2).
TYPE_WORDS = {
    "int8": 1, "uint8": 1,
    "int16": 1, "uint16": 1,
    "int32": 1, "uint32": 1,
    "bitfield": 1,
    "float": 1,
    "int64": 2, "uint64": 2,
    "double": 2,
}
WORD_BYTES = 4


@dataclass
class Header:
    """A parsed pulseUDP message header."""

    message_type: int
    sequence: int
    payload_length: int
    version: tuple[int, int] = VERSION

    def pack(self) -> bytes:
        type_seq = ((self.message_type & 0xFFFF) << 16) | (self.sequence & 0xFFFF)
        return _HEADER.pack(MAGIC, self.version[0], self.version[1], type_seq,
                            self.payload_length)

    @classmethod
    def unpack(cls, data: bytes) -> "Header":
        if len(data) < HEADER_SIZE:
            raise ValueError("buffer shorter than header")
        magic, major, minor, type_seq, payload_len = _HEADER.unpack_from(data)
        if magic != MAGIC:
            raise ValueError(f"bad magic {magic!r}; expected {MAGIC!r}")
        return cls(
            message_type=(type_seq >> 16) & 0xFFFF,
            sequence=type_seq & 0xFFFF,
            payload_length=payload_len,
            version=(major, minor),
        )


def frame_size(fields: list[dict]) -> int:
    """Return the byte size of one telemetry frame for a descriptor's fields."""
    return sum(TYPE_WORDS[f["type"]] for f in fields) * WORD_BYTES


# TODO(v0.1): frame value decoding (read each field from the low bytes of its
# word span per the declared type) and CRC-16 validation (v1.0).
