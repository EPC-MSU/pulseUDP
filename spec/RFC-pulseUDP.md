# pulseUDP — Telemetry-over-Ethernet Protocol

**Status:** Draft / working document
**Versions described:** `1.0` (lite) and `2.0` (full)
**Date:** 2026-05-29

---

## 1. Overview

Ethernet bandwidth in modern microcontrollers (100BASE-TX) is high enough to provide several synchronous data points per telemetry packet at the ~20 kHz rates typically used for motor control. For example, 8 full 32-bit words at a 20 kHz rate use less than 6% of the 100 Mbit/s capacity, even accounting for UDP overhead. This favors creation of a telemetry streaming protocol developed for 32-bit microcontrollers with Ethernet support - **pulseUDP**. The system requirements for microcontroller implementation are about 10 kbytes considering that the prerequisite UDP/IP stack usually takes about 10 times more. The protocol has two versions:

* Lite one `v1.0` with hardcoded telemetry list, one UDP datagram per packet, no CRC check, no packet sequencing, no version check. Easier to implement, fast to compute on the microcontroller side.
* Full one `v2.0` with user-selectable telemetry streams, multiple datagram packets, error detection.

The protocol has two parts:

1. **A small framed message format** carried in UDP datagrams (magic, version, message type,
   sequence number, payload length, payload, and a CRC trailer). Sequence numbering and CRC
   are present in the header/trailer of every version but only become active in v2.0.
2. **A JSON descriptor** that the controller sends on request, describing the layout structure of a
   telemetry packet so the client can parse the binary stream generically.

A protocol is strictly request/response: the server only sends message(s) as a response to the client; the message type of the response must be the same as in the request.

## 2. Transport

- **Protocol:** UDP.
- **Port:** `2102` (server listens here; client sends commands here).
- **Byte order:** little-endian for all multi-byte fields. The sole exception is the
  `GET_CHANNELS`/`SET_CHANNELS` channel bitmap (§4), sent most-significant word first.
- **MTU budget:** one datagram payload is kept within a single Ethernet frame — **1472 bytes**
  of UDP payload (1500 MTU − 20 IP − 8 UDP). In **v1.0** every message MUST fit in a single
  datagram. From protocol **v2.0** onward **any** message MAY be split across multiple datagrams 
  and reassembled by the receiver (see §5.6). Keeping a message to a single datagram is still preferred 
  for efficiency and stability where it fits; a sender splits only when a batch (e.g. a large telemetry message or a long descriptor reply) genuinely overflows.

**Single client.** The server serves exactly one client at a time. It learns the client's
IP address and port from the source of any valid command it receives, and streams telemetry
back to that address and port. A valid command from a **new** source address/port immediately
**supersedes** the previous client: the server aborts any in-progress stream or transfer to
the old client, resets its session state (including the sequence counter), and re-initializes
for the new client. There is no multi-client support; the most recent client wins.

## 3. Message format

A **message** is the logical unit defined here: a fixed header, an optional payload, and a CRC
trailer, all contiguous with no padding. In **v1.0** a message occupies exactly one UDP
datagram. In **v2.0** a large message MAY be split across several datagrams on the wire (§5.6);
the layout below always describes the **reassembled** message.

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

**Header size:** 12 bytes. **Trailer:** 4 bytes.

### 3.1 Field semantics

| Field | Type | Meaning |
|---|---|---|
| **Magic** | 2 bytes | The byte sequence `0x50 0x55` (ASCII "PU"), in this order on the wire — defined as a fixed byte sequence, not an endian-dependent integer. A datagram that does not begin with these bytes MUST be ignored, **except** continuation datagrams of a multi-datagram message in progress (§5.6), which carry raw payload and have no header. Lets a receiver find message boundaries / reject noise. |
| **Version major** | `uint8` | Incompatible protocol revision. This document covers major `1` and `2`. |
| **Version minor** | `uint8` | Compatible additions within a major. Combined value is written `major.minor` (e.g. `1.0`, `2.0`). Two different versions are **not** required to be wire-compatible. |
| **Type & sequence** | `uint32` | One 32-bit word carrying two sub-fields: **message type** in the high 16 bits, **sequence number** in the low 16 bits — `word = (message_type << 16) \| sequence_number`. |
| ↳ **Message type** | `uint16` | What the message is / what it commands. See §4. |
| ↳ **Sequence number** | `uint16` | Per-sender monotonic counter (wraps at 65536), one value **per message**, for stream-level loss detection. For a multi-datagram message (§5.6) it appears once, in the header (first datagram). **v1.0:** sender sets `0`, receiver ignores. **v2.0:** active. |
| **Payload length** | `uint32` | Total number of payload bytes in the message — for a multi-datagram message (§5.6), the sum across all datagrams. MUST be 32-bit word aligned. May be `0`. |
| **Payload** | bytes | Type-dependent; see §4 and §5. |
| **Reserved** | `uint16` | Present in every version. Sender sets `0`, reserved for future use. |
| **CRC-16** | `uint16` | Present in every version. CRC-16 (algorithm in §3.2) over the bytes from Magic through the end of Reserved (i.e. the whole message except the CRC field itself); for a multi-datagram message this covers the **entire reassembled** message. Stored as a `uint16` **little-endian** (low byte at offset 14+N), like every other multi-byte field. **v1.0:** sender sets `0`, receiver ignores. **v2.0:** filled and validated. |

### 3.2 CRC-16 algorithm

The CRC-16 in the trailer is **CRC-16/CCITT-FALSE**. To leave no ambiguity for
independent (including microcontroller) implementations, the full parameter set is:

| Parameter | Value |
|---|---|
| Width | 16 bits |
| Polynomial | `0x1021` |
| Initial value | `0xFFFF` |
| Input reflected | no |
| Output reflected | no |
| Final XOR | `0x0000` |
| **Check** (CRC of the ASCII bytes `"123456789"`) | `0x29B1` |

The **Check** value is the conformance test: an implementation that computes
`0x29B1` over the nine bytes `31 32 33 34 35 36 37 38 39` is correct. (This is the
non-reflected, init-`0xFFFF` variant — sometimes called "CCITT-FALSE" — **not** the
reflected, init-`0x0000` Kermit variant, which would yield a different value.)

**Coverage:** the CRC is computed over every byte of the message except the two
CRC bytes themselves — Magic, both Version bytes, the Type & sequence word, Payload
length, the whole Payload, and Reserved, in wire order. For a multi-datagram message
(§5.6) it is computed over the **entire reassembled** message, not per datagram.

The 16-bit result is then written to the trailer little-endian (§3.1), so on the wire
a receiver may equivalently verify the message by running the same CRC over the bytes
from Magic through the end of Reserved and comparing against the stored value.

Reference (bitwise, MSB-first — matches the parameters above):

```
crc = 0xFFFF
for each byte b in covered bytes:
    crc ^= (b << 8)                       # 16-bit register
    repeat 8 times:
        if crc & 0x8000: crc = (crc << 1) ^ 0x1021
        else:            crc = (crc << 1)
    crc &= 0xFFFF
# crc is the CRC-16 value; store little-endian
```

## 4. Message types

A message type is the high 16 bits of the word at offset 4 (see §3.1) and occupies **2 bytes**.
A request and the response it triggers carry the **same** message-type constant; direction and
payload distinguish them.

**Every request MUST produce a response.** `DESCRIPTION`, `STOP`, `GET_CHANNELS`, and
`SET_CHANNELS` are each answered by exactly one response message; `TELEMETRY` is answered by the
telemetry stream, whose first datagram acknowledges that streaming has begun. A client that does not observe the response within
a timeout retransmits the request (idempotent: a repeated request simply restarts the same
transaction).

| Constant | Value (uint16) | Transaction | client → server | server → client |
|---|---|---|---|---|
| `DESCRIPTION` | `0x0001` | Get descriptor | request, empty payload | JSON descriptor (see §5), UTF-8, NUL-terminated and padded to a 32-bit word boundary |
| `TELEMETRY` | `0x0002` | Telemetry stream | request to start, empty payload | streamed telemetry messages, with one or more packets (see §5.3); in **v2.0** a message MAY span several datagrams (§5.6), until stopped |
| `STOP` | `0x0003` | Stop stream | request, empty payload | acknowledgement, empty payload (sent once streaming has ceased) |
| `GET_CHANNELS` | `0x0004` | Read enabled channels (v2.0) | request, empty payload | the channel bitmap (defined below): the currently enabled channels |
| `SET_CHANNELS` | `0x0005` | Set enabled channels (v2.0) | request, a channel bitmap (defined below): the channels to enable | the channel bitmap the server accepted; may differ from the request |

### 4.1 Session flow

```
client                                      server
 |  DESCRIPTION (request)  ─────────────────►|   (records client source IP:port, determines the protocol)
 |◄────────────  DESCRIPTION (JSON reply)    |   ← response (required)
 |  GET_CHANNELS (v2.0 only)  ──────────────►|   (read enabled channels)
 |◄────────────  GET_CHANNELS (v2.0 only)    |   ← response (required)
 |  TELEMETRY (start request)  ─────────────►|
 |◄──────────────  TELEMETRY (packets)       |  ┐  first packet = ack (required)
 |◄──────────────  TELEMETRY (packets)       |  │  continuous stream
 |◄──────────────  TELEMETRY (packets)       |  ┘
 |  STOP  ──────────────────────────────────►|
 |◄──────────────────────  STOP (ack)        |   ← response (required)
```

Every request is answered by a response (the dashed-back arrows). The client repeats a request
until it observes that response. Because the protocol is explicitly request/response and a
request and its reply share a message type, the receiver distinguishes them by direction and
payload — no heuristics.

The protocol design allows client-side version discovery. Since a v1.0 server ignores the version number of incoming packets, the client can send a `v2.0` request and learn the server's protocol version from its reply (§6.1).

The handshake prerequisite differs by version. In v1.0 every descriptor field is enabled and the
list is immutable, so the descriptor alone suffices to parse telemetry. In v2.0 the descriptor
lists only the *possible* channels; the client uses `GET_CHANNELS`/`SET_CHANNELS` to read and
choose the enabled subset before streaming.

**Channel bitmap.** The `GET_CHANNELS`/`SET_CHANNELS` payload is a pure-binary bitmap (no JSON, so
a microcontroller needs no parser) of ⌈N/32⌉ `uint32` words, where *N* is the channel count in the
descriptor. The bitmap is one big integer sent **most-significant word first**: its overall
least-significant bit — the LSB of the **last** word — is the descriptor's first channel, and bit
*k* (from 0) is descriptor channel *k*+1, where `1` means enabled. Each extra group of 32 channels
prepends one more word. Only this word order is most-significant-first — the bytes **within** each
`uint32` stay little-endian; this is the **one exception** to the little-endian rule of §2.

Each descriptor entry is exactly one channel, in listed order. A `bitfield` is a single channel:
its one bit enables or disables the whole bitfield (all of its flags) together — individual flags
cannot be toggled.

A `SET_CHANNELS` response need not echo the request: the server returns the set it actually
accepted, which may keep the previous list, revert to a default, truncate to its capacity, or
disable everything.

## 5. Payload: telemetry packets and the JSON descriptor

### 5.1 The descriptor

The `DESCRIPTION` payload is a JSON object describing telemetry packet capabilities. It is sent as a
single UTF-8 string terminated by one NUL (`0x00`) byte, then padded so the payload length is a
whole number of 32-bit words (§3). A reader takes the JSON as the bytes up to the first NUL. Its
structure is defined in the Schema.json file.

The descriptor supports the list of the telemetry values, describing their

* types, so that we distinguish float from int32 or uint32 types and also can support two-word double and int64 types
* names, so that we can label the graphs in GUI
* units, so we can know the physical values where units apply
* multiplier, so that the microcontroller may send native representation (possible fixed point) and the conversion is done in the client

The special flag type is also supported so binary flags can be sent along with numbers.

There is a difference in interpretation of JSON descriptor between the protocol versions. In v1.0 the descriptor lists exactly the fields of the telemetry packet. In v2.0 the list consists of the possible telemetry channels the server can provide; a separate channel negotiation (§4) yields the enabled subset needed to parse the payload. The number of telemetry packets in one message is derived from the data-point count and their size.

The descriptor also carries a `version` (required) — its own revision in semantic-versioning form, e.g. `1.0.0`. This descriptor version is independent of the protocol version in the message header (§3); it lets a client recognise when the telemetry layout has changed. An optional `id` object may carry server identification (such as device name, serial number, and firmware version) for display; its inner structure is not yet fixed.

A descriptor MUST validate against the pulseUDP JSON-Schema (draft-07), published alongside
this document as **`Schema.json`**. That schema is the normative definition of the descriptor
format; this section only summarizes it.

A worked example descriptor and a validator are provided under `examples/`:

- `examples/telemetry_example.json` — an example descriptor (illustrative only, not normative).
- `examples/validate.py` — validates a descriptor against the schema:
  `python examples/validate.py examples/telemetry_example.json Schema.json` (requires the `jsonschema` package).

### 5.2 Types and on-wire width

**Each value occupies an integer number of 32-bit words on the wire.** The value is stored in
the low bytes of its word span (little-endian); any remaining high bytes are
sign/zero-extension per the declared type and are ignored by a reader that already knows the
type. This keeps every field naturally aligned and lets the server serialize straight
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
the descriptor, starting from the least-significant bit. Having fewer than 32 names, the high bits are
zero. The bit gaps are supported by using name `Reserved` in the descriptor.

### 5.3 Packet stream

A `TELEMETRY` payload is an integer number of telemetry packets laid end to end with a possible gap in the end. The trailing gap may happen when an integer number of telemetry packets can't fit in the payload. Each packet is the field values in descriptor order, each value sized per §5.2. The number of
packets in a message = round_down( Payload length ÷ packet size ), where `Payload length` is the
message total (§3).

### 5.4 Example packet

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
| | **Packet total** | | 8 | 32 |

**Datagram packing example:** header 12 B + 45 packets × 32 B + 4 B trailer = 1456 B ≤ 1472 B.
So up to **45 telemetry packets fit one UDP datagram**. In **v1.0** that is the hard per-message
limit. In **v2.0** a telemetry message MAY carry more (e.g. 100 packets) and be split across
several datagrams per §5.6; the reassembled message still has a single header, trailer, sequence
number, and whole-message CRC.

### 5.5 Units and multipliers

Raw integer values are converted to physical units on the client side using the descriptor as `value × mult` with the given `units` — the server never converts. `units` may be a raw indication such as
`ADC counts` or `UsrUnit` when no physical conversion is defined.

### 5.6 Multi-datagram messages (v2.0)

In **v1.0** every message MUST fit in a single datagram. From **v2.0** onward a message whose
payload exceeds the single-datagram budget MAY be split across several datagrams and reassembled
by the receiver. A multi-datagram message has **one header, one payload, and one trailer** — the
same layout as §3 — simply spread over consecutive datagrams. There is no per-datagram header.

**On-wire split.** The message is serialized as one contiguous byte string
(header ‖ payload ‖ trailer) and chopped into datagram-sized pieces:

```
datagram 1:  [ 12-byte header ][ payload bytes … ]
datagram 2:  [ … more payload bytes … ]
   …
datagram k:  [ … last payload bytes ][ 4-byte trailer ]
```

Only the **first** datagram carries the header (with `Magic`, `Version`, the message type, the
single `sequence_number`, and `Payload length` = the total payload size). Every following
datagram is **raw continuation bytes** with no header and no magic. The trailer (Reserved +
CRC-16) ends the last datagram.

**Reassembly is length-delimited.** The receiver:

1. validates the first datagram's header (magic, version) and reads `Payload length` = P;
2. enters reassembly mode and **concatenates the bytes of subsequent datagrams in arrival
   order** onto the buffer;
3. stops once it holds `HEADER_SIZE (12) + P + TRAILER_SIZE (4)` bytes — the message is complete;
4. verifies the **CRC-16 over the whole reassembled message** and accepts or rejects it.

No per-datagram sequence/offset is needed because the byte count is known in advance and the
server sends the pieces back to back.

**In-order, all-or-nothing.** This scheme assumes the datagrams of one transfer arrive **in
order**, which the single-client session (§2) and strict request/response (§4) make the normal
case: the server emits the datagrams of a message back to back with nothing interleaved between
its first datagram and its trailer. For a telemetry stream this applies per message — the server
finishes one multi-datagram telemetry message (header → continuation → trailer) before beginning
the next, each message carrying its own header, sequence number, and whole-message CRC. The
receiver cannot reorder headerless pieces, so
any reordering, loss, or duplication makes the concatenation wrong and the CRC (or the final
length) **rejects the entire message** — there is no partial recovery or per-datagram
retransmit. This can happen in the big networks with various ways of packet transfer, where packets order may change.

**Loss, timeout, retry.** If a datagram is lost the buffer never reaches the expected length; the
client MUST apply a **reassembly timeout** of `300 ms` and discard the partial buffer. For a
one-shot transaction (`DESCRIPTION`, `STOP`) it then re-requests (the transaction is idempotent,
§4); on a CRC reject it likewise discards and re-requests. For the continuous telemetry stream
there is nothing to re-request — a lost or CRC-rejected message is simply dropped, surfacing as a
**sequence gap** at the next good message, and streaming continues. The single `sequence_number`
identifies the *message* (for stream-level loss detection), not the pieces within it.

## 6. Version matrix

| Capability | v1.0 (lite) | v2.0 (full) |
|---|---|---|
| Magic / version / type / payload-length header | server sets its version but doesn't check client's version | ✔ |
| Telemetry ready after `DESCRIPTION` alone (no channel negotiation) | ✔ | ✗ |
| Single-client session (new client supersedes the old) | ✔ | ✔ |
| **Sequence number** | field present, sent as `0`, ignored | active, monotonic, used for loss detection |
| **CRC trailer** (Reserved + CRC-16) | present, sent as `0` and ignored | present, filled and validated |
| **Multi-datagram messages** (one header/trailer, payload split over datagrams) | not allowed — every message fits one datagram | supported; in-order, all-or-nothing, whole-message CRC |
| User-selectable telemetry channels | ✗ | ✔ |

`major.minor` is carried as two bytes (`Version major`, `Version minor`). A client MUST
read the version before parsing payload semantics, since major versions are not required to be
wire-compatible in their *payloads*.

A **request**, however, is version-independent above the header: `DESCRIPTION`, `TELEMETRY`,
and `STOP` requests carry no payload, and the only header fields that differ between v1.0 and
v2.0 are `sequence_number` and the CRC — which v1.0 already ignores. A server therefore
MUST answer a well-formed request **regardless of the request's version field**, applying its
own version's rules, and MUST stamp the response with the **server's own** version. A v1.0
server in particular ignores the client's version exactly as it ignores the client's
sequence and CRC. This is what makes the client-side version discovery in §6.1 a single
round-trip with no fallback.

### 6.1 Version negotiation (client procedure)

There is no separate negotiation message; the version a session uses is **discovered from the
`DESCRIPTION` response** and then fixed for the rest of the session:

1. The client sends `DESCRIPTION` framed as the **highest** version it supports (currently
   **v2.0**: active sequence number + valid CRC).
2. Every reachable server answers (§6), and the response header carries the server's
   own version. The client reads that version:
   - if it is a version the client supports (v1.0 or v2.0), the client **fixates** it and frames
     all further requests (`TELEMETRY`, `STOP`) accordingly;
   - if it is a version the client does **not** support, the client reports an incompatible
     server and does not stream.

Because a v1.0 server validates neither the probe's sequence nor its CRC, and a v2.0
server validates both (the v2.0 probe satisfies both), the first `DESCRIPTION` always
elicits a version-revealing reply. The client never has to guess or alternate versions.

## 7. Open issues

1. **Integrity strength** — the v2.0 trailer carries a 16-bit CRC (CRC-16/CCITT-FALSE, §3.2).
   For large multi-datagram messages this has a non-negligible chance of accepting a corrupted
   stream. A future major version may widen the trailer to a CRC-32; this would be a
   wire-incompatible change and is therefore deferred to a new `Version major`.
