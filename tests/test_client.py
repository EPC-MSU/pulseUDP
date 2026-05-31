"""Tests for UdpClient v1.0 / v2.0 behavior (no sockets, no Qt).

Covers the version-dependent wire rules: outbound framing (sequence + CRC) and
inbound validation (CRC check, sequence-loss detection), exercising the receiver
logic directly via ``_handle_datagram`` / ``_send`` without touching the network.
"""

import json
import struct
import time

import pytest

from pulseudp.client import PROBE_VERSION, UdpClient
from pulseudp.protocol import (Descriptor, Header, MessageType, crc16_ccitt,
                               encode_channel_bitmap)

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
    if version[0] >= 2:
        crc = crc16_ccitt(framed)
        if not good_crc:
            crc ^= 0xFFFF        # corrupt a real CRC
    else:
        crc = 0xBEEF if not good_crc else 0   # v1.0 garbage to prove it's ignored
    return framed + struct.pack("<H", crc)


class _FakeSock:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))


# -- inbound: CRC validation --------------------------------------------------

def test_v1_good_crc_is_accepted():
    c = UdpClient("127.0.0.1", version=(2, 0))
    c._handle_datagram(_datagram(MessageType.STOP, 0, (2, 0)))
    assert c._stop_ack.is_set()            # STOP ack delivered


def test_v1_bad_crc_is_dropped_and_logged():
    events = []
    c = UdpClient("127.0.0.1", version=(2, 0), on_log=lambda e: events.append(e))
    c._handle_datagram(_datagram(MessageType.STOP, 0, (2, 0), good_crc=False))
    assert not c._stop_ack.is_set()        # corrupt datagram dropped
    assert [e.category for e in events] == ["crc"]


def test_v01_crc_field_is_ignored():
    # v1.0 carries a nonzero (garbage) CRC; the receiver must still accept it.
    events = []
    c = UdpClient("127.0.0.1", version=(1, 0), on_log=lambda e: events.append(e))
    c._handle_datagram(_datagram(MessageType.STOP, 0, (1, 0), good_crc=False))
    assert c._stop_ack.is_set()
    assert events == []


# -- inbound: sequence-loss detection -----------------------------------------

def test_v1_telemetry_sequence_gap_logs():
    events = []
    c = UdpClient("127.0.0.1", version=(2, 0), on_log=lambda e: events.append(e))
    c._handle_datagram(_datagram(MessageType.TELEMETRY, 5, (2, 0)))
    c._handle_datagram(_datagram(MessageType.TELEMETRY, 9, (2, 0)))   # gap of 4
    cats = [e.category for e in events]
    assert cats == ["seq_gap"]
    assert "lost" in events[0].message


def test_v01_telemetry_sequence_gap_is_silent():
    # The whole point: v1.0 must not emit sequence errors (field is ignored).
    events = []
    c = UdpClient("127.0.0.1", version=(1, 0), on_log=lambda e: events.append(e))
    c._handle_datagram(_datagram(MessageType.TELEMETRY, 5, (1, 0)))
    c._handle_datagram(_datagram(MessageType.TELEMETRY, 99, (1, 0)))
    assert events == []


# -- outbound: framing per version --------------------------------------------

def test_send_v1_stamps_sequence_and_valid_crc():
    c = UdpClient("127.0.0.1", version=(2, 0))
    c._sock = _FakeSock()
    for _ in range(3):
        c._send(MessageType.DESCRIPTION)
    seqs, crc_ok = [], []
    for data, _addr in c._sock.sent:
        hdr = Header.unpack(data)
        seqs.append(hdr.sequence)
        stored = int.from_bytes(data[-2:], "little")
        crc_ok.append(stored == crc16_ccitt(data[:-2]))
        assert hdr.version == (2, 0)
    assert seqs == [0, 1, 2]                # monotonic per-message
    assert all(crc_ok)                      # every request carries a real CRC


def test_send_v01_zeroes_sequence_and_crc():
    c = UdpClient("127.0.0.1", version=(1, 0))
    c._sock = _FakeSock()
    c._send(MessageType.DESCRIPTION)
    c._send(MessageType.STOP)
    for data, _addr in c._sock.sent:
        hdr = Header.unpack(data)
        assert hdr.version == (1, 0)
        assert hdr.sequence == 0
        assert int.from_bytes(data[-2:], "little") == 0   # CRC sent as zero


# -- multi-datagram reassembly (RFC §5.6, v2.0) -------------------------------

def _split(message, pieces):
    """Chop a contiguous message into ``pieces`` datagram-sized parts (RFC §5.6)."""
    size = (len(message) + pieces - 1) // pieces
    return [message[i:i + size] for i in range(0, len(message), size)]


# A payload too big for one datagram; the first piece carries the header, the
# rest are headerless continuation bytes.
_BIG_PAYLOAD = b"D" * 3000


def test_multidatagram_description_reassembles():
    c = UdpClient("127.0.0.1", version=(2, 0))
    msg = _datagram(MessageType.DESCRIPTION, 0, (2, 0), payload=_BIG_PAYLOAD)
    pieces = _split(msg, 3)
    assert len(pieces) == 3 and len(pieces[0]) < len(msg)   # genuinely multi
    for p in pieces:
        c._handle_datagram(p)
    reply_version, payload = c._desc_q.get_nowait()
    assert reply_version == (2, 0)
    assert payload == _BIG_PAYLOAD                          # whole payload recovered


def test_multidatagram_bad_crc_rejected():
    events = []
    c = UdpClient("127.0.0.1", version=(2, 0), on_log=lambda e: events.append(e))
    msg = _datagram(MessageType.DESCRIPTION, 0, (2, 0),
                    payload=_BIG_PAYLOAD, good_crc=False)
    for p in _split(msg, 3):
        c._handle_datagram(p)
    assert c._desc_q.empty()                                # whole message dropped
    assert [e.category for e in events] == ["crc"]          # checked after reassembly


def test_multidatagram_overrun_rejected():
    events = []
    c = UdpClient("127.0.0.1", version=(2, 0), on_log=lambda e: events.append(e))
    pieces = _split(_datagram(MessageType.DESCRIPTION, 0, (2, 0),
                              payload=_BIG_PAYLOAD), 3)
    c._handle_datagram(pieces[0])
    c._handle_datagram(pieces[1])
    c._handle_datagram(pieces[2] + b"EXTRA")                # one byte run too long
    assert c._reasm is None
    assert c._desc_q.empty()
    assert [e.category for e in events] == ["reasm"]


def test_multidatagram_timeout_discards_then_recovers():
    events = []
    c = UdpClient("127.0.0.1", version=(2, 0), on_log=lambda e: events.append(e))
    pieces = _split(_datagram(MessageType.DESCRIPTION, 0, (2, 0),
                              payload=_BIG_PAYLOAD), 3)
    c._handle_datagram(pieces[0])
    assert c._reasm is not None                             # reassembly opened
    c._reasm.deadline = time.monotonic() - 0.001            # force past the deadline
    c._expire_reassembly()
    assert c._reasm is None
    assert [e.category for e in events] == ["reasm"]
    assert "timeout" in events[0].message
    # A complete single-datagram reply now lands normally (transaction re-requested).
    c._handle_datagram(_datagram(MessageType.DESCRIPTION, 1, (2, 0), payload=_DESC_JSON))
    _version, payload = c._desc_q.get_nowait()
    assert payload == _DESC_JSON


def test_v01_never_reassembles():
    # v1.0 has no multi-datagram support: a short datagram is just a short
    # datagram, never the start of a reassembly.
    c = UdpClient("127.0.0.1", version=(1, 0))
    msg = _datagram(MessageType.DESCRIPTION, 0, (1, 0), payload=_BIG_PAYLOAD)
    c._handle_datagram(_split(msg, 3)[0])                   # only the first piece
    assert c._reasm is None                                 # no reassembly state


# -- channel selection (RFC §4, v2.0) -----------------------------------------

def _channel_client(reply_enabled, version=(2, 0)):
    """A v2.0 client whose ``_send`` answers GET/SET_CHANNELS with a canned bitmap.

    The descriptor is the 2-field ``_DESC_JSON`` (Timestamp, V), so a reply of
    ``[True, False]`` enables only the time base.
    """
    c = UdpClient("127.0.0.1", version=version)
    c._sock = _FakeSock()
    c.version = version
    c.descriptor = Descriptor.from_json(_DESC_JSON)
    c._set_active([True] * len(c.descriptor.fields))

    def fake_send(mtype, payload=b""):
        if mtype in (MessageType.GET_CHANNELS, MessageType.SET_CHANNELS):
            bm = encode_channel_bitmap(reply_enabled)
            c._handle_datagram(_datagram(mtype, 0, version, payload=bm))

    c._send = fake_send
    return c


def test_get_channels_reads_bitmap_and_sets_active_subset():
    c = _channel_client([True, False])              # only the time base enabled
    enabled = c.get_channels(timeout=0.3, retries=2)
    assert enabled == [True, False]
    assert c.enabled_channels == [True, False]
    # active descriptor is now the 1-field subset used to decode telemetry
    assert [f.name for f in c._active_descriptor.fields] == ["Timestamp"]


def test_set_channels_returns_server_accepted_set():
    # Client asks for both; server accepts only the first — the reply wins.
    c = _channel_client([True, False])
    accepted = c.set_channels([True, True], timeout=0.3, retries=2)
    assert accepted == [True, False]                 # server's set, not requested
    assert c.enabled_channels == [True, False]
    assert [f.name for f in c._active_descriptor.fields] == ["Timestamp"]


def test_all_enabled_uses_full_descriptor():
    c = _channel_client([True, True])
    c.set_channels([True, True], timeout=0.3, retries=2)
    assert c._active_descriptor is c.descriptor     # no subset when all enabled


def test_v01_channels_are_immutable_and_offline():
    c = _channel_client([True, False], version=(1, 0))
    assert c.get_channels() == [True, True]          # all enabled, immutable
    assert c.set_channels([False, False]) == [True, True]
    assert c._sock.sent == []                        # nothing put on the wire


# -- version negotiation (RFC §6.1) -------------------------------------------

def _client_replying(reply_version, probe_log=None):
    """A client whose ``_send`` answers each DESCRIPTION with a canned reply.

    Models a reachable server that stamps its reply with ``reply_version``
    regardless of the probe's version. ``probe_log`` (if given) records the
    version the client was framing each request at, to assert the probe is v2.0.
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
    c = _client_replying((2, 0), probe_log=probes)
    desc = c.request_descriptor(timeout=0.3, retries=2)
    assert probes[0] == PROBE_VERSION == (2, 0)   # opening probe is v2.0
    assert c.version == (2, 0)                     # fixated to the reply
    assert len(desc.fields) == 2


def test_negotiation_fixes_v01_in_one_round_trip():
    # v1.0 server answers the v2.0 probe and reveals v1.0; client downgrades.
    events = []
    c = _client_replying((1, 0))
    c._on_log = lambda e: events.append(e)
    c.request_descriptor(timeout=0.3, retries=2)
    assert c.version == (1, 0)
    assert any("negotiated protocol v1.0" in e.message for e in events)


def test_negotiation_rejects_unsupported_version():
    # A reply in a parseable major but unknown version → incompatible.
    c = _client_replying((2, 5))
    with pytest.raises(RuntimeError, match="unsupported protocol v2.5"):
        c.request_descriptor(timeout=0.3, retries=2)


def test_negotiation_no_reply_times_out():
    # Unreachable endpoint: the server never answers (no version branch).
    c = UdpClient("127.0.0.1")
    c._sock = _FakeSock()
    c._send = lambda mtype, payload=b"": None
    with pytest.raises(TimeoutError):
        c.request_descriptor(timeout=0.05, retries=2)
