"""Wire protocol for pulseUDP.

This module implements the framing described in ``spec/RFC-pulseUDP.md``:
the 12-byte message header, the merged type/sequence word, and the rules for
laying telemetry values into frames. Payload semantics are driven at runtime by
the JSON descriptor (see ``spec/Schema.json``).

Everything here is little-endian, matching the RFC.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field as _dc_field
from enum import IntEnum
from typing import Any, Dict, List, Optional

import numpy as np

# --- Constants ---------------------------------------------------------------

#: Magic byte sequence at the start of every message: ASCII "PU".
MAGIC = b"\x50\x55"

#: Protocol version this implementation speaks (major, minor).
VERSION = (1, 0)

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
    GET_CHANNELS = 0x0004  # v2.0: read the enabled-channel bitmap (RFC §4)
    SET_CHANNELS = 0x0005  # v2.0: set the enabled-channel bitmap (RFC §4)


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


def encode_channel_bitmap(enabled: List[bool]) -> bytes:
    """Pack channel-enable flags into the RFC §4 channel bitmap (v2.0).

    ⌈N/32⌉ ``uint32`` words sent **most-significant word first**; channel ``c``
    (0-based, descriptor order) is bit ``c`` of the whole integer, so channel 0
    is the LSB of the *last* word. Bytes within each word stay little-endian —
    the one exception to the little-endian rule of §2.
    """
    n = len(enabled)
    nwords = (n + 31) // 32 or 1
    words = [0] * nwords            # words[0] = least-significant (channels 0..31)
    for c, on in enumerate(enabled):
        if on:
            words[c >> 5] |= 1 << (c & 31)
    return b"".join(struct.pack("<I", w) for w in reversed(words))


def decode_channel_bitmap(payload: bytes, n: int) -> List[bool]:
    """Inverse of :func:`encode_channel_bitmap`: bitmap bytes -> ``n`` bools."""
    wire = [struct.unpack_from("<I", payload, i * 4)[0]
            for i in range(len(payload) // 4)]
    words = list(reversed(wire))    # words[0] = least-significant
    out = []
    for c in range(n):
        w = words[c >> 5] if (c >> 5) < len(words) else 0
        out.append(bool((w >> (c & 31)) & 1))
    return out


def crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT-FALSE over ``data`` (RFC §3.2).

    Parameters: poly ``0x1021``, init ``0xFFFF``, no input/output reflection,
    final XOR ``0x0000``. Check value for ``b"123456789"`` is ``0x29B1``.
    Used for the v2.0 trailer CRC, computed over Magic through end of Reserved
    (i.e. the whole message except the two CRC bytes). The 16-bit result is
    stored little-endian in the trailer.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
        crc &= 0xFFFF
    return crc


# --- Descriptor and telemetry decoding --------------------------------------

#: descriptor ``type`` token -> little-endian NumPy code. Each value reads from
#: the LOW bytes of its 32-bit word span (RFC 5.2); for narrow types the high
#: bytes of the word are sign/zero extension and are simply not read.
_NUMPY_CODE = {
    "int8": "i1", "uint8": "u1",
    "int16": "<i2", "uint16": "<u2",
    "int32": "<i4", "uint32": "<u4",
    "bitfield": "<u4",
    "float": "<f4",
    "int64": "<i8", "uint64": "<u8",
    "double": "<f8",
}

#: Field names treated as the time base, in preference order (case-insensitive).
_TIMESTAMP_NAMES = ("timestamp", "time")


@dataclass
class Field:
    """One telemetry field from a descriptor."""

    name: str
    type: str
    units: Optional[str] = None
    mult: Optional[float] = None
    bits: Optional[List[str]] = None

    @property
    def words(self) -> int:
        return TYPE_WORDS[self.type]

    @property
    def is_bitfield(self) -> bool:
        return self.type == "bitfield"


class Descriptor:
    """A parsed telemetry descriptor: the field layout plus a NumPy decode plan.

    Build one with :meth:`from_json` (the wire form), then call :meth:`decode`
    on a ``TELEMETRY`` payload to get a structured array of packets, or
    :meth:`channels` to get per-field physical-unit arrays.
    """

    def __init__(self, fields: List[Field], version: str,
                 id: Optional[Dict[str, Any]] = None,
                 raw: Optional[Dict[str, Any]] = None) -> None:
        if not fields:
            raise ValueError("descriptor has no fields")
        self.fields = fields
        self.version = version
        self.id = id
        self.raw = raw

        # Build the structured dtype: each field at its word offset, sized by
        # type; itemsize is the whole packet so frombuffer strides correctly.
        self._dtype_names: List[str] = []
        formats: List[str] = []
        offsets: List[int] = []
        seen: Dict[str, int] = {}
        word = 0
        for f in fields:
            dname = f.name
            if dname in seen:  # NumPy needs unique field names
                seen[dname] += 1
                dname = "{}__{}".format(f.name, seen[dname])
            else:
                seen[dname] = 0
            self._dtype_names.append(dname)
            formats.append(_NUMPY_CODE[f.type])
            offsets.append(word * WORD_BYTES)
            word += f.words
        self.packet_size = word * WORD_BYTES
        self._dtype = np.dtype({
            "names": self._dtype_names,
            "formats": formats,
            "offsets": offsets,
            "itemsize": self.packet_size,
        })

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_json(cls, data, schema: Optional[Dict[str, Any]] = None,
                  validate: bool = True) -> "Descriptor":
        """Parse a descriptor from a JSON string/bytes or an already-parsed dict.

        If ``schema`` is given and ``validate`` is true, the descriptor is
        validated against it (draft-07) before parsing.
        """
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8")
        obj = json.loads(data) if isinstance(data, str) else data

        if schema is not None and validate:
            import jsonschema  # local import: only needed when validating
            jsonschema.validate(instance=obj, schema=schema)

        fields = [
            Field(
                name=f["name"],
                type=f["type"],
                units=f.get("units"),
                mult=f.get("mult"),
                bits=f.get("bits"),
            )
            for f in obj["fields"]
        ]
        return cls(fields=fields, version=obj["version"],
                   id=obj.get("id"), raw=obj)

    def subset(self, indices: List[int]) -> "Descriptor":
        """A descriptor with only ``indices`` (ascending, descriptor order).

        Used to decode a v2.0 telemetry packet that carries only the enabled
        channels (RFC §4): the sub-descriptor's packet layout matches the wire.
        """
        fields = [self.fields[i] for i in indices]
        return Descriptor(fields=fields, version=self.version,
                          id=self.id, raw=self.raw)

    # -- decoding -------------------------------------------------------------

    def packets_in(self, payload_length: int) -> int:
        """Number of whole packets in a payload of ``payload_length`` bytes.

        Trailing bytes that cannot form a whole packet are padding (RFC 5.3).
        """
        return payload_length // self.packet_size

    def decode(self, payload: bytes) -> np.ndarray:
        """Decode a ``TELEMETRY`` payload into a structured array of packets.

        Returns one record per packet; access a column by its descriptor name
        (or :meth:`channels` for physical-unit conversion).
        """
        n = self.packets_in(len(payload))
        if n == 0:
            return np.empty(0, dtype=self._dtype)
        return np.frombuffer(payload, dtype=self._dtype, count=n)

    def channels(self, packets: np.ndarray) -> Dict[str, np.ndarray]:
        """Split a decoded packet array into per-field arrays.

        Numeric fields are returned as ``float64`` with ``mult`` applied;
        bitfields are returned as raw ``uint32`` (decode bits with
        :meth:`bit_traces`). Keys are the original descriptor names.
        """
        out: Dict[str, np.ndarray] = {}
        for f, dname in zip(self.fields, self._dtype_names):
            col = packets[dname]
            if f.is_bitfield:
                out[f.name] = col
            else:
                v = col.astype(np.float64)
                if f.mult is not None:
                    v = v * f.mult
                out[f.name] = v
        return out

    @staticmethod
    def bit_traces(field: Field, values: np.ndarray) -> Dict[str, np.ndarray]:
        """Expand a bitfield column into one 0/1 array per named bit.

        Bits map from the LSB up; ``Reserved`` names are skipped (RFC 5.2).
        """
        traces: Dict[str, np.ndarray] = {}
        for bit, name in enumerate(field.bits or []):
            if name == "Reserved":
                continue
            traces[name] = ((values >> bit) & 1).astype(np.uint8)
        return traces

    # -- time base ------------------------------------------------------------

    @property
    def timestamp_index(self) -> int:
        """Index of the field used as the time (X) base.

        A field named ``timestamp``/``time`` (case-insensitive) wins; otherwise
        the first field. The caller falls back to host arrival time if this
        field is unsuitable.
        """
        for i, f in enumerate(self.fields):
            if f.name.lower() in _TIMESTAMP_NAMES:
                return i
        return 0

    @property
    def timestamp_field(self) -> Field:
        return self.fields[self.timestamp_index]

    @property
    def plot_fields(self) -> List[Field]:
        """Fields to plot: everything except the time-base field."""
        ts = self.timestamp_index
        return [f for i, f in enumerate(self.fields) if i != ts]


# v2.0 trailer CRC (CRC-16/CCITT-FALSE, see crc16_ccitt above) and length-delimited
# multi-datagram reassembly (RFC §5.6) are validated/performed in client.py, where
# the whole (possibly reassembled) message is available.
