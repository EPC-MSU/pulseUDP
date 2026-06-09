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
- **History:** a fixed, preallocated rolling **ring buffer** whose window length (number of
  **samples**) is **user-selectable in the GUI** from a fixed list of magnitudes
  (`1K · 10K · 100K · 1M · 10M · 100M · 1G`, default **1M**). Older data is discarded; post-Stop
  pan/zoom is bounded to the retained window. A **min/max decimation pyramid** alongside the ring
  keeps redraw cost bounded by the plot's pixel width, so the window can be pushed into the
  multi-GiB range without the GUI lagging — see [Rendering pipeline](#rendering-pipeline-ring-buffer--minmax-pyramid).
  A requested size that would not fit in free RAM is **rejected** (the previous, smaller size is
  kept) so the client never tries to allocate beyond memory.
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
- `discovery.py` *(new)* — pluggable `Discovery` interface with two backends: `SsdpDiscovery`
  (the GUI default — an SSDP/UPnP M-SEARCH probe, see [Discovery](#discovery-search-button)) and
  `NullDiscovery` (finds nothing). SSDP is a separate standard protocol, so this keeps the pulseUDP
  RFC itself discovery-free.
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

1. **Connection bar (top):** `[Search]` (SSDP probe — see [Discovery](#discovery-search-button))
   → device list ↔ editable IP field (list selection fills it; manual edit allowed) →
   `[Connect]` (sends `DESCRIPTION`) → status label → `[Start/Stop]` telemetry → rolling-window
   size selector (samples).
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
- **Zoom/resize are query-only:** changing the view (wheel, pan) or resizing the window never
  recomputes stored data. Each redraw computes samples-per-pixel for the visible range and asks
  the buffer for a decimated slice at that resolution (below); the response is bounded by the
  pixel width, not the history length.

## Rendering pipeline: ring buffer + min/max pyramid

The history store (`model.RingBuffer`) decouples *storage* from *rendering* so a multi-GiB window
draws at screen resolution without per-frame work proportional to its size. Two structures, both
**channel-major** and built for ≤16 simultaneous channels:

- **Raw ring** — a preallocated `(n_channels, capacity)` `float32` array with a wrapping write
  index. Appends are a single vectorised slice-assign (no per-frame concatenation, the old chunk
  list's cost); the X axis is *not* stored — it is the integer sample index, regenerated as
  `float64` on read (`float32` cannot represent sample indices past `2**24`). `float32` halves the
  footprint versus the decoded `float64`, which matters at large windows; plotting needs only
  ~7 digits.
- **Min/max pyramid** — for each level `L ≥ 1`, two `(n_channels, capacity / Fᴸ)` arrays holding
  the **min and max** of every `Fᴸ` raw samples, with **`F = 8`**. `F = 8` keeps the level count
  low (one level per three binary octaves) while bounding total pyramid memory to ≈ `1/(F−1) ≈ 14%`
  of the raw store. The pyramid is itself a set of rings aligned to the raw ring, so coarse buckets
  age out in lockstep with the raw samples they summarise.

**The pyramid is maintained incrementally, never rebuilt from all data.** On each `append`, the
new samples are folded up the levels — level 1 from the raw block, level `L` by reducing `F`
freshly-completed level-`L−1` buckets — touching only the buckets the new block lands in. Cost is
`O(n_new · n_levels)`, i.e. **amortised O(1) per sample**, independent of how much history already
exists. Only complete buckets are stored; the in-progress tail bucket is filled in once it
completes, so a coarse zoom-out can lag the very latest sample by at most one bucket width.

**Reading is `O(visible pixels)`.** `RingBuffer.view(x0, x1, max_points)` clamps the range to the
resident window, computes `samples_per_pixel = span / max_points`, and picks the coarsest level
whose bucket spans ≤ one pixel (`L = ⌊log_F(spp)⌋`, clamped). It then slices that level's min/max
rings over the visible range and returns ~`2 · pixel_width` points. When zoomed in past one
sample per pixel it returns **exact raw samples** instead. The GUI draws an envelope by emitting
two points per bucket (min then max) at the bucket's X, so spikes survive decimation; this is the
agreed trade-off — **every level except the finest shows the min/max envelope, the finest is exact
samples**. Disabled v2.0 channels are all-NaN and reduce to NaN buckets, which draw as gaps.

`pyqtgraph`'s own auto-downsampling is therefore left off (we pre-decimate), and global
antialiasing is disabled because the point count is already near the pixel width.

**Changing the history size** (`set_window`) reallocates the ring/pyramid and copies the retained
**tail back at its original sample indices** so the X axis stays continuous, then rebuilds the
pyramid over it — an `O(retained)` one-shot on a deliberate user action, not a per-frame cost.

**RAM guard.** `RingBuffer.footprint_bytes(n_channels, window_n)` reports the exact allocation
(raw + pyramid). Before accepting a new history size the GUI checks it against
`RAM_BUDGET_FRACTION` (0.8) of free RAM (via `psutil` if present, else `/proc/meminfo`); an
over-budget choice is logged and rejected (the previous size is kept), and once the real channel
count is known on connect an over-budget default is clamped down to the largest option that fits.

## Discovery (Search button)

The pulseUDP RFC defines no discovery handshake, so the **Search** button uses a standard,
RFC-independent mechanism instead: an **SSDP/UPnP M-SEARCH** probe (`discovery.SsdpDiscovery`,
the GUI default). SSDP only locates a host's IP address; the client then addresses it on the
telemetry port exactly as if the IP had been typed in, so nothing about the wire protocol changes.

- **Probe.** A `M-SEARCH * HTTP/1.1` datagram (`MAN: "ssdp:discover"`, `MX: 2`, `ST: ssdp:all` —
  the broadest target) is multicast to `239.255.255.250:1900`. On a **multi-homed host** (common
  on Windows) the probe is sent on **every IPv4 interface** (enumerated via `ifaddr`, a `gui`-extra
  dependency), so devices on secondary subnets are not missed; an interface that fails to bind is
  logged and skipped. The probe is sent twice per interface to ride out UDP loss.
- **Collect.** `HTTP/1.1 200 OK` replies and `ssdp:alive` `NOTIFY` advertisements are gathered for
  the search timeout (~3 s, ≥ `MX`); `ssdp:byebye` is ignored. Responders are de-duplicated by
  their `LOCATION` URL.
- **Name & filter.** Each unique `LOCATION` is fetched over HTTP and its `<friendlyName>` is read
  from the UPnP description (namespace-agnostic XML parse, regex fallback). **Only devices that
  yield a friendly name are listed** — a responder without a readable description is dropped rather
  than shown as an opaque address. The dropdown shows `name (address)`; selecting one fills the IP
  field with the address.
- **Threading.** The probe blocks for a few seconds (multicast wait + per-device HTTP fetches), so
  `search()` runs on a worker thread; the Search button shows *Searching…* and is disabled until
  results arrive on the GUI thread via a Qt signal. Discovery progress is mirrored to the log dock.

`NullDiscovery` (finds nothing) remains available for environments with no SSDP responders or
where multicast is undesirable.

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

Selectable telemetry fields (RFC §8). (Device discovery is now implemented via SSDP — see
[Discovery](#discovery-search-button).)

The v2.0 wire path is fully implemented: the CRC is validated (CRC-16/CCITT-FALSE, RFC §3.2)
and large messages split across several datagrams are reassembled length-delimited (RFC §5.6,
300 ms timeout, all-or-nothing) — exercised by `tools/sim.py --version 2.0 --descriptor
spec/examples/telemetry_long_example.json`, whose descriptor reply spans three datagrams.
