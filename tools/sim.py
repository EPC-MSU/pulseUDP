"""pulseUDP server simulator (RFC §4); behaviour and usage in docs/gui-design.md (Simulator)."""

from __future__ import annotations

import argparse
import math
import socket
import struct
import sys
import time
from pathlib import Path

# Allow running straight from the repo without installing the package.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pulseudp.protocol import (HEADER_SIZE, TRAILER_SIZE, WORD_BYTES,  # noqa: E402
                               Descriptor, Header, MessageType, crc16_ccitt,
                               decode_channel_bitmap, encode_channel_bitmap)

DEFAULT_PORT = 2102
MTU_BUDGET = 1472


def _example_descriptor() -> str:
    path = (Path(__file__).resolve().parents[1]
            / "spec" / "examples" / "telemetry_example.json")
    return path.read_text(encoding="utf-8")


def _frame_descriptor(descriptor_json: str) -> bytes:
    """Wire form (RFC §5.1): NUL-terminated UTF-8 padded to a 32-bit word."""
    raw = descriptor_json.encode("utf-8") + b"\x00"
    pad = (-len(raw)) % WORD_BYTES
    return raw + b"\x00" * pad


def _encode_value(field, value: float) -> bytes:
    """Serialize one value into its 32-bit-word span (low bytes, little-endian)."""
    t = field.type
    if t in ("int8", "int16", "int32"):
        raw = struct.pack("<i", int(value))[:4]
        return raw  # int32 width; narrower types still occupy one 4-byte word
    if t in ("uint8", "uint16", "uint32", "bitfield"):
        return struct.pack("<I", int(value) & 0xFFFFFFFF)
    if t == "float":
        return struct.pack("<f", float(value))
    if t in ("int64", "uint64"):
        if t == "uint64":
            # The generator can yield negatives, which "<Q" rejects.
            return struct.pack("<Q", int(value) & 0xFFFFFFFFFFFFFFFF)
        return struct.pack("<q", int(value))
    if t == "double":
        return struct.pack("<d", float(value))
    raise ValueError("unknown type " + t)


def _sample(descriptor: Descriptor, n: int, enabled=None):
    """Yield one packet (bytes) for sample index ``n`` over the enabled channels."""
    parts = []
    phase = n * 0.01
    for i, f in enumerate(descriptor.fields):
        if enabled is not None and not enabled[i]:
            continue   # disabled channel: omitted from the packet
        if f.name.lower() in ("timestamp", "time"):
            value = n
        elif f.is_bitfield:
            nbits = max(1, len(f.bits or [1]))
            value = 1 << (n % nbits)
        elif f.type in ("float", "double"):
            value = math.sin(phase + i)
        else:
            value = int(300 * math.sin(phase + i))
        parts.append(_encode_value(f, value))
    return b"".join(parts)


def serve(host: str, port: int, descriptor_json: str, rate: float,
          version, drop: float, bad_crc: bool,
          packets_per_message=None) -> None:
    descriptor = Descriptor.from_json(descriptor_json)
    n_channels = len(descriptor.fields)
    enabled = [True] * n_channels

    packet_size = 0   # bytes of one packet over the *enabled* channels
    batch = 0         # telemetry packets per message

    def recompute_layout():
        """Packets carry only enabled channels, so SET_CHANNELS changes the layout."""
        nonlocal packet_size, batch
        packet_size = WORD_BYTES * sum(
            f.words for f, on in zip(descriptor.fields, enabled) if on)
        dgram_packets = ((MTU_BUDGET - HEADER_SIZE - TRAILER_SIZE) // packet_size
                         if packet_size else 0)
        batch = (packets_per_message if packets_per_message is not None
                 else max(1, dgram_packets))

    recompute_layout()

    # v1.0 cannot split a message: reject over-MTU settings up front.
    full_dgram_packets = (MTU_BUDGET - HEADER_SIZE - TRAILER_SIZE) // packet_size
    if version[0] < 2:
        if packets_per_message is not None and packets_per_message > full_dgram_packets:
            raise SystemExit(
                "--packets-per-message {} exceeds the {} that fit one datagram; "
                "v1.0 telemetry must be single-datagram (use --version 2.0)"
                .format(packets_per_message, full_dgram_packets))
        if packets_per_message is None and full_dgram_packets < 1:
            raise SystemExit("packet size {} B exceeds the MTU budget; v1.0 has no "
                             "multi-datagram support".format(packet_size))

    descriptor_bytes = _frame_descriptor(descriptor_json)
    desc_reply_bytes = HEADER_SIZE + len(descriptor_bytes) + TRAILER_SIZE
    if version[0] < 2 and desc_reply_bytes > MTU_BUDGET:
        raise SystemExit(
            "descriptor reply is {} B > {} B MTU budget; v1.0 has no "
            "multi-datagram support (RFC §5.6) — shorten the descriptor or use "
            "--version 2.0".format(desc_reply_bytes, MTU_BUDGET))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.settimeout(0.05)
    msg_bytes = HEADER_SIZE + batch * packet_size + TRAILER_SIZE
    n_dgrams = (msg_bytes + MTU_BUDGET - 1) // MTU_BUDGET
    print("pulseUDP sim on {}:{}  {} channels  packet={} B  {} packets/message"
          "{}  rate={} Hz  v{}.{}".format(
              host, port, n_channels, packet_size, batch,
              " ({} datagrams/message)".format(n_dgrams) if n_dgrams > 1 else "",
              rate, version[0], version[1]))

    client = None          # current (addr) we stream to
    streaming = False
    seq = 0
    n = 0                  # global sample counter
    next_emit = time.time()
    period = 1.0 / rate if rate > 0 else 0.0

    def send(mtype, payload=b"", drop=False):
        nonlocal seq
        assert client is not None
        seq_field = (seq & 0xFFFF) if version[0] >= 2 else 0
        hdr = Header(message_type=int(mtype), sequence=seq_field,
                     payload_length=len(payload), version=(version[0], version[1]))
        reserved = b"\x00\x00"
        body = hdr.pack() + payload + reserved
        if version[0] >= 2:
            crc_val = crc16_ccitt(body)
            if bad_crc:
                crc_val ^= 0xFFFF
            message = body + struct.pack("<H", crc_val)
        else:
            message = body + b"\x00\x00"
        # A dropped datagram still consumes its sequence number.
        if not drop:
            try:
                if version[0] >= 2 and len(message) > MTU_BUDGET:
                    # RFC §5.6: contiguous pieces; only the first has a header.
                    pieces = [message[i:i + MTU_BUDGET]
                              for i in range(0, len(message), MTU_BUDGET)]
                    for piece in pieces:
                        sock.sendto(piece, client)
                    # Telemetry splits are reported once in the startup line.
                    if int(mtype) != MessageType.TELEMETRY:
                        print("  {} sent as {} datagrams ({} B total)".format(
                            MessageType(int(mtype)).name, len(pieces), len(message)))
                else:
                    sock.sendto(message, client)
            except OSError as exc:
                print("  WARNING: failed to send {} ({} B): {}".format(
                    MessageType(int(mtype)).name, len(message), exc))
        if version[0] >= 2:
            seq = (seq + 1) & 0xFFFF

    while True:
        try:
            data, addr = sock.recvfrom(65535)
            try:
                header = Header.unpack(data)
            except ValueError:
                continue
            if addr != client:           # single-client supersede
                client = addr
                streaming = False
                seq = 0
                enabled[:] = [True] * n_channels
                recompute_layout()
                print("client -> {}".format(addr))
            mtype = header.message_type
            payload = data[HEADER_SIZE:HEADER_SIZE + header.payload_length]
            if mtype == MessageType.DESCRIPTION:
                send(MessageType.DESCRIPTION, descriptor_bytes)
            elif mtype == MessageType.TELEMETRY:
                streaming = True
                next_emit = time.time()
            elif mtype == MessageType.STOP:
                streaming = False
                send(MessageType.STOP)
            elif mtype == MessageType.GET_CHANNELS and version[0] >= 2:
                send(MessageType.GET_CHANNELS, encode_channel_bitmap(enabled))
            elif mtype == MessageType.SET_CHANNELS and version[0] >= 2:
                enabled[:] = decode_channel_bitmap(payload, n_channels)
                recompute_layout()
                print("channels set -> {}/{} enabled  packet={} B".format(
                    sum(enabled), n_channels, packet_size))
                send(MessageType.SET_CHANNELS, encode_channel_bitmap(enabled))
        except socket.timeout:
            pass

        if streaming and client is not None:
            now = time.time()
            if now >= next_emit:
                due = batch
                payload = b"".join(
                    _sample(descriptor, n + k, enabled) for k in range(due))
                n += due
                drop_this = (drop > 0 and drop < 1
                             and (n // due) % int(1 / drop) == 0)
                send(MessageType.TELEMETRY, payload, drop=drop_this)
                next_emit += period * due if period else 0.0
                if period == 0.0:
                    next_emit = now + 0.001


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="pulseUDP telemetry simulator")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--rate", type=float, default=1000.0, help="samples/second")
    p.add_argument("--version", default="1.0", choices=("1.0", "2.0"),
                   help="protocol version: 1.0 (seq=0, CRC=0) or 2.0 (active seq, "
                        "real CRC-16/CCITT-FALSE)")
    p.add_argument("--descriptor", help="path to a descriptor JSON (default: example)")
    p.add_argument("--packets-per-message", type=int, default=None,
                   help="telemetry packets per message (default: one datagram's "
                        "worth). In v2.0 a value larger than one datagram makes "
                        "the message span several datagrams (RFC §5.6); v1.0 "
                        "rejects an over-MTU value.")
    p.add_argument("--drop", type=float, default=0.0,
                   help="fraction of datagrams to drop (v2.0 loss test), 0..1")
    p.add_argument("--bad-crc", action="store_true",
                   help="send a wrong CRC-16 (v2.0 only) to test the log path")
    args = p.parse_args(argv)

    major, minor = (int(x) for x in args.version.split("."))
    descriptor_json = (Path(args.descriptor).read_text(encoding="utf-8")
                       if args.descriptor else _example_descriptor())
    try:
        serve(args.host, args.port, descriptor_json, args.rate,
              (major, minor), args.drop, args.bad_crc,
              packets_per_message=args.packets_per_message)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
