"""pulseUDP framing and descriptor decoding, per spec/RFC-pulseUDP.md (little-endian)."""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field as _dc_field
from enum import IntEnum
from typing import Any, Dict, List, Optional

import numpy as np

MAGIC = b"\x50\x55"

VERSION = (1, 0)

#: Header layout: magic(2) major(1) minor(1) type_seq(uint32) payload_len(uint32).
_HEADER = struct.Struct("<2sBBII")
HEADER_SIZE = _HEADER.size
TRAILER_SIZE = 4            # Reserved(uint16) + CRC-16(uint16), present in every version


class MessageType(IntEnum):
    """High 16 bits of the type/sequence word; a request and its response share one (RFC §4)."""

    DESCRIPTION = 0x0001
    TELEMETRY = 0x0002
    STOP = 0x0003
    GET_CHANNELS = 0x0004  # v2.0
    SET_CHANNELS = 0x0005  # v2.0


#: On-wire footprint of each descriptor type, in 32-bit words (RFC §5.2).
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
    return sum(TYPE_WORDS[f["type"]] for f in fields) * WORD_BYTES


def encode_channel_bitmap(enabled: List[bool]) -> bytes:
    """Pack channel-enable flags into the RFC §4 bitmap (most-significant word first)."""
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
    words = list(reversed(wire))
    out = []
    for c in range(n):
        w = words[c >> 5] if (c >> 5) < len(words) else 0
        out.append(bool((w >> (c & 31)) & 1))
    return out


def crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT-FALSE for the v2.0 trailer (RFC §3.2)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
        crc &= 0xFFFF
    return crc


#: descriptor ``type`` -> NumPy code, read from the low bytes of the word span (RFC §5.2).
_NUMPY_CODE = {
    "int8": "i1", "uint8": "u1",
    "int16": "<i2", "uint16": "<u2",
    "int32": "<i4", "uint32": "<u4",
    "bitfield": "<u4",
    "float": "<f4",
    "int64": "<i8", "uint64": "<u8",
    "double": "<f8",
}


@dataclass
class Field:
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

    @property
    def dtype(self) -> np.dtype:
        """Native NumPy dtype of the wire value (before ``mult``)."""
        return np.dtype(_NUMPY_CODE[self.type])

    @property
    def scale(self) -> Optional[float]:
        """``mult`` to apply on read, or None when absent or 1."""
        return None if self.mult is None or self.mult == 1 else float(self.mult)


class Descriptor:
    """A parsed telemetry descriptor: field layout plus a NumPy decode plan."""

    def __init__(self, fields: List[Field], version: str,
                 id: Optional[Dict[str, Any]] = None,
                 raw: Optional[Dict[str, Any]] = None) -> None:
        if not fields:
            raise ValueError("descriptor has no fields")
        self.fields = fields
        self.version = version
        self.id = id
        self.raw = raw

        # itemsize is the whole packet so frombuffer strides correctly.
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

    @classmethod
    def from_json(cls, data, schema: Optional[Dict[str, Any]] = None,
                  validate: bool = True) -> "Descriptor":
        """Parse from wire bytes, a JSON string or a dict; validate if ``schema`` is given."""
        if isinstance(data, (bytes, bytearray)):
            # Wire form (RFC §5.1): cut at the first NUL, dropping terminator and padding.
            nul = data.find(b"\x00")
            if nul != -1:
                data = data[:nul]
            data = data.decode("utf-8")
        obj = json.loads(data) if isinstance(data, str) else data

        if schema is not None and validate:
            import jsonschema
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
        """Layout of a v2.0 packet carrying only the enabled channels (RFC §4)."""
        fields = [self.fields[i] for i in indices]
        return Descriptor(fields=fields, version=self.version,
                          id=self.id, raw=self.raw)

    def packets_in(self, payload_length: int) -> int:
        """Whole packets in the payload; a trailing remainder is padding (RFC §5.3)."""
        return payload_length // self.packet_size

    def decode(self, payload: bytes) -> np.ndarray:
        """Decode a ``TELEMETRY`` payload into a structured array, one record per packet."""
        n = self.packets_in(len(payload))
        if n == 0:
            return np.empty(0, dtype=self._dtype)
        return np.frombuffer(payload, dtype=self._dtype, count=n)

    def channels(self, packets: np.ndarray) -> Dict[str, np.ndarray]:
        """Per-field raw wire arrays in their native dtype; ``mult`` is applied on read."""
        return {f.name: packets[dname]
                for f, dname in zip(self.fields, self._dtype_names)}

    @staticmethod
    def bit_traces(field: Field, values: np.ndarray) -> Dict[str, np.ndarray]:
        """One 0/1 array per named bit, LSB first, skipping ``Reserved`` (RFC §5.2)."""
        traces: Dict[str, np.ndarray] = {}
        for bit, name in enumerate(field.bits or []):
            if name == "Reserved":
                continue
            traces[name] = ((values >> bit) & 1).astype(np.uint8)
        return traces

    @property
    def plot_fields(self) -> List[Field]:
        """Every field; X is the client's sample counter, not a descriptor field."""
        return list(self.fields)


# v2.0 CRC validation and multi-datagram reassembly (RFC §5.6) live in client.py.
