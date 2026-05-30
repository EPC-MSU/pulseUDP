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
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import numpy as np

from .protocol import (HEADER_SIZE, TRAILER_SIZE, Descriptor, Header,
                       MessageType)

DEFAULT_PORT = 2102
_RECV_BUFSIZE = 65535
_SOCK_TIMEOUT = 0.2  # s, receiver loop wakeup so it can observe close()


@dataclass
class LogEvent:
    """A protocol-level event for the log panel. The consumer adds the wall clock."""

    category: str    # bad_magic | bad_version | short | decode | seq_gap | crc | info | error
    message: str
    level: str = "warning"   # info | warning | error


# Callback type aliases (documentation only).
TelemetryCb = Callable[[np.ndarray], None]   # receives a decoded packet array
LogCb = Callable[[LogEvent], None]
StateCb = Callable[[str, str], None]          # (state, detail)


class UdpClient:
    """A single-controller pulseUDP client.

    Parameters
    ----------
    host, port:
        Controller address. ``port`` defaults to the protocol port 2102.
    schema:
        Optional descriptor JSON-Schema; when given, descriptors are validated
        against it before use.
    on_telemetry, on_log, on_state:
        Callbacks. ``on_telemetry`` is invoked from the receiver thread with a
        decoded packet array; keep it cheap and thread-safe (the GUI appends to
        a thread-safe ``RingBuffer``).
    """

    def __init__(self, host: str, port: int = DEFAULT_PORT,
                 schema: Optional[Dict[str, Any]] = None,
                 on_telemetry: Optional[TelemetryCb] = None,
                 on_log: Optional[LogCb] = None,
                 on_state: Optional[StateCb] = None) -> None:
        self.host = host
        self.port = port
        self.schema = schema
        self._on_telemetry = on_telemetry
        self._on_log = on_log
        self._on_state = on_state

        self.descriptor: Optional[Descriptor] = None

        self._sock: Optional[socket.socket] = None
        self._rx_thread: Optional[threading.Thread] = None
        self._closing = threading.Event()

        # Response routing from the receiver thread to waiting callers.
        self._desc_q: "queue.Queue[bytes]" = queue.Queue()
        self._stop_ack = threading.Event()
        self._stream_started = threading.Event()

        self._streaming = False
        self._last_seq: Optional[int] = None

    # -- lifecycle ------------------------------------------------------------

    def open(self) -> None:
        """Open the socket and start the receiver thread."""
        if self._sock is not None:
            return
        self._closing.clear()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
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
        """Send ``DESCRIPTION`` and return the parsed descriptor (retransmits)."""
        self._require_open()
        self._emit_state("connecting", "{}:{}".format(self.host, self.port))
        # Drain any stale reply.
        self._drain(self._desc_q)
        for attempt in range(retries):
            self._send(MessageType.DESCRIPTION)
            try:
                payload = self._desc_q.get(timeout=timeout)
            except queue.Empty:
                self._log("info", "info",
                          "DESCRIPTION timeout, retry {}/{}".format(attempt + 1, retries))
                continue
            descriptor = Descriptor.from_json(
                payload, schema=self.schema, validate=self.schema is not None)
            self.descriptor = descriptor
            self._emit_state("connected",
                             "descriptor v{} · {} fields".format(
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

    # -- receiver thread ------------------------------------------------------

    def _rx_loop(self) -> None:
        sock = self._sock
        assert sock is not None
        while not self._closing.is_set():
            try:
                data, _addr = sock.recvfrom(_RECV_BUFSIZE)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle_datagram(data)

    def _handle_datagram(self, data: bytes) -> None:
        try:
            header = Header.unpack(data)
        except ValueError as exc:
            msg = str(exc)
            if "magic" in msg:
                self._log("bad_magic", "warning", "ignored datagram: " + msg)
            else:
                self._log("short", "warning", "short datagram: " + msg)
            return

        if header.version[0] not in (0, 1):
            self._log("bad_version", "warning",
                      "unsupported version {}.{}".format(*header.version))
            return

        # v1.0+ activates sequence-loss detection; v0.1 sends 0 and is ignored.
        if header.version[0] >= 1 and header.message_type == MessageType.TELEMETRY:
            self._check_sequence(header.sequence)
        # CRC-16 is a v1.0 concern (CRC-16/CCITT-FALSE, RFC §3.2); not yet
        # implemented. Hook: validate over data[:-2] here.

        mtype = header.message_type
        avail = len(data) - HEADER_SIZE - TRAILER_SIZE
        n_payload = max(0, min(header.payload_length, avail))
        payload = data[HEADER_SIZE:HEADER_SIZE + n_payload]

        if mtype == MessageType.DESCRIPTION:
            self._desc_q.put(payload)
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
        if self.descriptor is None or self._on_telemetry is None:
            return
        if len(payload) % self.descriptor.packet_size != 0:
            # Trailing pad is allowed (RFC §5.3); only flag a non-integer count
            # that also leaves no whole packets.
            if self.descriptor.packets_in(len(payload)) == 0:
                self._log("decode", "warning",
                          "telemetry payload {} B < packet size {} B".format(
                              len(payload), self.descriptor.packet_size))
                return
        packets = self.descriptor.decode(payload)
        if packets.size:
            self._on_telemetry(packets)

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
        header = Header(message_type=int(mtype), sequence=0,
                        payload_length=len(payload))
        msg = header.pack() + payload + b"\x00\x00\x00\x00"  # Reserved + CRC, zero in v0.1
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
