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
    # The "s"-units field shares a plot like any other channel.
    assert groups["s"].kind == "numeric" and len(groups["s"].traces) == 1
    assert model.channel_keys == ["Timestamp", "VoltageA", "VoltageB", "Speed",
                                  "Flags.Run", "Flags.Fault"]


def test_extract_returns_sample_count_and_all_channels():
    import struct
    d = Descriptor.from_json(DESC)
    model = PlotModel(d)
    payload = (struct.pack("<I", 5000)        # Timestamp -> 5.0
               + struct.pack("<hh", 100, 0)   # VoltageA
               + struct.pack("<hh", 200, 0)   # VoltageB
               + struct.pack("<i", 7)         # Speed
               + struct.pack("<I", 0b101))    # Run + Fault
    n, flat = model.extract(d.decode(payload))
    assert n == 1
    assert np.allclose(flat["Timestamp"], [5.0])
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
    n, flat = model.flatten(channels)
    assert n == 2
    assert set(flat) == {"Timestamp", "VoltageA"}
    rb = RingBuffer(model.channel_keys)
    rb.append(n, flat)
    x, chans = rb.snapshot()
    assert np.array_equal(x, [0.0, 1.0])              # synthetic per-sample index
    assert np.allclose(chans["VoltageA"], [0.1, 0.2])
    assert np.all(np.isnan(chans["VoltageB"]))        # disabled numeric -> NaN
    assert np.all(np.isnan(chans["Flags.Run"]))       # disabled bitfield -> NaN


def test_flatten_empty_channels_returns_zero():
    d = Descriptor.from_json(DESC)
    model = PlotModel(d)
    n, flat = model.flatten({})
    assert n == 0 and flat == {}


def test_ring_buffer_trims_to_window():
    rb = RingBuffer(["a"], window_n=3)
    for i in range(10):
        rb.append(1, {"a": np.array([float(i)])})   # one sample per append, X = 0..9
    x, chans = rb.snapshot()
    # latest index is 9; window 3 keeps indices >= 6 (chunk-granular).
    assert x[-1] == 9
    assert x[0] >= 6
    assert rb.latest_index() == 9


def test_ring_buffer_set_window_shrinks():
    rb = RingBuffer(["a"], window_n=100)
    for i in range(20):
        rb.append(1, {"a": np.array([float(i)])})   # X = 0..19
    rb.set_window(3)
    x, _ = rb.snapshot()
    assert x[0] >= 16 and x[-1] == 19


def test_sequence_gap_detection_logs():
    events = []
    c = UdpClient("127.0.0.1", on_log=lambda ev: events.append(ev))
    c._check_sequence(10)        # first, no log
    c._check_sequence(11)        # in order, no log
    c._check_sequence(15)        # gap of 4
    cats = [e.category for e in events]
    assert cats == ["seq_gap"]
    assert "lost" in events[0].message
