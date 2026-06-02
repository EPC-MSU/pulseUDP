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
    rb = RingBuffer(model.channel_keys, window_n=100)
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


def test_view_exact_when_zoomed_in():
    # A handful of pixels per sample -> the view returns raw samples, not buckets.
    rb = RingBuffer(["a"], window_n=10000)
    rb.append(1000, {"a": np.arange(1000, dtype=float)})
    v = rb.view(100.0, 149.0, max_points=2000)   # < 1 sample/pixel -> exact
    assert v is not None and v.exact
    assert np.array_equal(v.x, np.arange(100, 150, dtype=float))
    assert np.allclose(v.ymin["a"], np.arange(100, 150))
    assert v.ymin["a"] is v.ymax["a"]            # envelope collapses to the samples


def test_view_decimates_to_min_max_buckets():
    # Many samples per pixel -> envelope buckets. For a ramp y == index, bucket b
    # at level-L stride s spans [b*s, (b+1)*s): min == b*s, max == b*s + s - 1.
    rb = RingBuffer(["a"], window_n=10000)
    rb.append(4096, {"a": np.arange(4096, dtype=float)})
    v = rb.view(0.0, 4095.0, max_points=16)      # 256 samples/pixel -> level 2 (stride 64)
    assert v is not None and not v.exact
    s = 64
    assert v.x.size == 4096 // s
    assert np.allclose(v.ymin["a"], np.arange(v.x.size) * s)
    assert np.allclose(v.ymax["a"], np.arange(v.x.size) * s + (s - 1))
    assert np.allclose(v.x, (np.arange(v.x.size) + 0.5) * s)   # bucket centres


def test_view_envelope_preserves_spikes():
    # A lone spike must survive decimation via the per-bucket max.
    rb = RingBuffer(["a"], window_n=100000)
    y = np.zeros(8192, dtype=float)
    y[5000] = 999.0
    rb.append(8192, {"a": y})
    v = rb.view(0.0, 8191.0, max_points=8)       # heavy decimation
    assert v is not None and not v.exact
    assert v.ymax["a"].max() == 999.0            # the spike is still there


def test_set_window_keeps_absolute_indices_for_view():
    rb = RingBuffer(["a"], window_n=100000)
    rb.append(50000, {"a": np.arange(50000, dtype=float)})
    rb.set_window(1000)                          # shrink; keep the recent tail
    assert rb.latest_index() == 49999
    x, chans = rb.snapshot()
    assert x[-1] == 49999 and x[0] >= 49000      # recent window, original indices
    assert np.allclose(chans["a"], x)            # values still equal their index


def test_footprint_bytes_grows_with_window():
    small = RingBuffer.footprint_bytes(8, 1000)
    big = RingBuffer.footprint_bytes(8, 1_000_000)
    assert 0 < small < big
    # Pyramid overhead is bounded (~1/7 of raw); total stays well under 2x raw.
    raw = 8 * 1_000_000 * 4
    assert big < 2 * raw


def test_sequence_gap_detection_logs():
    events = []
    c = UdpClient("127.0.0.1", on_log=lambda ev: events.append(ev))
    c._check_sequence(10)        # first, no log
    c._check_sequence(11)        # in order, no log
    c._check_sequence(15)        # gap of 4
    cats = [e.category for e in events]
    assert cats == ["seq_gap"]
    assert "lost" in events[0].message
