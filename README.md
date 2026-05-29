# pulseUDP

On-the-fly telemetry collection tool designed for microcontrollers with Ethernet support.

pulseUDP is a UDP telemetry protocol plus a desktop client that receives and visualizes
telemetry streamed from an Ethernet-capable microcontroller. The microcontroller firmware is
out of scope for this repository.

## The protocol

See [`spec/RFC-pulseUDP.md`](spec/RFC-pulseUDP.md). In short: the client requests a JSON
descriptor from the controller over UDP port 2102, then starts a binary telemetry stream whose
frame layout is given by that descriptor and validated against `spec/Schema.json`.

## Development

```sh
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e .[dev]
```

Validate the example descriptor against the schema:

```sh
python spec/examples/validate.py spec/examples/ShortJSON.json spec/Schema.json
```

> The GUI framework is not yet finalized; PySide6 + pyqtgraph is the leading candidate for
> high-rate plotting. The application is currently a scaffold.
