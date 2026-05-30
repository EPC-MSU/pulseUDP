"""Tests for descriptor decoding and header framing (no Qt, no sockets)."""

import struct

import numpy as np

from pulseudp.protocol import Descriptor, Header, MessageType, crc16_ccitt


EXAMPLE = {
    "version": "1.0.0",
    "fields": [
        {"name": "Timestamp", "type": "uint32", "mult": 0.001, "units": "s"},
        {"name": "VoltageA", "type": "int16", "mult": 0.01, "units": "V"},
        {"name": "Current", "type": "int16", "mult": 0.001, "units": "A"},
        {"name": "Flags", "type": "bitfield",
         "bits": ["Run", "Reserved", "Fault"]},
        {"name": "Counter", "type": "int32"},
    ],
}


def _pack(ts, va, cur, flags, counter):
    return (struct.pack("<I", ts)
            + struct.pack("<hh", va, 0)     # int16 in low half of its word
            + struct.pack("<hh", cur, 0)
            + struct.pack("<I", flags)
            + struct.pack("<i", counter))


def test_packet_size_and_timestamp_detection():
    d = Descriptor.from_json(EXAMPLE)
    assert d.packet_size == 5 * 4           # five single-word fields
    assert d.timestamp_field.name == "Timestamp"
    assert [f.name for f in d.plot_fields] == ["VoltageA", "Current", "Flags", "Counter"]


def test_decode_roundtrip_applies_mult_and_signs():
    d = Descriptor.from_json(EXAMPLE)
    payload = _pack(1000, 250, -5, 0b101, -42) + _pack(2000, -250, 7, 0b100, 99)
    packets = d.decode(payload)
    assert packets.size == 2
    ch = d.channels(packets)
    assert np.allclose(ch["Timestamp"], [1.0, 2.0])      # uint32 * 0.001
    assert np.allclose(ch["VoltageA"], [2.5, -2.5])      # int16 * 0.01
    assert np.allclose(ch["Current"], [-0.005, 0.007])   # int16 * 0.001
    assert np.allclose(ch["Counter"], [-42.0, 99.0])     # int32, no mult


def test_trailing_pad_is_ignored():
    d = Descriptor.from_json(EXAMPLE)
    payload = _pack(1, 0, 0, 0, 0) + b"\x00\x00\x00"     # one packet + 3 pad bytes
    assert d.packets_in(len(payload)) == 1
    assert d.decode(payload).size == 1


def test_bit_traces_skip_reserved():
    d = Descriptor.from_json(EXAMPLE)
    flags = next(f for f in d.fields if f.is_bitfield)
    values = np.array([0b101, 0b100], dtype=np.uint32)   # Run+Fault, then Fault
    traces = Descriptor.bit_traces(flags, values)
    assert set(traces) == {"Run", "Fault"}               # Reserved (bit 1) dropped
    assert np.array_equal(traces["Run"], [1, 0])
    assert np.array_equal(traces["Fault"], [1, 1])       # bit 2


def test_header_type_sequence_word_roundtrip():
    h = Header(message_type=int(MessageType.TELEMETRY), sequence=0x1234,
               payload_length=32)
    again = Header.unpack(h.pack())
    assert again.message_type == int(MessageType.TELEMETRY)
    assert again.sequence == 0x1234
    assert again.payload_length == 32


def test_crc16_ccitt_check_value():
    # RFC §3.2 conformance check: CRC-16/CCITT-FALSE of "123456789" is 0x29B1.
    assert crc16_ccitt(b"123456789") == 0x29B1


def test_crc16_ccitt_empty_is_init():
    # No bytes consumed -> register stays at the init value 0xFFFF.
    assert crc16_ccitt(b"") == 0xFFFF


def test_crc16_ccitt_covers_whole_frame_except_crc():
    # A valid v1.0 trailer: CRC over Magic..end of Reserved round-trips.
    hdr = Header(message_type=int(MessageType.TELEMETRY), sequence=7,
                 payload_length=0, version=(1, 0)).pack()
    framed = hdr + b"\x00\x00"                 # + empty payload + Reserved
    crc = crc16_ccitt(framed)
    datagram = framed + struct.pack("<H", crc)
    assert crc16_ccitt(datagram[:-2]) == struct.unpack_from("<H", datagram, len(datagram) - 2)[0]
