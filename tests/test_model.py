"""Tests for the plot model and rolling ring buffer (no Qt)."""

import numpy as np

from pulseudp.client import LogEvent, UdpClient
from pulseudp.model import PlotModel, RingBuffer
from pulseudp.protocol import Descriptor


DESC = {
    "version": "1.0.0",
    "fields": [
        {"name": "Timestamp", "type": "uint32", "mult": 0.001, "units": "s"},
        {"name": "VoltageA", "type": "int16", "mult": 0.01, "units": "V"},
        {"name": "VoltageB", "type": "int16", "mult": 0.01, "units": "V"},
        {"name": "Speed", "type": "int32"},                       # unitless
        {"name": "Flags", "type": "bitfield",
         "bits": ["Run", "Reserved", "Fault"]},
    ],
}


def test_grouping_rules():
    model = PlotModel(Descriptor.from_json(DESC))
    groups = {g.title: g for g in model.groups}
    # Same-units share a plot; unitless gets its own; bitfield its own.
    assert groups["V"].kind == "numeric" and len(groups["V"].traces) == 2
    assert groups["Speed"].kind == "numeric" and len(groups["Speed"].traces) == 1
    assert groups["Flags"].kind == "bitfield"
    # Reserved bit excluded from traces.
    assert {t.label for t in groups["Flags"].traces} == {"Run", "Fault"}
    assert model.channel_keys == ["VoltageA", "VoltageB", "Speed",
                                  "Flags.Run", "Flags.Fault"]


def test_extract_returns_time_and_bit_traces():
    import struct
    d = Descriptor.from_json(DESC)
    model = PlotModel(d)
    payload = (struct.pack("<I", 5000)        # Timestamp -> 5.0 s
               + struct.pack("<hh", 100, 0)   # VoltageA
               + struct.pack("<hh", 200, 0)   # VoltageB
               + struct.pack("<i", 7)         # Speed
               + struct.pack("<I", 0b101))    # Run + Fault
    t, flat = model.extract(d.decode(payload))
    assert np.allclose(t, [5.0])
    assert np.allclose(flat["VoltageA"], [1.0])
    assert np.array_equal(flat["Flags.Run"], [1.0])
    assert np.array_equal(flat["Flags.Fault"], [1.0])


def test_flatten_subset_omits_disabled_then_ring_fills_nan():
    # v2.0: only some channels enabled -> flatten yields only those; the buffer
    # fills the disabled trace keys with NaN so they draw as gaps.
    d = Descriptor.from_json(DESC)
    model = PlotModel(d)
    channels = {"Timestamp": np.array([1.0, 2.0]),
                "VoltageA": np.array([0.1, 0.2])}     # VoltageB/Speed/Flags off
    t, flat = model.flatten(channels)
    assert t is not None
    assert np.allclose(t, [1.0, 2.0])
    assert set(flat) == {"VoltageA"}
    rb = RingBuffer(model.channel_keys)
    rb.append(t, flat)
    _, chans = rb.snapshot()
    assert np.allclose(chans["VoltageA"], [0.1, 0.2])
    assert np.all(np.isnan(chans["VoltageB"]))        # disabled numeric -> NaN
    assert np.all(np.isnan(chans["Flags.Run"]))       # disabled bitfield -> NaN


def test_flatten_without_timebase_returns_none():
    d = Descriptor.from_json(DESC)
    model = PlotModel(d)
    t, flat = model.flatten({"VoltageA": np.array([1.0])})
    assert t is None and flat == {}


def test_ring_buffer_trims_to_window():
    rb = RingBuffer(["a"], window_s=1.0)
    for i in range(10):
        t = np.array([i * 0.5], dtype=np.float64)   # 0.0, 0.5, ... 4.5
        rb.append(t, {"a": np.array([float(i)])})
    t, chans = rb.snapshot()
    # latest is 4.5; window 1.0 keeps samples with t >= 3.5 (chunk-granular).
    assert t[-1] == 4.5
    assert t[0] >= 3.0
    assert rb.latest_time() == 4.5


def test_ring_buffer_set_window_shrinks():
    rb = RingBuffer(["a"], window_s=10.0)
    for i in range(20):
        rb.append(np.array([float(i)]), {"a": np.array([float(i)])})
    rb.set_window(2.0)
    t, _ = rb.snapshot()
    assert t[0] >= 16.0 and t[-1] == 19.0


def test_sequence_gap_detection_logs():
    events = []
    c = UdpClient("127.0.0.1", on_log=lambda ev: events.append(ev))
    c._check_sequence(10)        # first, no log
    c._check_sequence(11)        # in order, no log
    c._check_sequence(15)        # gap of 4
    cats = [e.category for e in events]
    assert cats == ["seq_gap"]
    assert "lost" in events[0].message
