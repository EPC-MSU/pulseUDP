# Third-party notices — pulseUDP standalone build

The pulseUDP **source code** is released into the public domain under
**CC0 1.0** (see the repository `LICENSE`).

This **standalone executable**, however, bundles third-party components. The
most restrictive of them, **PyQt5**, is licensed **GPL v3**. A combined work
that includes PyQt5 must be distributed under the GPL v3, so **this binary
distribution as a whole is provided under the terms of the GNU GPL v3**
(`gpl-3.0.txt` in this folder). The pulseUDP-authored portions remain CC0 and
may be reused freely outside this binary.

## Bundled components

| Component  | License        | Text                  |
|------------|----------------|-----------------------|
| PyQt5      | GPL v3         | `gpl-3.0.txt`         |
| Qt 5       | LGPL v3        | `lgpl-3.0.txt`        |
| pyqtgraph  | MIT            | (permissive)          |
| NumPy      | BSD 3-Clause   | (permissive)          |
| pulseUDP   | CC0 1.0        | repository `LICENSE`  |

Qt itself is available under the LGPL v3; the corresponding library sources can
be obtained from https://www.qt.io/. PyQt5 sources are available from Riverbank
Computing at https://www.riverbankcomputing.com/software/pyqt/.

If you wish to use pulseUDP without the GPL obligations of this binary, run it
from source with an LGPL Qt binding (e.g. PySide on a Python ≥ 3.9), or use the
Qt-free parts of the package (`pulseudp.protocol`, `pulseudp.client`,
`pulseudp.model`), which import no Qt at all.
