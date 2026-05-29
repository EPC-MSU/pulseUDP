"""pulseUDP telemetry simulator.

A stand-in for the (out-of-scope) microcontroller firmware so the GUI and client
can be developed and tested end to end. It implements the controller side of
RFC §4:

* answers ``DESCRIPTION`` with a descriptor (the example descriptor by default),
* on ``TELEMETRY`` streams RFC-conformant packets at a configurable rate, packing
  several packets per datagram,
* acks ``STOP``,
* honours the single-client rule: a command from a new source supersedes the
  previous client and resets the sequence counter.

The generated signals are synthetic (sines / counters / walking flag bits) — just
enough to exercise the plots.

Usage::

    py -3.8 tools/sim.py                       # serve example descriptor on :2102
    py -3.8 tools/sim.py --rate 2000 --version 1.0
    py -3.8 tools/sim.py --descriptor path/to/descriptor.json
"""

from __future__ import annotations

import argparse
import json
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
                               Descriptor, Header, MessageType)

DEFAULT_PORT = 2102
MTU_BUDGET = 1472


def _example_descriptor() -> str:
    path = (Path(__file__).resolve().parents[1]
            / "spec" / "examples" / "telemetry_example.json")
    return path.read_text(encoding="utf-8")


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
        code = "<q" if t == "int64" else "<Q"
        return struct.pack(code, int(value))
    if t == "double":
        return struct.pack("<d", float(value))
    raise ValueError("unknown type " + t)


def _sample(descriptor: Descriptor, n: int, t0: float):
    """Yield one packet (bytes) for sample index ``n``."""
    parts = []
    phase = n * 0.01
    for i, f in enumerate(descriptor.fields):
        if i == descriptor.timestamp_index:
            # Timestamp in ms tick if mult looks like 0.001, else raw counter.
            value = n
        elif f.is_bitfield:
            # Walk a single set bit across the named (non-Reserved) bits.
            nbits = max(1, len(f.bits or [1]))
            value = 1 << (n % nbits)
        elif f.type in ("float", "double"):
            value = math.sin(phase + i)
        else:
            # Integer numeric: a sine scaled into a few hundred counts.
            value = int(300 * math.sin(phase + i))
        parts.append(_encode_value(f, value))
    return b"".join(parts)


def serve(host: str, port: int, descriptor_json: str, rate: float,
          version, drop: float, bad_crc: bool) -> None:
    descriptor = Descriptor.from_json(descriptor_json)
    packet_size = descriptor.packet_size
    max_packets = (MTU_BUDGET - HEADER_SIZE - TRAILER_SIZE) // packet_size
    if max_packets < 1:
        raise SystemExit("packet size {} B exceeds the MTU budget".format(packet_size))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.settimeout(0.05)
    print("pulseUDP sim on {}:{}  packet={} B  up to {} packets/datagram  "
          "rate={} Hz  v{}.{}".format(host, port, packet_size, max_packets,
                                      rate, version[0], version[1]))

    client = None          # current (addr) we stream to
    streaming = False
    seq = 0
    n = 0                  # global sample counter
    t0 = time.time()
    next_emit = time.time()
    period = 1.0 / rate if rate > 0 else 0.0
    # Emit in datagram-sized batches; cap batch so we don't busy-spin.
    batch = max_packets

    def send(mtype, payload=b""):
        nonlocal seq
        hdr = Header(message_type=int(mtype), sequence=(seq & 0xFFFF if version[0] >= 1 else 0),
                     payload_length=len(payload), version=(version[0], version[1]))
        crc = b"\x00\x00"
        if version[0] >= 1 and bad_crc:
            crc = b"\xff\xff"   # deliberately wrong, to exercise the client log path
        trailer = b"\x00\x00" + crc  # Reserved + CRC-16
        sock.sendto(hdr.pack() + payload + trailer, client)
        if version[0] >= 1:
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
                print("client -> {}".format(addr))
            mtype = header.message_type
            if mtype == MessageType.DESCRIPTION:
                send(MessageType.DESCRIPTION, descriptor_json.encode("utf-8"))
            elif mtype == MessageType.TELEMETRY:
                streaming = True
                next_emit = time.time()
            elif mtype == MessageType.STOP:
                streaming = False
                send(MessageType.STOP)
        except socket.timeout:
            pass

        if streaming and client is not None:
            now = time.time()
            if now >= next_emit:
                # How many samples are due since the last emit.
                due = batch
                payload = b"".join(_sample(descriptor, n + k, t0) for k in range(due))
                n += due
                if not (drop > 0 and (n // due) % int(1 / drop) == 0 and drop < 1):
                    send(MessageType.TELEMETRY, payload)
                next_emit += period * due if period else 0.0
                if period == 0.0:
                    next_emit = now + 0.001


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="pulseUDP telemetry simulator")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--rate", type=float, default=1000.0, help="samples/second")
    p.add_argument("--version", default="0.1", help="protocol version major.minor")
    p.add_argument("--descriptor", help="path to a descriptor JSON (default: example)")
    p.add_argument("--drop", type=float, default=0.0,
                   help="fraction of datagrams to drop (v1.0 loss test), 0..1")
    p.add_argument("--bad-crc", action="store_true",
                   help="send a wrong CRC-16 (v1.0 only) to test the log path")
    args = p.parse_args(argv)

    major, minor = (int(x) for x in args.version.split("."))
    descriptor_json = (Path(args.descriptor).read_text(encoding="utf-8")
                       if args.descriptor else _example_descriptor())
    try:
        serve(args.host, args.port, descriptor_json, args.rate,
              (major, minor), args.drop, args.bad_crc)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
