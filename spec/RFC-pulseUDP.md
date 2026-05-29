# pulseUDP — Telemetry-over-Ethernet Protocol

**Status:** Draft / working document
**Versions described:** `0.1` (current, partial) and `1.0` (full)
**Date:** 2026-05-29

---

## 1. Overview

Ethernet bandwidth in modern microcontrollers (100BASE-TX) is high enough to provide several synchronous data points per timestamp at the ~20 kHz rates typically used for motor control. For example, 8 full 32-bit words at a 20 kHz rate use less than 6% of the 100 Mbit/s capacity, even accounting for UDP overhead.

The protocol has two parts:

1. **A small framed message format** carried in UDP datagrams (magic, version, message type,
   sequence number, payload length, payload, and a CRC trailer). Sequence numbering and CRC
   are present in the header/trailer of every version but only become active in v1.0.
2. **A JSON descriptor** that the controller sends on request, describing the layout of each
   telemetry frame so the client can parse the binary stream generically.

A session is request/response: the client asks the controller for the descriptor and to start
streaming; the controller then emits a continuous stream of telemetry messages until told to
stop.

## 2. Transport

- **Protocol:** UDP.
- **Port:** `2102` (controller listens here; client sends commands here).
- **Byte order:** little-endian for all multi-byte fields.
- **MTU budget:** one datagram payload is kept within a single Ethernet frame — **1472 bytes**
  of UDP payload (1500 MTU − 20 IP − 8 UDP). A telemetry message MUST fit in one datagram for
  efficiency. From protocol **v1.0** onward, other message types MAY span multiple datagrams
  (see §5.7); in **v0.1** every message MUST fit in a single datagram.

**Single client.** The controller serves exactly one client at a time. It learns the client's
IP address and port from the source of any valid command it receives, and streams telemetry
back to that address and port. A valid command from a **new** source address/port immediately
**supersedes** the previous client: the controller aborts any in-progress stream or transfer to
the old client, resets its session state (including the sequence counter), and re-initializes
for the new client. There is no multi-client support; the most recent client wins.

## 3. Message format

Every message is a fixed header, an optional payload, and a CRC trailer. All fields are contiguous with no padding between them.

```
 offset  size  field
 ------  ----  -------------------------------------------------
   0      2    Magic            = 0x50 0x55  ("PU")
   2      1    Version major
   3      1    Version minor
   4      4    Type & sequence  (uint32 = (message_type << 16) | sequence_number)
   8      4    Payload length   (uint32, = N bytes of payload)
  12      N    Payload          (N bytes)
 12+N     2    Reserved         (uint16)
 14+N     2    CRC-16           (uint16)
```

**Header size:** 12 bytes. **Trailer:** 4 bytes, present in every version (CRC unused in v0.1).

### 3.1 Field semantics

| Field | Type | Meaning |
|---|---|---|
| **Magic** | 2 bytes | The byte sequence `0x50 0x55` (ASCII "PU"), in this order on the wire — defined as a fixed byte sequence, not an endian-dependent integer. Any datagram not beginning with these bytes MUST be ignored. Lets a receiver find message boundaries / reject noise. |
| **Version major** | `uint8` | Incompatible protocol revision. This document covers major `0` and `1`. |
| **Version minor** | `uint8` | Compatible additions within a major. Combined value is written `major.minor` (e.g. `0.1`, `1.0`). Two different versions are **not** required to be wire-compatible. |
| **Type & sequence** | `uint32` | One 32-bit word carrying two sub-fields: **message type** in the high 16 bits, **sequence number** in the low 16 bits — `word = (message_type << 16) \| sequence_number`. |
| ↳ **Message type** | `uint16` | What the message is / what it commands. See §4. |
| ↳ **Sequence number** | `uint16` | Per-sender monotonic counter (wraps at 65536) for loss detection. **v0.1:** sender sets `0`, receiver ignores. **v1.0:** active. |
| **Payload length** | `uint32` | Number of payload bytes that follow the header. MUST be 32-bit word aligned. May be `0`. |
| **Payload** | bytes | Type-dependent; see §4 and §5. |
| **Reserved** | `uint16` | Present in every version. Sender sets `0`, reserved for future use. |
| **CRC-16** | `uint16` | Present in every version. CRC-16 over the bytes from Magic through the end of Reserved (i.e. the whole message except the CRC field itself). **v0.1:** sender sets `0`, receiver ignores. **v1.0:** filled and validated. |

## 4. Message types

A message type is the high 16 bits of the word at offset 4 (see §3.1) and occupies **2 bytes**.
A request and the response it triggers carry the **same** message-type constant; direction and
payload distinguish them.

| Constant | Value (uint16) | Transaction | client → controller | controller → client |
|---|---|---|---|---|
| `DESCRIPTION` | `0x0001` | Get descriptor | request, empty payload | JSON descriptor (see §5), UTF-8, no NUL terminator |
| `TELEMETRY` | `0x0002` | Telemetry stream | request to start, empty payload | streamed telemetry frames, one or more per datagram (see §5.3), until stopped |
| `STOP` | `0x0003` | Stop stream | request, empty payload | optional acknowledgement, empty payload |

### 4.1 Session flow

```
client                                      Controller
 |  DESCRIPTION (request)  ─────────────────►|   (records client source IP:port)
 |◄────────────  DESCRIPTION (JSON reply)    |
 |  TELEMETRY (start request)  ─────────────►|
 |◄──────────────  TELEMETRY (frames)        |  ┐
 |◄──────────────  TELEMETRY (frames)        |  │  continuous stream
 |◄──────────────  TELEMETRY (frames)        |  ┘
 |  STOP  ──────────────────────────────────►|
```

The client repeats the `DESCRIPTION` request until it receives a valid `DESCRIPTION` reply; it then knows the controller's address and the frame layout. Because the protocol is explicitly request/response and request and reply share a message type, the receiver distinguishes them by direction and payload — no heuristics.

## 5. Payload: telemetry frames and the JSON descriptor

### 5.1 The descriptor

The `DESCRIPTION` payload is a JSON object describing one telemetry frame. It is sent as a
single UTF-8 string with no NUL terminator. For v0.1 the descriptor is **static** (fixed at
build time); runtime/adaptive descriptors are out of scope until a later version.

A descriptor MUST validate against the pulseUDP JSON-Schema (draft-07), published alongside
this document as **`Schema.json`**. That schema is the normative definition of the descriptor
format; this section only summarises it.

A worked example descriptor and a validator are provided under `examples/`:

- `examples/telemetry_example.json` — an example descriptor (illustrative only, not normative).
- `examples/validate.py` — validates a descriptor against the schema:
  `python examples/validate.py examples/telemetry_example.json Schema.json` (requires the `jsonschema`
  package).

### 5.2 Types and on-wire width

**Each value occupies an integer number of 32-bit words on the wire.** The value is stored in
the low bytes of its word span (little-endian); any remaining high bytes are
sign/zero-extension per the declared type and are ignored by a reader that already knows the
type. This keeps every field naturally aligned and lets the controller serialize straight
from its in-memory buffer with no packing.

| `type` token | Logical type | Words | Wire bytes |
|---|---|---|---|
| `int8` / `uint8` | 8-bit integer | 1 | 4 |
| `int16` / `uint16` | 16-bit integer | 1 | 4 |
| `int32` / `uint32` | 32-bit integer | 1 | 4 |
| `bitfield` | 32 flags packed into a `uint32` | 1 | 4 |
| `int64` / `uint64` | 64-bit integer | 2 | 8 |
| `float` | IEEE-754 single | 1 | 4 |
| `double` | IEEE-754 double | 2 | 8 |

**Bitfields.** A `bitfield` is always a `uint32`. Bits are assigned in the order listed in
`bits`, starting from the least-significant bit. With fewer than 32 names, the high bits are
zero. (The current schema assumes flags are contiguous from bit 0 with no gaps — the count of
names equals the count of flags.)

### 5.3 Frame stream

A `TELEMETRY` payload is an integer number of frames laid end to end with no gaps. Each frame
is the field values in descriptor order, each value sized per §5.2. The number of frames in a
datagram = `Payload length / frame_size`.

### 5.4 Example frame

| # | Field | Type | Words | Bytes |
|---|---|---|---|---|
| 1 | `Timestamp` | uint32 (ms tick) | 1 | 4 |
| 2 | `VoltageA` | int16, ×0.01 V | 1 | 4 |
| 3 | `VoltageB` | int16, ×0.01 V | 1 | 4 |
| 4 | `VoltageC` | int16, ×0.01 V | 1 | 4 |
| 5 | `CurrentA` | int16, ×0.001 A | 1 | 4 |
| 6 | `CurrentB` | int16, ×0.001 A | 1 | 4 |
| 7 | `Flags` | bitfield (32 flags) | 1 | 4 |
| 8 | `GeneralPurpose1` | int32, ×0.001 UsrUnit | 1 | 4 |
| | **Frame total** | | 8 | 32 |

**Datagram packing example:** header 12 B + 45 frames × 32 B + 4 B trailer = 1456 B ≤ 1472 B.
So up to **45 frames per datagram** (~2.3 ms of data at a 19531 Hz sample rate).

### 5.5 Reconstructing time

`Timestamp` is a millisecond counter (the controller has no microsecond counter). At
~19531 Hz there are ~19 frames per millisecond, so the client reconstructs sub-millisecond
sample times by interpolating frame indices between timestamp ticks.

### 5.6 Units and multipliers

Raw integer values are converted to physical units on the client side as `value × mult` with the
given `unit` — the controller never converts. `unit` may be a raw indication such as
`ADC counts` or `UsrUnit` when no physical conversion is defined.

### 5.7 Multi-datagram messages (v1.0)

In **v0.1** every message MUST fit in a single datagram. From **v1.0** onward a message whose
payload exceeds the single-datagram budget MAY be split across several datagrams ("fragments")
and reassembled by the receiver. There is no separate fragment header; reassembly relies on the
single-client session (§2) and the sequence number (§3.1), both of which are active in v1.0.

**`Payload length` is the total.** For every message — single- or multi-datagram — the
`Payload length` header field carries the size of the **whole logical payload**, not the bytes
in the current datagram. The byte count contributed by each individual datagram is taken from
the received UDP datagram length (UDP preserves message boundaries):

```
fragment_payload_bytes = udp_datagram_length − HEADER_SIZE (12) − TRAILER_SIZE (4)
```

For a single-datagram message the total equals the per-datagram count, so this definition is
backward-compatible.

**Fragment layout.** Every fragment is a complete, independently framed message: it has the
full 12-byte header (same `Magic`, `Version`, message type, and `Payload length` = total) and
its own 4-byte trailer. The fragments of one message carry **consecutive sequence numbers** and
their payload bytes are concatenated in sequence-number order; a fragment's offset into the
reassembled payload is the sum of the payload byte counts of all lower-numbered fragments. There
is no explicit per-fragment offset or index field — sequence order *is* the order.

**No interleaving (MUST).** A multi-datagram transfer MUST occupy a single, uninterrupted run of
sequence numbers: the controller MUST NOT emit any other datagram (of any message type) between
the first and last fragment of a transfer. In particular, a message MUST NOT be fragmented while
a telemetry stream is active, since streamed frames would consume sequence numbers and appear as
gaps. Fetch large messages (e.g. a `DESCRIPTION`) while idle, before `TELEMETRY` starts.

**Reassembly.** Receiving a fragmented message, the client:

1. reads `Payload length` (the total) from any received fragment;
2. orders received fragments by sequence number and verifies they form a **gap-free** run;
3. sums the per-datagram payload byte counts until they equal the total;
4. checks each fragment's per-datagram **CRC-16** (v1.0).

When the run is gap-free, the accumulated bytes equal the total, and every fragment's CRC
passes, the message is complete. The header CRC is **per-datagram** (per-fragment corruption
detection); there is no separate checksum over the reassembled payload — gap-free sequencing
plus the exact total-length match provides whole-message integrity.

**Loss, retries, duplicates.** If a fragment is lost the run never completes; the client MUST
apply a **reassembly timeout**, discard the partial message, and re-request. Each response to a
request begins a **fresh** contiguous run; the client anchors on the most recently started run
and discards stragglers bearing older sequence numbers. A duplicated fragment (a sequence number
already held) is ignored. Because the session is single-client and the controller resets its
sequence counter when a new client takes over (§2), sequence numbers are unambiguous within a
session.

## 6. Version matrix

| Capability | v0.1 (current) | v1.0 (full) |
|---|---|---|
| Magic / version / type / payload-length header | ✔ | ✔ |
| Request/response handshake (`DESCRIPTION` / `TELEMETRY` / `STOP`) | ✔ | ✔ |
| JSON descriptor + binary frame stream | ✔ (static descriptor) | ✔ |
| Single-client session (new client supersedes the old) | ✔ | ✔ |
| **Sequence number** | field present, sent as `0`, ignored | active, monotonic, used for loss detection |
| **CRC trailer** (Reserved + CRC-16) | present, sent as `0` and ignored | present, filled and validated |
| **Multi-datagram messages** (fragmented payloads) | not allowed — every message fits one datagram | supported (relies on sequence numbering) |
| Adaptive / runtime descriptor (runtime substitution of reserved names) | not implemented | candidate (TBD) |

`major.minor` is carried as two bytes (`Version major`, `Version minor`). A receiver MUST
check the version before parsing further, since major versions are not required to be
compatible.

## 7. Open items

1. **CRC-16 algorithm (v1.0)** — polynomial/variant to be fixed when v1.0 is finalized.
