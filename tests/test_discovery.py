"""Tests for SSDP discovery (no sockets, no network).

The socket/multicast plumbing of :class:`SsdpDiscovery` is exercised manually;
here we pin the pure, decision-making pieces: SSDP header parsing, friendly-name
extraction from a UPnP description, and the response -> device assembly (dedupe
by responder, drop the nameless), with the HTTP fetch stubbed out.
"""

from pulseudp.discovery import (Device, NullDiscovery, SsdpDiscovery,
                                extract_friendly_name, parse_ssdp_headers)

# A realistic M-SEARCH reply from the reference server (RFC headers, CRLF lines).
_OK_REPLY = (
    "HTTP/1.1 200 OK\r\n"
    "CACHE-CONTROL: max-age=1800\r\n"
    "ST: upnp:rootdevice\r\n"
    "USN: uuid:abcd::upnp:rootdevice\r\n"
    "SERVER: lwIP/1.4.1 UPnP/2.0 8SMC5-USB/4.7.7\r\n"
    "LOCATION: http://192.168.0.42:5050/upnp_description.xml\r\n"
    "\r\n"
).encode("utf-8")

_NOTIFY_ALIVE = (
    "NOTIFY * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    "NTS: ssdp:alive\r\n"
    "NT: upnp:rootdevice\r\n"
    "USN: uuid:abcd::upnp:rootdevice\r\n"
    "LOCATION: http://192.168.0.42:5050/upnp_description.xml\r\n"
    "\r\n"
).encode("utf-8")

_NOTIFY_BYEBYE = (
    "NOTIFY * HTTP/1.1\r\n"
    "NTS: ssdp:byebye\r\n"
    "USN: uuid:dead::upnp:rootdevice\r\n"
    "LOCATION: http://192.168.0.9:5050/upnp_description.xml\r\n"
    "\r\n"
).encode("utf-8")

# Minimal UPnP device description (namespaced, as real devices send it).
_DESC_XML = (
    '<?xml version="1.0"?>'
    '<root xmlns="urn:schemas-upnp-org:device-1-0">'
    "<device><friendlyName>Bench rig 3</friendlyName>"
    "<manufacturer>ACME</manufacturer></device></root>"
)


# -- header parsing -----------------------------------------------------------

def test_parse_msearch_reply_lowercases_headers():
    headers = parse_ssdp_headers(_OK_REPLY)
    assert headers is not None
    assert headers["location"] == "http://192.168.0.42:5050/upnp_description.xml"
    assert headers["usn"] == "uuid:abcd::upnp:rootdevice"
    assert headers["server"].endswith("8SMC5-USB/4.7.7")


def test_parse_notify_alive_is_accepted():
    headers = parse_ssdp_headers(_NOTIFY_ALIVE)
    assert headers is not None
    assert headers["nts"] == "ssdp:alive"
    assert "location" in headers


def test_parse_rejects_non_ssdp_datagram():
    assert parse_ssdp_headers(b"hello world\r\n\r\n") is None
    assert parse_ssdp_headers(b"") is None


# -- friendly-name extraction -------------------------------------------------

def test_extract_friendly_name_namespaced_xml():
    assert extract_friendly_name(_DESC_XML) == "Bench rig 3"
    assert extract_friendly_name(_DESC_XML.encode("utf-8")) == "Bench rig 3"


def test_extract_friendly_name_regex_fallback_on_bad_xml():
    # Unclosed <root> won't parse as XML; the regex still recovers the name.
    broken = "<root><device><friendlyName> Probe A </friendlyName></device>"
    assert extract_friendly_name(broken) == "Probe A"


def test_extract_friendly_name_absent_returns_none():
    assert extract_friendly_name("<root><device/></root>") is None
    assert extract_friendly_name("<root><friendlyName>   </friendlyName></root>") is None


# -- response -> device assembly (HTTP fetch stubbed) -------------------------

def _disco_with_names(name_by_location):
    """An SsdpDiscovery whose description fetch is replaced by a lookup table."""
    disco = SsdpDiscovery()
    disco._fetch_friendly_name = lambda loc: name_by_location.get(loc)
    return disco


def test_assemble_dedupes_responder_and_keeps_named():
    disco = _disco_with_names(
        {"http://192.168.0.42:5050/upnp_description.xml": "Bench rig 3"})
    headers = parse_ssdp_headers(_OK_REPLY)
    # Same device answers both the M-SEARCH and a NOTIFY: one Device, not two.
    devices = disco._assemble_devices([
        (headers, "192.168.0.42"),
        (parse_ssdp_headers(_NOTIFY_ALIVE), "192.168.0.42"),
    ])
    assert devices == [Device(name="Bench rig 3", address="192.168.0.42")]


def test_assemble_drops_responder_without_friendly_name():
    # Fetch yields no name (no description / no <friendlyName>): not listed.
    disco = _disco_with_names({})
    devices = disco._assemble_devices([(parse_ssdp_headers(_OK_REPLY), "192.168.0.42")])
    assert devices == []


def test_assemble_skips_byebye():
    disco = _disco_with_names(
        {"http://192.168.0.9:5050/upnp_description.xml": "Leaving device"})
    devices = disco._assemble_devices([(parse_ssdp_headers(_NOTIFY_BYEBYE), "192.168.0.9")])
    assert devices == []        # a departing device is never offered


def test_assemble_sorts_by_name():
    disco = _disco_with_names({
        "http://10.0.0.2:5050/x.xml": "Zeta",
        "http://10.0.0.1:5050/x.xml": "Alpha",
    })
    headers2 = dict(parse_ssdp_headers(_OK_REPLY))
    headers2["location"] = "http://10.0.0.2:5050/x.xml"
    headers1 = dict(parse_ssdp_headers(_OK_REPLY))
    headers1["location"] = "http://10.0.0.1:5050/x.xml"
    devices = disco._assemble_devices([(headers2, "10.0.0.2"), (headers1, "10.0.0.1")])
    assert [d.name for d in devices] == ["Alpha", "Zeta"]


# -- NullDiscovery ------------------------------------------------------------

def test_null_discovery_finds_nothing():
    assert NullDiscovery().search() == []
