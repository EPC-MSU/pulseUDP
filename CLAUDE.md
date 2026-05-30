# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

pulseUDP is **two coupled deliverables**: a UDP telemetry protocol and a Python desktop client
that visualizes telemetry from Ethernet-capable microcontrollers. The microcontroller firmware
is **out of scope** for this repo. The repo is being prepared for **public release** — keep
working-group-internal references (Redmine links, internal usage scenarios, named colleagues)
out of published files under `spec/`.

This is a **spec-first** project. `spec/RFC-pulseUDP.md` is the normative protocol definition;
`spec/Schema.json` is the normative descriptor schema. Code in `src/pulseudp/` **implements**
the spec and must stay consistent with it — when you change wire behavior, change the RFC and
the code together.

## Layout

- `spec/RFC-pulseUDP.md` — the protocol RFC (the source of truth for wire behavior).
- `spec/Schema.json` — JSON-Schema (draft-07) that telemetry **descriptors** must validate against.
- `spec/examples/` — `telemetry_example.json` (illustrative descriptor, **not** normative) and `validate.py` (validator).
- `src/pulseudp/protocol.py` — wire framing/parsing + `Descriptor` (validate JSON, build a NumPy structured-dtype decode plan); mirrors RFC §3 (header) and §5.2 (type widths).
- `src/pulseudp/client.py` — `UdpClient`: socket + receiver thread, `DESCRIPTION`/`TELEMETRY`/`STOP` transactions; Qt-free (delivers via callbacks).
- `src/pulseudp/model.py` — `PlotModel` (units grouping) + thread-safe rolling `RingBuffer`.
- `src/pulseudp/discovery.py` — pluggable `Discovery`; default `NullDiscovery` finds nothing (no discovery protocol in the public RFC).
- `src/pulseudp/app.py`, `__main__.py` — PyQt5 + pyqtgraph GUI. Receiver thread fills the `RingBuffer`; a `QTimer` redraws on the GUI thread. Design in `docs/gui-design.md`.
- `tools/sim.py` — UDP telemetry simulator (firmware is out of scope); drives the client end to end.

## Commands

```sh
pip install -e .[gui]                  # GUI deps: PyQt5 + pyqtgraph + numpy (PyQt5 is GPLv3)
pip install -e .[dev]                  # package + pytest + numpy (editable, src layout)
python -m pulseudp                     # launch the GUI client
python tools/sim.py --rate 1000        # run the simulator (serves the example descriptor on :2102)
pytest                                 # run tests
pytest tests/test_x.py::test_name      # run a single test

# Validate a descriptor against the schema:
python spec/examples/validate.py spec/examples/telemetry_example.json spec/Schema.json
```

The GUI splits acquisition from rendering: a receiver thread decodes datagrams (NumPy
structured-dtype) into a thread-safe `RingBuffer`; a `QTimer` redraws curves on the GUI thread
(never paint Qt from a worker thread). X-axis = an auto-detected timestamp field; the rolling
history window is user-selectable. See `docs/gui-design.md`.

### Python environment gotcha (this machine)

Only Python **3.6** (the default `python`) and **3.8** (`py -3.8`) are installed — there is no
3.10+. `pyproject.toml` sets `requires-python = ">=3.8"`, so **prefer `py -3.8`** for everything
(tests, validator, the package). Both interpreters have `jsonschema` + `pytest` installed, but
3.8's `Scripts` dir isn't on PATH — invoke tools as modules (`py -3.8 -m pytest`). `protocol.py`
uses `from __future__ import annotations`, so its PEP 585 hints (`tuple[int, int]`) work on 3.8.
The GUI/decode stack (`numpy`, `PyQt5`, `pyqtgraph`) is installed on **3.8 only** — run the app,
the simulator, and the test suite with `py -3.8`.

## Protocol invariants (must hold across RFC ↔ code)

These span multiple sections of the RFC; get them right when editing either side:

- **Header is 12 bytes, little-endian:** Magic `0x50 0x55` (ASCII "PU", a fixed byte sequence,
  *not* an endian-dependent integer) · Version major (u8) · Version minor (u8) · **type/sequence
  word** (u32) · Payload length (u32). A **4-byte trailer** (Reserved u16 + CRC-16 u16) is
  present in **every** version.
- **Message type and sequence share one word:** `word = (message_type << 16) | sequence_number`
  — type in the high 16 bits, sequence in the low 16 bits. Both are therefore `uint16`.
- **Message-type constants are shared by a request and its response** (`DESCRIPTION 0x0001`,
  `TELEMETRY 0x0002`, `STOP 0x0003`); direction + payload disambiguate.
- **Telemetry packets:** each value occupies an integer number of **32-bit words** on the wire
  (≤4-byte types → 1 word; `int64`/`uint64`/`double` → 2 words). `Payload length` is the
  **total** logical payload size; a single datagram's byte count comes from the UDP datagram
  length (`udp_len − 12 − 4`).
- **Versioning is `major.minor`** (two bytes). The RFC documents **v1.0** (sequence numbering and
  CRC fields present but **ignored/zero**; single-datagram only) and **v2.0** (sequence + CRC
  active; multi-datagram messages allowed). Don't conflate the two.
- **Single client:** the server serves one client at a time; a command from a new
  source address supersedes the previous client and **resets session state including the
  sequence counter**. Multi-datagram reassembly (RFC §5.7) depends on this.
- **Descriptors are static in v1.0**, sent as a UTF-8 JSON string with no NUL terminator, and
  must validate against `spec/Schema.json`. `bitfield` is always a `uint32`; bits map to the
  `bits[]` names from the LSB up.

## Open item

The **CRC-16 algorithm/polynomial** for v2.0 is not yet fixed (see RFC §7).
