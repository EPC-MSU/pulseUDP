# pulseUDP

On-the-fly telemetry collection tool designed for microcontrollers with Ethernet support.

pulseUDP is a UDP telemetry protocol plus a desktop client that receives and visualizes
telemetry streamed from an Ethernet-capable microcontroller. The microcontroller firmware is
out of scope for this repository.

## The protocol

See [`spec/RFC-pulseUDP.md`](spec/RFC-pulseUDP.md). In short: the client requests a JSON
descriptor from the controller over UDP port 2102, then starts a binary telemetry stream whose
packet layout is given by that descriptor and validated against `spec/Schema.json`.

## The client

A PyQt5 + pyqtgraph desktop app receives the descriptor, then plots the live stream — fields
sharing `units` share a plot, unitless fields get one each, and each bitfield is a stacked
digital plot. See [`docs/gui-design.md`](docs/gui-design.md). (PyQt5 is GPLv3, so the GUI app
is GPLv3.)

```sh
pip install -e .[gui]
python -m pulseudp                              # launch the client
```

Since the microcontroller firmware is out of scope, a UDP simulator drives the client during
development:

```sh
python tools/sim.py --rate 1000                 # serve the example descriptor on :2102
```

## Development

```sh
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e .[dev]
pytest
```

Validate the example descriptor against the schema:

```sh
python spec/examples/validate.py spec/examples/telemetry_example.json spec/Schema.json
```
