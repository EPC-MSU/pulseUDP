"""pulseUDP GUI application (scaffold).

The GUI:
  1. discovers / addresses a controller on UDP port 2102,
  2. requests the JSON descriptor (DESCRIPTION),
  3. starts the telemetry stream (TELEMETRY) and plots incoming frames,
  4. stops the stream (STOP) on exit.

The GUI framework (e.g. PySide6 + pyqtgraph for high-rate plotting) is not yet
chosen; see README. This is a placeholder entry point.
"""

from __future__ import annotations


def run() -> int:
    """Launch the GUI. Returns a process exit code."""
    raise NotImplementedError("pulseUDP GUI not implemented yet")
