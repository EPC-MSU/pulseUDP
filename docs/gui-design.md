# pulseUDP client — GUI design

This is the implementation guide for deliverable #2, the desktop telemetry client.
It implements the protocol in [`../spec/RFC-pulseUDP.md`](../spec/RFC-pulseUDP.md) and
parses descriptors validated against [`../spec/Schema.json`](../spec/Schema.json).

## Confirmed decisions

- **Qt binding:** PyQt5. Best support across the `requires-python >=3.8` range and most
  battle-tested with pyqtgraph. PyQt5 is **GPLv3**, so the released app is GPLv3.
- **X axis:** a synthetic **per-sample counter** the client generates — each received
  telemetry packet gets the next integer index. A datagram lost in transit (silently in v1.0,
  logged as a `seq_gap` row in v2.0) simply means the index skips the lost samples, with no
  visible gap. Note the granularity is *message*, not *packet*: v2.0 sequence numbers detect a
  missing message but not how many packets it held.
- **History:** rolling ring buffer whose window length (number of **samples**) is
  **user-selectable in the GUI**. Older data is discarded; post-Stop pan/zoom is bounded to the
  retained window.
- **Test source:** a UDP simulator (`tools/sim.py`), since the microcontroller firmware is
  out of scope and no real device exists.
- **Grouping:** fields sharing `units` share one plot; **unitless fields get one plot each**;
  each `bitfield` is one stacked digital plot with one colored trace per bit (`Reserved`
  bits hidden).
- **Protocol version:** auto-negotiated, not chosen in the UI. On `Connect` the client sends the
  opening `DESCRIPTION` framed at the highest supported version (v2.0: active sequence + real
  CRC-16/CCITT-FALSE, RFC §3.2) and **fixates the session to whatever version the server
  reveals in its reply** (RFC §6.1). Because a server answers any request regardless of its
  version field, this is a single round trip with no fallback: a v2.0 server replies v2.0, a
  v1.0 server replies v1.0, and the client adopts it. A reply in an unsupported version is
  reported as incompatible; no reply at all is a connection failure (unreachable endpoint), never
  a version mismatch. The negotiated version is logged (`negotiated protocol vX.Y`).
- **Per-version wire behavior:** inbound datagrams are judged by their **own** version field.
  Under v2.0 the CRC is validated over Magic..Reserved and a mismatch drops the datagram with a
  `crc` log row, while telemetry sequence loss logs a `seq_gap` row. Under v1.0 the sequence and
  CRC fields are sent as zero and ignored, so those rows stay inert; bad-magic / unknown-version
  / short-datagram / decode-size-mismatch are active in both versions.

## Library stack

- **PyQt5** — widgets, event loop, `QTimer`, signals/slots.
- **pyqtgraph** (0.13.x) — `PlotWidget` + custom `ViewBox` for the zoom state machine.
- **NumPy** — structured-dtype packet decode and ring buffers.
- **stdlib `socket` + `threading`** — blocking receiver socket in its own thread (not
  `QUdpSocket`, which couples to the GUI event loop and stalls at ~20 kHz).

## Module layout (`src/pulseudp/`)

- `protocol.py` *(exists; extended)* — header framing, `MessageType`, `TYPE_WORDS`, plus
  `Descriptor` (parse + validate, build decode plan) and the NumPy decoder.
- `client.py` *(new)* — `UdpClient`: socket lifecycle, `DESCRIPTION`/`TELEMETRY`/`STOP`
  transactions with retransmit timeout, receiver thread, Qt signals for samples + log events.
- `discovery.py` *(new)* — pluggable `Discovery` interface with a no-op stub backend (Search
  returns nothing for now; keeps the public RFC discovery-free).
- `model.py` *(new)* — `RingBuffer` (sample-counter X, sample-bounded history), channel/units
  grouping.
- `app.py` *(replaces stub)* — `MainWindow` and panels.
- `tools/sim.py` *(new)* — UDP telemetry simulator.

## Threading & data flow

```
[receiver thread]                         [GUI thread]
 blocking recvfrom ──► decode datagram      QTimer ~60 Hz
   → NumPy structured-dtype view              └─ read latest slice of RingBuffer
   → (n packets × fields) float array         └─ update pyqtgraph curves
   → append to RingBuffer (lock)              user events → UdpClient (queued signals)
   → emit log events (loss/CRC/bad-magic)
```

Acquisition is threaded; **rendering is timer-driven on the main thread**. The redraw rate is
fixed (~60 Hz) regardless of the inbound packet rate.

## Packet decode

From the descriptor build a NumPy **structured dtype** with explicit per-field offsets and
`itemsize = packet_size`. Each field reads the low bytes of its 32-bit word span
(little-endian → an `int16` in a 4-byte word reads its low 2 bytes; `double` reads 8). Then
`np.frombuffer(payload[:n*packet], dtype=plan)` decodes a whole datagram (up to 45 packets) in
one call; multipliers apply as vectorized float ops. `n_packets = payload_len // packet_size`
(trailing pad ignored, per RFC §5.3).

## UI layout (single window, stacked panels)

1. **Connection bar (top):** `[Search]` → device list ↔ editable IP field (list selection
   fills it; manual edit allowed) → `[Connect]` (sends `DESCRIPTION`) → status label →
   `[Start/Stop]` telemetry → rolling-window size selector (samples).
2. **Telemetry list (left):** one row per field — name, type, a checkbox **checked + disabled**
   (reserved for the future selectable-fields feature, RFC §8), and a color swatch matching
   the curve. Every field is plotted; the X axis is a synthetic sample counter.
3. **Graph area (center/right):** one `PlotWidget` per units group; one per unitless field;
   one stacked digital plot per bitfield.
4. **Log dock (bottom, collapsible):** host-time timestamps; categories listed above.

## X axis & zoom/pan state machine

- **X base:** a synthetic per-sample counter (sample index since the buffer was last cleared).
- **Running:** X window width `W` (default **500 samples**); the view auto-scrolls so the right
  edge tracks the latest sample. Wheel: `W ×= 0.5 / ×2` (clamped), still following latest. Y
  autoscales to the visible window.
- **Stopped:** free pan; wheel zooms in **×2 steps anchored at the mouse pointer** (custom
  `wheelEvent` overriding pyqtgraph's continuous zoom). No auto-follow.

## Simulator (`tools/sim.py`)

Listens on UDP 2102; answers `DESCRIPTION` with `spec/examples/telemetry_example.json`; on
`TELEMETRY` streams RFC-conformant packets (configurable rate, multiple packets per datagram,
optional injected loss / bad CRC to exercise v2.0 log paths). Honors single-client supersede
and the `STOP` ack.

## Testing

Headless units: descriptor → decode-plan correctness, decode round-trip vs `tools/sim.py`,
ring-buffer trim-by-sample-window, sequence-gap detection. GUI smoke test optional
(`pytest-qt`), kept minimal.

## Deferred (unchanged by this design)

Real discovery algorithm; selectable telemetry fields (RFC §8).

The v2.0 wire path is fully implemented: the CRC is validated (CRC-16/CCITT-FALSE, RFC §3.2)
and large messages split across several datagrams are reassembled length-delimited (RFC §5.6,
300 ms timeout, all-or-nothing) — exercised by `tools/sim.py --version 2.0 --descriptor
spec/examples/telemetry_long_example.json`, whose descriptor reply spans three datagrams.
