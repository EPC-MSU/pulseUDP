"""Qt-free pulseUDP client: socket, receiver thread and RFC §4 transactions."""

from __future__ import annotations

import queue
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from .protocol import (HEADER_SIZE, TRAILER_SIZE, WORD_BYTES, Descriptor,
                       Header, MessageType, crc16_ccitt, decode_channel_bitmap,
                       encode_channel_bitmap)

DEFAULT_PORT = 2102
#: Highest first; the first is the DESCRIPTION probe version (RFC §6.1).
SUPPORTED_VERSIONS = ((2, 0), (1, 0))
PROBE_VERSION = SUPPORTED_VERSIONS[0]
_RECV_BUFSIZE = 65535
_SOCK_TIMEOUT = 0.2    # s, receiver loop wakeup so it can observe close()
_REASM_TIMEOUT = 0.3   # s, multi-datagram reassembly deadline (RFC §5.6)


@dataclass
class LogEvent:
    """The consumer adds the wall clock."""

    category: str    # bad_magic | bad_version | short | align | decode | seq_gap | crc | reasm | info | error
    message: str
    level: str = "warning"   # info | warning | error


TelemetryCb = Callable[[Dict[str, np.ndarray]], None]  # per-field physical arrays
LogCb = Callable[[LogEvent], None]
StateCb = Callable[[str, str], None]          # (state, detail)


@dataclass
class _Reassembly:
    """In-progress multi-datagram message (RFC §5.6); only the first piece has a header."""

    header: Header
    expected: int
    deadline: float       # time.monotonic()
    buf: bytearray


class UdpClient:
    """Single-server client; callbacks run on the receiver thread (docs/gui-design.md)."""

    def __init__(self, host: str, port: int = DEFAULT_PORT,
                 version: Tuple[int, int] = PROBE_VERSION,
                 schema: Optional[Dict[str, Any]] = None,
                 on_telemetry: Optional[TelemetryCb] = None,
                 on_log: Optional[LogCb] = None,
                 on_state: Optional[StateCb] = None) -> None:
        self.host = host
        self.port = port
        self.version = version
        self.schema = schema
        self._on_telemetry = on_telemetry
        self._on_log = on_log
        self._on_state = on_state

        self.descriptor: Optional[Descriptor] = None
        # The active descriptor matches the enabled channels' wire layout (RFC §4).
        self.enabled_channels: Optional[List[bool]] = None
        self._active_descriptor: Optional[Descriptor] = None

        self._sock: Optional[socket.socket] = None
        self._rx_thread: Optional[threading.Thread] = None
        self._closing = threading.Event()

        # (reply_version, payload): the reply's version is the negotiated one.
        self._desc_q: "queue.Queue[Tuple[Tuple[int, int], bytes]]" = queue.Queue()
        # GET_ and SET_CHANNELS are serialized, so they share one queue.
        self._chan_q: "queue.Queue[bytes]" = queue.Queue()
        self._stop_ack = threading.Event()
        self._stream_started = threading.Event()

        self._streaming = False
        self._last_seq: Optional[int] = None   # last RX sequence (loss detection)
        self._tx_seq = 0                        # next TX sequence (v2.0 outbound)
        self._reasm: Optional[_Reassembly] = None  # multi-datagram in progress

    def open(self) -> None:
        if self._sock is not None:
            return
        self._closing.clear()
        self._tx_seq = 0
        self._last_seq = None
        self._reasm = None
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(_SOCK_TIMEOUT)
        self._sock = sock
        self._rx_thread = threading.Thread(
            target=self._rx_loop, name="pulseudp-rx", daemon=True)
        self._rx_thread.start()

    def close(self) -> None:
        self._closing.set()
        if self._rx_thread is not None:
            self._rx_thread.join(timeout=1.0)
            self._rx_thread = None
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        self._streaming = False
        self._emit_state("disconnected", "")

    def request_descriptor(self, timeout: float = 1.0, retries: int = 3) -> Descriptor:
        """Negotiate the version (RFC §6.1); RuntimeError if unsupported, TimeoutError if no reply."""
        self._require_open()
        self._emit_state("connecting", "{}:{}".format(self.host, self.port))
        self.version = PROBE_VERSION
        self._drain(self._desc_q)
        for attempt in range(retries):
            self._send(MessageType.DESCRIPTION)
            try:
                reply_version, payload = self._desc_q.get(timeout=timeout)
            except queue.Empty:
                self._log("info", "info",
                          "DESCRIPTION timeout, retry {}/{}".format(attempt + 1, retries))
                continue
            if reply_version not in SUPPORTED_VERSIONS:
                self._emit_state("error", "unsupported server protocol "
                                 "v{}.{}".format(*reply_version))
                raise RuntimeError(
                    "server speaks unsupported protocol v{}.{}".format(
                        *reply_version))
            self.version = reply_version
            self._log("info", "info",
                      "negotiated protocol v{}.{}".format(*self.version))
            descriptor = Descriptor.from_json(
                payload, schema=self.schema, validate=self.schema is not None)
            self.descriptor = descriptor
            # All enabled until a v2.0 caller narrows it.
            self._set_active([True] * len(descriptor.fields))
            self._emit_state("connected",
                             "protocol v{}.{} · descriptor v{} · {} fields".format(
                                 self.version[0], self.version[1],
                                 descriptor.version, len(descriptor.fields)))
            return descriptor
        self._emit_state("error", "no descriptor response")
        raise TimeoutError("no DESCRIPTION response after {} attempts".format(retries))

    def start_stream(self, timeout: float = 1.0, retries: int = 3) -> None:
        """Send ``TELEMETRY``; the first streamed packet is the ack (RFC §4)."""
        self._require_open()
        if self.descriptor is None:
            raise RuntimeError("request the descriptor before starting the stream")
        self._last_seq = None
        self._stream_started.clear()
        for attempt in range(retries):
            self._send(MessageType.TELEMETRY)
            if self._stream_started.wait(timeout=timeout):
                self._streaming = True
                self._emit_state("streaming", "")
                return
            self._log("info", "info",
                      "TELEMETRY start timeout, retry {}/{}".format(attempt + 1, retries))
        self._emit_state("error", "stream did not start")
        raise TimeoutError("telemetry stream did not start")

    def stop_stream(self, timeout: float = 1.0, retries: int = 3) -> None:
        """Send ``STOP`` and wait for the mandatory ack (RFC §4)."""
        self._require_open()
        self._stop_ack.clear()
        for attempt in range(retries):
            self._send(MessageType.STOP)
            if self._stop_ack.wait(timeout=timeout):
                break
            self._log("info", "info",
                      "STOP ack timeout, retry {}/{}".format(attempt + 1, retries))
        self._streaming = False
        self._emit_state("stopped", "")

    def get_channels(self, timeout: float = 1.0, retries: int = 3) -> List[bool]:
        """One bool per descriptor field (RFC §4); all True under v1.0."""
        self._require_open()
        if self.descriptor is None:
            raise RuntimeError("request the descriptor before reading channels")
        n = len(self.descriptor.fields)
        if self.version[0] < 2:
            self._set_active([True] * n)
            return list(self.enabled_channels or [])
        self._drain(self._chan_q)
        for attempt in range(retries):
            self._send(MessageType.GET_CHANNELS)
            try:
                payload = self._chan_q.get(timeout=timeout)
            except queue.Empty:
                self._log("info", "info",
                          "GET_CHANNELS timeout, retry {}/{}".format(attempt + 1, retries))
                continue
            enabled = decode_channel_bitmap(payload, n)
            self._set_active(enabled)
            return enabled
        raise TimeoutError("no GET_CHANNELS response after {} attempts".format(retries))

    def set_channels(self, enabled, timeout: float = 1.0,
                     retries: int = 3) -> List[bool]:
        """Returns the set the server accepted, which is authoritative (RFC §4)."""
        self._require_open()
        if self.descriptor is None:
            raise RuntimeError("request the descriptor before setting channels")
        n = len(self.descriptor.fields)
        if self.version[0] < 2:
            self._set_active([True] * n)
            return list(self.enabled_channels or [])
        payload = encode_channel_bitmap([bool(x) for x in enabled])
        self._drain(self._chan_q)
        for attempt in range(retries):
            self._send(MessageType.SET_CHANNELS, payload)
            try:
                reply = self._chan_q.get(timeout=timeout)
            except queue.Empty:
                self._log("info", "info",
                          "SET_CHANNELS timeout, retry {}/{}".format(attempt + 1, retries))
                continue
            accepted = decode_channel_bitmap(reply, n)
            self._set_active(accepted)
            return accepted
        raise TimeoutError("no SET_CHANNELS response after {} attempts".format(retries))

    def _set_active(self, enabled: List[bool]) -> None:
        self.enabled_channels = list(enabled)
        if self.descriptor is None or all(enabled):
            self._active_descriptor = self.descriptor
        else:
            idx = [i for i, on in enumerate(enabled) if on]
            self._active_descriptor = (self.descriptor.subset(idx) if idx
                                       else self.descriptor)

    def _rx_loop(self) -> None:
        sock = self._sock
        assert sock is not None
        while not self._closing.is_set():
            try:
                data, _addr = sock.recvfrom(_RECV_BUFSIZE)
            except socket.timeout:
                # Expire a stalled reassembly even when nothing more arrives.
                self._expire_reassembly()
                continue
            except OSError:
                break
            self._handle_datagram(data)

    def _handle_datagram(self, data: bytes) -> None:
        # Continuation pieces carry no header (RFC §5.6).
        self._expire_reassembly()
        if self._reasm is not None:
            self._feed_reassembly(data)
            return

        try:
            header = Header.unpack(data)
        except ValueError as exc:
            msg = str(exc)
            if "magic" in msg:
                self._log("bad_magic", "warning", "ignored datagram: " + msg)
            else:
                self._log("short", "warning", "short datagram: " + msg)
            return

        if header.version[0] not in (1, 2):
            self._log("bad_version", "warning",
                      "unsupported version {}.{}".format(*header.version))
            return

        # Shorter than declared: a v2.0 multi-datagram message starts (RFC §5.6).
        expected = HEADER_SIZE + header.payload_length + TRAILER_SIZE
        if header.version[0] >= 2 and len(data) < expected:
            self._reasm = _Reassembly(
                header=header, expected=expected,
                deadline=time.monotonic() + _REASM_TIMEOUT,
                buf=bytearray(data))
            return

        self._process_message(header, data)

    def _feed_reassembly(self, data: bytes) -> None:
        r = self._reasm
        assert r is not None
        r.buf += data
        if len(r.buf) < r.expected:
            return
        if len(r.buf) > r.expected:
            # A lost/reordered/duplicated piece; all-or-nothing (RFC §5.6).
            self._log("reasm", "warning",
                      "reassembled {} B exceeds expected {} B; message dropped".format(
                          len(r.buf), r.expected))
            self._reasm = None
            return
        header, message = r.header, bytes(r.buf)
        self._reasm = None
        self._process_message(header, message)

    def _expire_reassembly(self) -> None:
        r = self._reasm
        if r is not None and time.monotonic() > r.deadline:
            self._log("reasm", "warning",
                      "reassembly timeout: discarded {}/{} B; message will be "
                      "re-requested".format(len(r.buf), r.expected))
            self._reasm = None

    def _process_message(self, header: Header, data: bytes) -> None:
        """``data`` is header ‖ payload ‖ trailer, single-datagram or reassembled."""
        if header.version[0] >= 2:
            # Before anything else: a bad CRC makes the sequence number untrustworthy.
            if not self._crc_ok(data):
                self._log("crc", "warning",
                          "CRC mismatch (type 0x{:04x}); message dropped".format(
                              header.message_type))
                return
            if header.message_type == MessageType.TELEMETRY:
                self._check_sequence(header.sequence)

        if header.payload_length % WORD_BYTES != 0:
            self._log("align", "warning",
                      "payload length {} B is not 32-bit word aligned (RFC §3); "
                      "message type 0x{:04x} processed tolerantly".format(
                          header.payload_length, header.message_type))

        mtype = header.message_type
        avail = len(data) - HEADER_SIZE - TRAILER_SIZE
        n_payload = max(0, min(header.payload_length, avail))
        payload = data[HEADER_SIZE:HEADER_SIZE + n_payload]

        if mtype == MessageType.DESCRIPTION:
            self._desc_q.put((header.version, payload))
        elif mtype in (MessageType.GET_CHANNELS, MessageType.SET_CHANNELS):
            self._chan_q.put(payload)
        elif mtype == MessageType.STOP:
            self._stop_ack.set()
        elif mtype == MessageType.TELEMETRY:
            if not self._stream_started.is_set():
                self._stream_started.set()   # first packet = ack
            self._dispatch_telemetry(payload)
        else:
            self._log("decode", "warning",
                      "unknown message type 0x{:04x}".format(mtype))

    def _dispatch_telemetry(self, payload: bytes) -> None:
        dd = self._active_descriptor or self.descriptor
        if dd is None or self._on_telemetry is None:
            return
        if dd.packet_size == 0:
            return                      # no channels enabled
        if len(payload) % dd.packet_size != 0:
            # Trailing pad is allowed (RFC §5.3).
            if dd.packets_in(len(payload)) == 0:
                self._log("decode", "warning",
                          "telemetry payload {} B < packet size {} B".format(
                              len(payload), dd.packet_size))
                return
        packets = dd.decode(payload)
        if packets.size:
            self._on_telemetry(dd.channels(packets))

    def _crc_ok(self, data: bytes) -> bool:
        """RFC §3.2: the CRC covers Magic..Reserved."""
        if len(data) < HEADER_SIZE + TRAILER_SIZE:
            return False
        stored = int.from_bytes(data[-2:], "little")
        return stored == crc16_ccitt(data[:-2])

    def _check_sequence(self, seq: int) -> None:
        if self._last_seq is not None:
            expected = (self._last_seq + 1) & 0xFFFF
            if seq != expected:
                gap = (seq - expected) & 0xFFFF
                self._log("seq_gap", "warning",
                          "sequence gap: expected {}, got {} ({} lost)".format(
                              expected, seq, gap))
        self._last_seq = seq

    def _send(self, mtype: MessageType, payload: bytes = b"") -> None:
        assert self._sock is not None
        if self.version[0] >= 2:
            seq = self._tx_seq & 0xFFFF
            self._tx_seq = (self._tx_seq + 1) & 0xFFFF
        else:
            seq = 0
        header = Header(message_type=int(mtype), sequence=seq,
                        payload_length=len(payload), version=self.version)
        framed = header.pack() + payload + b"\x00\x00"   # ... + Reserved
        crc = crc16_ccitt(framed) if self.version[0] >= 2 else 0
        msg = framed + struct.pack("<H", crc)
        self._sock.sendto(msg, (self.host, self.port))

    def _require_open(self) -> None:
        if self._sock is None:
            raise RuntimeError("client is not open(); call open() first")

    @staticmethod
    def _drain(q: "queue.Queue") -> None:
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass

    def _log(self, category: str, level: str, message: str) -> None:
        if self._on_log is not None:
            self._on_log(LogEvent(category=category, message=message, level=level))

    def _emit_state(self, state: str, detail: str) -> None:
        if self._on_state is not None:
            self._on_state(state, detail)
