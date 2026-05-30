"""Tests for UdpClient v0.1 / v1.0 behavior (no sockets, no Qt).

Covers the version-dependent wire rules: outbound framing (sequence + CRC) and
inbound validation (CRC check, sequence-loss detection), exercising the receiver
logic directly via ``_handle_datagram`` / ``_send`` without touching the network.
"""

import json
import struct

import pytest

from pulseudp.client import PROBE_VERSION, UdpClient
from pulseudp.protocol import Header, MessageType, crc16_ccitt

# Minimal structurally-valid descriptor for the negotiation tests (schema=None,
# so it is parsed but not schema-validated).
_DESC_JSON = json.dumps({
    "version": "1.0.0",
    "fields": [
        {"name": "Timestamp", "type": "uint32", "mult": 0.001, "units": "s"},
        {"name": "V", "type": "int16"},
    ],
}).encode("utf-8")


def _datagram(mtype, seq, version, payload=b"", good_crc=True,
              reserved=b"\x00\x00"):
    """Build an on-wire datagram for the given version (RFC §3)."""
    hdr = Header(message_type=int(mtype), sequence=seq,
                 payload_length=len(payload), version=version)
    framed = hdr.pack() + payload + reserved
    if version[0] >= 1:
        crc = crc16_ccitt(framed)
        if not good_crc:
            crc ^= 0xFFFF        # corrupt a real CRC
    else:
        crc = 0xBEEF if not good_crc else 0   # v0.1 garbage to prove it's ignored
    return framed + struct.pack("<H", crc)


class _FakeSock:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))


# -- inbound: CRC validation --------------------------------------------------

def test_v1_good_crc_is_accepted():
    c = UdpClient("127.0.0.1", version=(1, 0))
    c._handle_datagram(_datagram(MessageType.STOP, 0, (1, 0)))
    assert c._stop_ack.is_set()            # STOP ack delivered


def test_v1_bad_crc_is_dropped_and_logged():
    events = []
    c = UdpClient("127.0.0.1", version=(1, 0), on_log=lambda e: events.append(e))
    c._handle_datagram(_datagram(MessageType.STOP, 0, (1, 0), good_crc=False))
    assert not c._stop_ack.is_set()        # corrupt datagram dropped
    assert [e.category for e in events] == ["crc"]


def test_v01_crc_field_is_ignored():
    # v0.1 carries a nonzero (garbage) CRC; the receiver must still accept it.
    events = []
    c = UdpClient("127.0.0.1", version=(0, 1), on_log=lambda e: events.append(e))
    c._handle_datagram(_datagram(MessageType.STOP, 0, (0, 1), good_crc=False))
    assert c._stop_ack.is_set()
    assert events == []


# -- inbound: sequence-loss detection -----------------------------------------

def test_v1_telemetry_sequence_gap_logs():
    events = []
    c = UdpClient("127.0.0.1", version=(1, 0), on_log=lambda e: events.append(e))
    c._handle_datagram(_datagram(MessageType.TELEMETRY, 5, (1, 0)))
    c._handle_datagram(_datagram(MessageType.TELEMETRY, 9, (1, 0)))   # gap of 4
    cats = [e.category for e in events]
    assert cats == ["seq_gap"]
    assert "lost" in events[0].message


def test_v01_telemetry_sequence_gap_is_silent():
    # The whole point: v0.1 must not emit sequence errors (field is ignored).
    events = []
    c = UdpClient("127.0.0.1", version=(0, 1), on_log=lambda e: events.append(e))
    c._handle_datagram(_datagram(MessageType.TELEMETRY, 5, (0, 1)))
    c._handle_datagram(_datagram(MessageType.TELEMETRY, 99, (0, 1)))
    assert events == []


# -- outbound: framing per version --------------------------------------------

def test_send_v1_stamps_sequence_and_valid_crc():
    c = UdpClient("127.0.0.1", version=(1, 0))
    c._sock = _FakeSock()
    for _ in range(3):
        c._send(MessageType.DESCRIPTION)
    seqs, crc_ok = [], []
    for data, _addr in c._sock.sent:
        hdr = Header.unpack(data)
        seqs.append(hdr.sequence)
        stored = int.from_bytes(data[-2:], "little")
        crc_ok.append(stored == crc16_ccitt(data[:-2]))
        assert hdr.version == (1, 0)
    assert seqs == [0, 1, 2]                # monotonic per-message
    assert all(crc_ok)                      # every request carries a real CRC


def test_send_v01_zeroes_sequence_and_crc():
    c = UdpClient("127.0.0.1", version=(0, 1))
    c._sock = _FakeSock()
    c._send(MessageType.DESCRIPTION)
    c._send(MessageType.STOP)
    for data, _addr in c._sock.sent:
        hdr = Header.unpack(data)
        assert hdr.version == (0, 1)
        assert hdr.sequence == 0
        assert int.from_bytes(data[-2:], "little") == 0   # CRC sent as zero


# -- version negotiation (RFC §6.1) -------------------------------------------

def _client_replying(reply_version, probe_log=None):
    """A client whose ``_send`` answers each DESCRIPTION with a canned reply.

    Models a reachable server that stamps its reply with ``reply_version``
    regardless of the probe's version. ``probe_log`` (if given) records the
    version the client was framing each request at, to assert the probe is v1.0.
    """
    c = UdpClient("127.0.0.1")
    c._sock = _FakeSock()

    def fake_send(mtype, payload=b""):
        if probe_log is not None:
            probe_log.append(c.version)
        if mtype == MessageType.DESCRIPTION:
            c._handle_datagram(
                _datagram(MessageType.DESCRIPTION, 0, reply_version,
                          payload=_DESC_JSON))

    c._send = fake_send
    return c


def test_negotiation_probes_at_v1_and_fixes_v1():
    probes = []
    c = _client_replying((1, 0), probe_log=probes)
    desc = c.request_descriptor(timeout=0.3, retries=2)
    assert probes[0] == PROBE_VERSION == (1, 0)   # opening probe is v1.0
    assert c.version == (1, 0)                     # fixated to the reply
    assert len(desc.fields) == 2


def test_negotiation_fixes_v01_in_one_round_trip():
    # v0.1 server answers the v1.0 probe and reveals v0.1; client downgrades.
    events = []
    c = _client_replying((0, 1))
    c._on_log = lambda e: events.append(e)
    c.request_descriptor(timeout=0.3, retries=2)
    assert c.version == (0, 1)
    assert any("negotiated protocol v0.1" in e.message for e in events)


def test_negotiation_rejects_unsupported_version():
    # A reply in a parseable major but unknown version → incompatible.
    c = _client_replying((1, 5))
    with pytest.raises(RuntimeError, match="unsupported protocol v1.5"):
        c.request_descriptor(timeout=0.3, retries=2)


def test_negotiation_no_reply_times_out():
    # Unreachable endpoint: the server never answers (no version branch).
    c = UdpClient("127.0.0.1")
    c._sock = _FakeSock()
    c._send = lambda mtype, payload=b"": None
    with pytest.raises(TimeoutError):
        c.request_descriptor(timeout=0.05, retries=2)
