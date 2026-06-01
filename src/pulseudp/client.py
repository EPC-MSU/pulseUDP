"""UDP client for the pulseUDP protocol.

:class:`UdpClient` owns the socket and a single receiver thread that reads every
inbound datagram for the session. It exposes the three transactions from RFC §4
— ``DESCRIPTION``, ``TELEMETRY`` (start), ``STOP`` — each with a retransmit
timeout (every request must produce a response). Decoded telemetry and protocol
log events are delivered through plain callables, so this module has no Qt
dependency and can be driven headless (e.g. against ``tools/sim.py``). The GUI
wraps the callbacks in Qt signals to marshal them onto the main thread.
"""

from __future__ import annotations

import queue
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from .protocol import (HEADER_SIZE, TRAILER_SIZE, Descriptor, Header,
                       MessageType, crc16_ccitt, decode_channel_bitmap,
                       encode_channel_bitmap)

DEFAULT_PORT = 2102
#: Protocol versions this client understands, highest first. The first entry is
#: the version used for the opening DESCRIPTION probe; the session then adopts
#: whatever version the server reveals in its reply (RFC §6.1).
SUPPORTED_VERSIONS = ((2, 0), (1, 0))
PROBE_VERSION = SUPPORTED_VERSIONS[0]
_RECV_BUFSIZE = 65535
_SOCK_TIMEOUT = 0.2    # s, receiver loop wakeup so it can observe close()
_REASM_TIMEOUT = 0.3   # s, multi-datagram reassembly deadline (RFC §5.6)


@dataclass
class LogEvent:
    """A protocol-level event for the log panel. The consumer adds the wall clock."""

    category: str    # bad_magic | bad_version | short | decode | seq_gap | crc | reasm | info | error
    message: str
    level: str = "warning"   # info | warning | error


# Callback type aliases (documentation only).
TelemetryCb = Callable[[Dict[str, np.ndarray]], None]  # per-field physical arrays
LogCb = Callable[[LogEvent], None]
StateCb = Callable[[str, str], None]          # (state, detail)


@dataclass
class _Reassembly:
    """In-progress multi-datagram message (v2.0, RFC §5.6).

    Only the first datagram carries the header; the rest are raw continuation
    bytes appended to ``buf`` until it reaches ``expected`` total bytes
    (``HEADER_SIZE + payload_length + TRAILER_SIZE``).
    """

    header: Header
    expected: int
    deadline: float       # time.monotonic() past which the partial buffer is dropped
    buf: bytearray


class UdpClient:
    """A single-server pulseUDP client.

    Parameters
    ----------
    host, port:
        Server address. ``port`` defaults to the protocol port 2102.
    version:
        The version used for the opening ``DESCRIPTION`` **probe**, ``(major,
        minor)`` — the highest supported (v2.0) by default. The session version
        is then *discovered*: :meth:`request_descriptor` reads the server's
        version from its reply and fixates ``self.version`` to it (RFC §6.1), so
        all later requests are framed at the negotiated version. v2.0 stamps an
        active sequence number + real CRC-16 (RFC §3.2); v1.0 sends both as zero.
        Inbound validation always keys off each datagram's own version field.
    schema:
        Optional descriptor JSON-Schema; when given, descriptors are validated
        against it before use.
    on_telemetry, on_log, on_state:
        Callbacks. ``on_telemetry`` is invoked from the receiver thread with a
        dict of per-field physical-unit arrays (``{field_name: ndarray}``) for
        the enabled channels; keep it cheap and thread-safe (the GUI flattens it
        and appends to a thread-safe ``RingBuffer``).
    """

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
        # v2.0 channel selection (RFC §4): which descriptor channels are enabled,
        # and the descriptor used to decode telemetry — the full descriptor, or a
        # subset matching the enabled channels' on-wire packet layout.
        self.enabled_channels: Optional[List[bool]] = None
        self._active_descriptor: Optional[Descriptor] = None

        self._sock: Optional[socket.socket] = None
        self._rx_thread: Optional[threading.Thread] = None
        self._closing = threading.Event()

        # Response routing from the receiver thread to waiting callers.
        # DESCRIPTION carries (reply_version, payload) so the caller can fixate
        # the negotiated protocol version (RFC §6.1).
        self._desc_q: "queue.Queue[Tuple[Tuple[int, int], bytes]]" = queue.Queue()
        # GET_CHANNELS / SET_CHANNELS responses both carry a channel bitmap (RFC §4);
        # the two are serialized request/response transactions, so one queue serves.
        self._chan_q: "queue.Queue[bytes]" = queue.Queue()
        self._stop_ack = threading.Event()
        self._stream_started = threading.Event()

        self._streaming = False
        self._last_seq: Optional[int] = None   # last RX sequence (loss detection)
        self._tx_seq = 0                        # next TX sequence (v2.0 outbound)
        self._reasm: Optional[_Reassembly] = None  # multi-datagram in progress

    # -- lifecycle ------------------------------------------------------------

    def open(self) -> None:
        """Open the socket and start the receiver thread."""
        if self._sock is not None:
            return
        self._closing.clear()
        self._tx_seq = 0
        self._last_seq = None
        self._reasm = None
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('', DEFAULT_PORT))
        sock.settimeout(_SOCK_TIMEOUT)
        self._sock = sock
        self._rx_thread = threading.Thread(
            target=self._rx_loop, name="pulseudp-rx", daemon=True)
        self._rx_thread.start()

    def close(self) -> None:
        """Stop streaming if needed, stop the receiver thread, close the socket."""
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

    # -- transactions ---------------------------------------------------------

    def request_descriptor(self, timeout: float = 1.0, retries: int = 3) -> Descriptor:
        """Probe with ``DESCRIPTION``, negotiate the version, return the descriptor.

        Sends the opening ``DESCRIPTION`` framed at the probe version (highest
        supported), then fixates :attr:`version` to whatever the server
        reveals in its reply and uses that for the rest of the session (RFC
        §6.1). A reply in an unsupported version raises ``RuntimeError``; no
        reply within the retransmit budget raises ``TimeoutError`` (the endpoint
        is unreachable — version is never the cause, since any server
        answers any request).
        """
        self._require_open()
        self._emit_state("connecting", "{}:{}".format(self.host, self.port))
        # Probe at the highest supported version; the server answers
        # regardless of version and the reply reveals its own (RFC §6.1).
        self.version = PROBE_VERSION
        # Drain any stale reply.
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
            # Start with every channel enabled (v1.0 immutable; v2.0 until the
            # caller narrows it via set_channels). Decode against the full layout.
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

    # -- channel selection (v2.0, RFC §4) -------------------------------------

    def get_channels(self, timeout: float = 1.0, retries: int = 3) -> List[bool]:
        """Read the enabled-channel set (RFC §4), one bool per descriptor field.

        v1.0 has no selection — every channel is enabled and immutable, so this
        returns all-``True`` without touching the wire.
        """
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
        """Request an enabled-channel set; return the set the server accepted.

        The server may refuse or alter the request (RFC §4), so the returned list
        — not the requested one — is authoritative and becomes the decode layout.
        v1.0 has no selection: returns all-``True`` and sends nothing.
        """
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
        """Record the enabled set and the descriptor used to decode telemetry.

        With every channel enabled the full descriptor is used; otherwise a
        :meth:`Descriptor.subset` matching the enabled channels' packet layout.
        """
        self.enabled_channels = list(enabled)
        if self.descriptor is None or all(enabled):
            self._active_descriptor = self.descriptor
        else:
            idx = [i for i, on in enumerate(enabled) if on]
            self._active_descriptor = (self.descriptor.subset(idx) if idx
                                       else self.descriptor)

    # -- receiver thread ------------------------------------------------------

    def _rx_loop(self) -> None:
        sock = self._sock
        assert sock is not None
        while not self._closing.is_set():
            try:
                data, _addr = sock.recvfrom(_RECV_BUFSIZE)
            except socket.timeout:
                # Idle wakeup: also a chance to time out a stalled reassembly even
                # when no further datagrams arrive (RFC §5.6).
                self._expire_reassembly()
                continue
            except OSError:
                break
            self._handle_datagram(data)

    def _handle_datagram(self, data: bytes) -> None:
        # A multi-datagram message in progress (v2.0, RFC §5.6): every datagram
        # after the first is raw continuation bytes — no header, no magic — so it
        # must not be parsed as a header. Append it and try to complete.
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

        # Length-delimited reassembly (RFC §5.6, v2.0 only): a first datagram that
        # carries fewer bytes than its header declares (12 + payload_length + 4)
        # opens a multi-datagram message; the remaining pieces arrive headerless.
        expected = HEADER_SIZE + header.payload_length + TRAILER_SIZE
        if header.version[0] >= 2 and len(data) < expected:
            self._reasm = _Reassembly(
                header=header, expected=expected,
                deadline=time.monotonic() + _REASM_TIMEOUT,
                buf=bytearray(data))
            return

        self._process_message(header, data)

    def _feed_reassembly(self, data: bytes) -> None:
        """Append a continuation datagram; process the message once it's complete."""
        r = self._reasm
        assert r is not None
        r.buf += data
        if len(r.buf) < r.expected:
            return                          # still waiting for more pieces
        if len(r.buf) > r.expected:
            # Over-run: a lost/reordered/duplicated piece corrupted the stream.
            # All-or-nothing (RFC §5.6) — drop and let the caller re-request.
            self._log("reasm", "warning",
                      "reassembled {} B exceeds expected {} B; message dropped".format(
                          len(r.buf), r.expected))
            self._reasm = None
            return
        header, message = r.header, bytes(r.buf)
        self._reasm = None
        self._process_message(header, message)

    def _expire_reassembly(self) -> None:
        """Discard a partial multi-datagram buffer past its deadline (RFC §5.6)."""
        r = self._reasm
        if r is not None and time.monotonic() > r.deadline:
            self._log("reasm", "warning",
                      "reassembly timeout: discarded {}/{} B; message will be "
                      "re-requested".format(len(r.buf), r.expected))
            self._reasm = None

    def _process_message(self, header: Header, data: bytes) -> None:
        """Handle one complete message: ``data`` is header ‖ payload ‖ trailer.

        Called for single-datagram messages and for the reassembled bytes of a
        multi-datagram one alike, so the CRC covers the whole message either way.
        """
        # CRC and sequence are v2.0 concerns: in v1.0 both fields are sent as 0
        # and ignored, so neither is checked. Each message is judged by its own
        # version field, so the client handles a mixed/either-version server.
        if header.version[0] >= 2:
            # Validate the trailer CRC-16/CCITT-FALSE (RFC §3.2) before trusting
            # anything else; a bad CRC means the header (and its sequence number)
            # is unreliable, so drop without updating seq state. For a reassembled
            # message this covers the entire reassembled message (RFC §5.6).
            if not self._crc_ok(data):
                self._log("crc", "warning",
                          "CRC mismatch (type 0x{:04x}); message dropped".format(
                              header.message_type))
                return
            # Sequence-loss detection runs on the telemetry stream only.
            if header.message_type == MessageType.TELEMETRY:
                self._check_sequence(header.sequence)

        mtype = header.message_type
        avail = len(data) - HEADER_SIZE - TRAILER_SIZE
        n_payload = max(0, min(header.payload_length, avail))
        payload = data[HEADER_SIZE:HEADER_SIZE + n_payload]

        if mtype == MessageType.DESCRIPTION:
            # Forward the reply's version so the caller can fixate it (RFC §6.1).
            self._desc_q.put((header.version, payload))
        elif mtype in (MessageType.GET_CHANNELS, MessageType.SET_CHANNELS):
            # Both responses carry the channel bitmap (RFC §4); hand it to the
            # waiting get_channels/set_channels caller.
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
        # Decode against the *active* descriptor: the full layout, or a subset
        # matching the v2.0 enabled channels (RFC §4) — the packet on the wire
        # carries only the enabled fields.
        dd = self._active_descriptor or self.descriptor
        if dd is None or self._on_telemetry is None:
            return
        if dd.packet_size == 0:
            return                      # no channels enabled -> nothing to decode
        if len(payload) % dd.packet_size != 0:
            # Trailing pad is allowed (RFC §5.3); only flag a non-integer count
            # that also leaves no whole packets.
            if dd.packets_in(len(payload)) == 0:
                self._log("decode", "warning",
                          "telemetry payload {} B < packet size {} B".format(
                              len(payload), dd.packet_size))
                return
        packets = dd.decode(payload)
        if packets.size:
            # Deliver per-field physical arrays (only the enabled channels).
            self._on_telemetry(dd.channels(packets))

    def _crc_ok(self, data: bytes) -> bool:
        """True if the trailer CRC-16 matches (RFC §3.2 covers Magic..Reserved)."""
        if len(data) < HEADER_SIZE + TRAILER_SIZE:
            return False   # too short to carry a trailer
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

    # -- helpers --------------------------------------------------------------

    def _send(self, mtype: MessageType, payload: bytes = b"") -> None:
        assert self._sock is not None
        # v2.0: stamp an active per-message sequence and a real CRC; v1.0 zeroes
        # both (sent and ignored, RFC §3.1).
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
