"""Device discovery (pluggable).

The public pulseUDP RFC does not define a discovery handshake — the
working-group discovery protocol was intentionally left out of the spec. The
GUI's Search button therefore talks to a :class:`Discovery` backend, and the
shipped default is a no-op stub that finds nothing. A real backend (UDP
broadcast probe, mDNS, …) can be dropped in later without touching the GUI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class Device:
    """A discovered controller the user can pick from the list."""

    name: str
    address: str        # IPv4 string the IP field is filled with


class Discovery:
    """Discovery backend interface."""

    def search(self, timeout: float = 1.0) -> List[Device]:
        """Return the controllers found within ``timeout`` seconds."""
        raise NotImplementedError


class NullDiscovery(Discovery):
    """Default backend: finds nothing (no discovery protocol in the public RFC)."""

    def search(self, timeout: float = 1.0) -> List[Device]:
        return []
