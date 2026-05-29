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
digital plot. See [`docs/gui-design.md`](docs/gui-design.md). (Built on PyQt5 — see
[License](#license) for what that means for a distributed binary.)

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

## Packaging

Build distributable artifacts (`pip install -e .[package]` first):

```sh
python -m build                                       # wheel + sdist in dist/
python -m PyInstaller packaging/pulseudp.spec --noconfirm   # standalone app in dist/pulseUDP/
```

The PyInstaller build is **one-folder** (faster startup, fewer antivirus false positives than
one-file) and bundles the descriptor schema, Qt platform plugins, and the third-party license
texts. Ship the whole `dist/pulseUDP/` folder, e.g. zipped. Pushing a `vX.Y.Z` tag runs
`.github/workflows/release.yml`, which builds the wheel/sdist and the Windows zip and attaches
them to a GitHub Release.

## License

The pulseUDP **source and specification are released under [CC0 1.0](LICENSE)** (public domain).

⚠️ The **standalone binary** is a different matter: it bundles **PyQt5 (GPL v3)**, so the
*executable distribution as a whole is governed by the GPL v3*. Your own use of the CC0 source
is unaffected — only the shipped binary carries the GPL obligation, and it includes the required
license texts (see [`packaging/licenses/THIRD_PARTY_NOTICES.md`](packaging/licenses/THIRD_PARTY_NOTICES.md)).
The Qt-free modules (`pulseudp.protocol`, `pulseudp.client`, `pulseudp.model`) import no Qt and
remain freely reusable.
