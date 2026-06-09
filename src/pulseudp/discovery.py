"""Device discovery (pluggable).

The pulseUDP RFC does not define a discovery handshake — the working-group
discovery protocol was intentionally left out of the spec. The GUI's Search
button therefore talks to a :class:`Discovery` backend chosen at construction
time, decoupled from the wire protocol.

Two backends ship:

* :class:`SsdpDiscovery` — the GUI default. It finds devices with a standard
  **SSDP/UPnP M-SEARCH** probe (multicast ``239.255.255.250:1900``), then reads
  each responder's UPnP description over HTTP and lists those that advertise a
  ``<friendlyName>``. SSDP is a separate, standard protocol — using it here adds
  no discovery requirement to the pulseUDP RFC; it only locates a host's IP,
  which the client then addresses on the telemetry port like any typed-in IP.
* :class:`NullDiscovery` — a no-op backend that finds nothing, for environments
  with no SSDP responders (or where multicast is undesirable).
"""

from __future__ import annotations

import re
import select
import socket
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# Standard SSDP multicast endpoint (UPnP Device Architecture, RFC-independent).
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900


@dataclass
class Device:
    """A discovered server the user can pick from the list."""

    name: str
    address: str        # IPv4 string the IP field is filled with


class Discovery:
    """Discovery backend interface."""

    def search(self, timeout: float = 1.0) -> List[Device]:
        """Return the servers found within ``timeout`` seconds."""
        raise NotImplementedError


class NullDiscovery(Discovery):
    """Backend that finds nothing (no SSDP probe is sent)."""

    def search(self, timeout: float = 1.0) -> List[Device]:
        return []


# -- SSDP response parsing (pure helpers, no I/O — unit-tested directly) -------

def parse_ssdp_headers(data: bytes) -> Optional[Dict[str, str]]:
    """Parse an SSDP datagram into a lower-cased header dict.

    Accepts both M-SEARCH replies (``HTTP/1.1 200 OK``) and ``NOTIFY``
    advertisements; returns ``None`` for anything that is not one of those, so
    stray datagrams on the multicast group are ignored. The start line is
    dropped — callers key off headers (``location``, ``usn``, ``server``,
    ``nts``).
    """
    try:
        text = data.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - defensive: any decode failure → ignore
        return None
    lines = text.split("\r\n")
    if not lines or not lines[0]:
        return None
    first = lines[0].upper()
    if not (first.startswith("HTTP/") or first.startswith("NOTIFY")):
        return None
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    return headers


def extract_friendly_name(raw) -> Optional[str]:
    """Pull ``<friendlyName>`` out of a UPnP device-description XML.

    Namespace-agnostic (UPnP descriptions live in the ``urn:schemas-upnp-org``
    namespace, so a literal tag match would miss them); falls back to a regex if
    the XML does not parse. Returns ``None`` when no non-empty name is present.
    """
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        root = None
    if root is not None:
        for elem in root.iter():
            tag = elem.tag.rsplit("}", 1)[-1]   # strip {namespace}
            if tag == "friendlyName" and elem.text and elem.text.strip():
                return elem.text.strip()
    m = re.search(r"<friendlyName>\s*(.*?)\s*</friendlyName>",
                  text, re.IGNORECASE | re.DOTALL)
    if m:
        name = m.group(1).strip()
        if name:
            return name
    return None


class SsdpDiscovery(Discovery):
    """Find devices via an SSDP/UPnP M-SEARCH probe on every local interface.

    A multi-homed host (common on Windows) is probed on each IPv4 interface, so
    devices on secondary subnets are not missed. The probe is sent, replies and
    ``ssdp:alive`` notifications are collected for ``timeout`` seconds, and each
    unique ``LOCATION`` is fetched over HTTP for its ``<friendlyName>``. **Only
    devices that yield a friendly name are listed** — a responder without a
    readable description is dropped rather than shown as an opaque address.

    ``ifaddr`` is required for per-interface enumeration; a clear error is raised
    if it is missing (it ships in the ``gui`` extra).
    """

    def __init__(self, search_target: str = "ssdp:all", mx: int = 2,
                 ttl: int = 2, http_timeout: float = 2.0,
                 on_log: Optional[Callable[[str, str], None]] = None) -> None:
        self._st = search_target          # broadest target: every responder
        self._mx = mx                     # server answers within 0..MX seconds
        self._ttl = ttl                   # multicast hop limit
        self._http_timeout = http_timeout  # per-description HTTP fetch budget
        self._on_log = on_log             # optional (level, message) sink

    # -- public API -----------------------------------------------------------

    def search(self, timeout: float = 3.0) -> List[Device]:
        socks = self._open_sockets()
        if not socks:
            self._log("warning", "SSDP: no usable network interfaces found.")
            return []
        try:
            self._send_msearch(socks)
            responses = self._collect(socks, timeout)
        finally:
            for s in socks:
                try:
                    s.close()
                except OSError:
                    pass
        devices = self._assemble_devices(responses)
        self._log("info", "SSDP: {} device(s) with a description.".format(len(devices)))
        return devices

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _import_ifaddr():
        try:
            import ifaddr
        except ImportError as exc:  # hard requirement (gui extra)
            raise RuntimeError(
                "SSDP discovery needs the 'ifaddr' package "
                "(pip install ifaddr, or install the GUI extra: "
                "pip install -e .[gui])") from exc
        return ifaddr

    def _open_sockets(self) -> List[socket.socket]:
        """One multicast-sending UDP socket bound to each IPv4 interface."""
        ifaddr = self._import_ifaddr()
        socks: List[socket.socket] = []
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                addr = ip.ip
                # IPv6 addresses come back as a (addr, flowinfo, scope) tuple.
                if not isinstance(addr, str) or addr == "127.0.0.1":
                    continue
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                                      socket.IPPROTO_UDP)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self._ttl)
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                 socket.inet_aton(addr))
                    s.bind((addr, 0))
                    s.setblocking(False)
                except OSError as exc:
                    self._log("info", "SSDP: skipping interface {}: {}".format(addr, exc))
                    continue
                socks.append(s)
        return socks

    def _msearch_datagram(self) -> bytes:
        return (
            "M-SEARCH * HTTP/1.1\r\n"
            "HOST: {}:{}\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: {}\r\n"
            "ST: {}\r\n"
            "\r\n"
        ).format(SSDP_ADDR, SSDP_PORT, self._mx, self._st).encode("ascii")

    def _send_msearch(self, socks: List[socket.socket]) -> None:
        dgram = self._msearch_datagram()
        for s in socks:
            for _ in range(2):              # send twice; a UDP probe may be lost
                try:
                    s.sendto(dgram, (SSDP_ADDR, SSDP_PORT))
                except OSError as exc:
                    self._log("info", "SSDP: send failed on {}: {}".format(
                        s.getsockname()[0], exc))
                    break

    def _collect(self, socks: List[socket.socket],
                 timeout: float) -> List[Tuple[Dict[str, str], str]]:
        responses: List[Tuple[Dict[str, str], str]] = []
        end = time.monotonic() + timeout
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            try:
                rlist, _, _ = select.select(socks, [], [], remaining)
            except (OSError, ValueError):
                break
            for s in rlist:
                try:
                    data, addr = s.recvfrom(4096)
                except OSError:
                    continue
                headers = parse_ssdp_headers(data)
                if headers is not None:
                    responses.append((headers, addr[0]))
        return responses

    def _fetch_friendly_name(self, location: str) -> Optional[str]:
        import urllib.request          # lazy: only when a LOCATION is fetched
        req = urllib.request.Request(location, headers={"User-Agent": "pulseUDP-client"})
        with urllib.request.urlopen(req, timeout=self._http_timeout) as resp:
            raw = resp.read(65536)      # descriptions are small; cap the read
        return extract_friendly_name(raw)

    def _assemble_devices(
            self, responses: List[Tuple[Dict[str, str], str]]) -> List[Device]:
        """Dedupe responders by LOCATION, fetch each name, drop the nameless."""
        locations: Dict[str, Dict[str, str]] = {}
        for headers, _src_ip in responses:
            if headers.get("nts") == "ssdp:byebye":   # device leaving, not present
                continue
            loc = headers.get("location")
            if loc:
                locations.setdefault(loc, headers)

        devices: Dict[str, Device] = {}
        for loc in locations:
            try:
                name = self._fetch_friendly_name(loc)
            except Exception as exc:  # noqa: BLE001 - unreachable/HTTP/parse error
                self._log("info", "SSDP: no description from {}: {}".format(loc, exc))
                continue
            if not name:
                continue
            host = urlparse(loc).hostname
            if not host:
                continue
            devices.setdefault(host, Device(name=name, address=host))
        return sorted(devices.values(), key=lambda d: (d.name.lower(), d.address))

    def _log(self, level: str, message: str) -> None:
        if self._on_log is not None:
            self._on_log(level, message)
