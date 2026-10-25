"""Tests for the plot model and rolling ring buffer (no Qt)."""

import numpy as np

from pulseudp.client import LogEvent, UdpClient
from pulseudp.model import Column, PlotModel, RingBuffer
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


def _col(name="a", dtype=np.float64, scale=None):
    return Column(name, np.dtype(dtype), scale=scale)


def test_ring_stores_wire_values_from_descriptor_columns():
    import struct
    d = Descriptor.from_json(DESC)
    model = PlotModel(d)
    payload = (struct.pack("<I", 5000)        # Timestamp -> 5.0 s
               + struct.pack("<hh", 100, 0)   # VoltageA -> 1.0 V
               + struct.pack("<hh", 200, 0)   # VoltageB
               + struct.pack("<i", 7)         # Speed
               + struct.pack("<I", 0b101))    # Run + Fault
    rb = RingBuffer(model.columns(), window_n=100)
    rb.append(d.channels(d.decode(payload)))
    x, chans = rb.snapshot()
    assert x.dtype == np.int64 and list(x) == [0]
    assert set(chans) == set(model.channel_keys) == set(rb.trace_keys)
    assert np.allclose(chans["Timestamp"], [5.0])            # mult applied on read
    assert np.allclose(chans["VoltageA"], [1.0])
    assert chans["Speed"].dtype == np.int32 and list(chans["Speed"]) == [7]
    assert list(chans["Flags.Run"]) == [1] and list(chans["Flags.Fault"]) == [1]


def test_ring_over_enabled_subset_has_no_gaps():
    d = Descriptor.from_json(DESC)
    model = PlotModel(d)
    enabled = [True, True, False, False, False]              # Timestamp, VoltageA
    cols = model.columns(enabled)
    assert [c.name for c in cols] == ["Timestamp", "VoltageA"]
    rb = RingBuffer(cols, window_n=100)
    rb.append({"Timestamp": np.array([1, 2], np.uint32),
               "VoltageA": np.array([10, 20], np.int16)})
    x, chans = rb.snapshot()
    assert list(x) == [0, 1]
    assert set(chans) == {"Timestamp", "VoltageA"}           # disabled ones not stored
    assert np.allclose(chans["VoltageA"], [0.1, 0.2])


def test_append_drops_batch_with_stale_layout():
    rb = RingBuffer([_col("a"), _col("b")], window_n=100)
    rb.append({"a": np.array([1.0])})                       # missing "b"
    rb.append({"a": np.array([1.0]), "b": np.array([2.0]), "c": np.array([3.0])})
    assert rb.latest_index() is None


def test_native_types_round_trip_exactly():
    cols = [_col("i8", np.int8), _col("i32", np.int32), _col("u32", np.uint32),
            _col("i64", np.int64), _col("u64", np.uint64), _col("f8", np.float64)]
    vals = {"i8": np.array([-128, 127], np.int8),
            "i32": np.array([-2**31, 2**31 - 1], np.int32),
            "u32": np.array([2**24 + 1, 2**32 - 1], np.uint32),
            "i64": np.array([2**53 + 1, -2**63], np.int64),
            "u64": np.array([2**64 - 1, 2**53 + 1], np.uint64),
            "f8": np.array([0.1 + 1e-15, -1e300], np.float64)}
    rb = RingBuffer(cols, window_n=100)
    rb.append(vals)
    _, chans = rb.snapshot()
    for k, v in vals.items():
        assert chans[k].dtype == v.dtype
        assert np.array_equal(chans[k], v), k


def test_footprint_bytes_follows_dtypes():
    one_byte = RingBuffer.footprint_bytes([_col("a", np.int8)], 1_000_000)
    eight = RingBuffer.footprint_bytes([_col("a", np.float64)], 1_000_000)
    assert eight == 8 * one_byte
    small = RingBuffer.footprint_bytes([_col("a", np.float32)] * 8, 1000)
    big = RingBuffer.footprint_bytes([_col("a", np.float32)] * 8, 1_000_000)
    assert 0 < small < big
    # Pyramid overhead is bounded (~1/7 of raw); total stays well under 2x raw.
    assert big < 2 * 8 * 1_000_000 * 4


def test_trace_format_counters_vs_measurements():
    model = PlotModel(Descriptor.from_json(DESC))
    assert model.trace_format("Speed") == "%d"             # int32, no mult: a counter
    assert model.trace_format("VoltageA") == "%.5g"        # scaled measurement
    assert model.trace_format("Flags.Run") == "%d"


def test_ring_buffer_trims_to_window():
    rb = RingBuffer([_col()], window_n=3)
    for i in range(10):
        rb.append({"a": np.array([float(i)])})       # one sample per append, X = 0..9
    x, chans = rb.snapshot()
    # latest index is 9; window 3 keeps indices >= 6 (chunk-granular).
    assert x[-1] == 9
    assert x[0] >= 6
    assert rb.latest_index() == 9


def test_ring_buffer_set_window_shrinks():
    rb = RingBuffer([_col()], window_n=100)
    for i in range(20):
        rb.append({"a": np.array([float(i)])})       # X = 0..19
    rb.set_window(3)
    x, _ = rb.snapshot()
    assert x[0] >= 16 and x[-1] == 19


def test_view_exact_when_zoomed_in():
    # A handful of pixels per sample -> the view returns raw samples, not buckets.
    rb = RingBuffer([_col("a", np.int32)], window_n=10000)
    rb.append({"a": np.arange(1000, dtype=np.int32)})
    v = rb.view(100.0, 149.0, max_points=2000)   # < 1 sample/pixel -> exact
    assert v is not None and v.exact
    assert v.x.dtype == np.int64
    assert np.array_equal(v.x, np.arange(100, 150))
    assert v.ymin["a"].dtype == np.int32
    assert np.array_equal(v.ymin["a"], np.arange(100, 150))
    assert v.ymin["a"] is v.ymax["a"]            # envelope collapses to the samples


def test_view_decimates_to_native_min_max_buckets():
    # Ramp y == index: bucket b of stride s has min b*s, max b*s + s - 1.
    rb = RingBuffer([_col("a", np.int16)], window_n=10000)
    rb.append({"a": np.arange(4096, dtype=np.int16)})
    v = rb.view(0.0, 4095.0, max_points=16)      # 256 samples/pixel -> level 2 (stride 64)
    assert v is not None and not v.exact
    s = 64
    assert v.x.size == 4096 // s
    assert v.ymin["a"].dtype == np.int16 and v.ymax["a"].dtype == np.int16
    assert np.array_equal(v.ymin["a"], np.arange(v.x.size) * s)
    assert np.array_equal(v.ymax["a"], np.arange(v.x.size) * s + (s - 1))
    assert v.x.dtype == np.int64
    assert np.array_equal(v.x, np.arange(v.x.size) * s + s // 2)   # integer bucket centres


def test_view_negative_mult_swaps_min_max():
    rb = RingBuffer([_col("a", np.int16, scale=-0.5)], window_n=10000)
    rb.append({"a": np.arange(4096, dtype=np.int16)})
    v = rb.view(0.0, 4095.0, max_points=16)
    assert np.all(v.ymin["a"] <= v.ymax["a"])
    assert v.ymin["a"][0] == -0.5 * 63 and v.ymax["a"][0] == 0.0


def test_view_bitfield_envelope_is_and_or():
    col = Column("F", np.dtype(np.uint32), is_bitfield=True,
                 bits=[("F.on", 0), ("F.blip", 1), ("F.off", 2)])
    rb = RingBuffer([col], window_n=100000)
    y = np.ones(8192, dtype=np.uint32)           # bit 0 always set, bit 2 never
    y[5000] |= 0b10                              # a single-sample blip on bit 1
    rb.append({"F": y})
    v = rb.view(0.0, 8191.0, max_points=8)
    assert not v.exact
    assert np.all(v.ymin["F.on"] == 1) and np.all(v.ymax["F.on"] == 1)
    assert np.all(v.ymin["F.off"] == 0) and np.all(v.ymax["F.off"] == 0)
    assert v.ymax["F.blip"].sum() == 1 and v.ymin["F.blip"].sum() == 0


def test_view_envelope_preserves_spikes():
    # A lone spike must survive decimation via the per-bucket max.
    rb = RingBuffer([_col()], window_n=100000)
    y = np.zeros(8192, dtype=float)
    y[5000] = 999.0
    rb.append({"a": y})
    v = rb.view(0.0, 8191.0, max_points=8)       # heavy decimation
    assert v is not None and not v.exact
    assert v.ymax["a"].max() == 999.0            # the spike is still there


def test_set_window_keeps_absolute_indices_for_view():
    rb = RingBuffer([_col("a", np.int64)], window_n=100000)
    rb.append({"a": np.arange(50000, dtype=np.int64)})
    rb.set_window(1000)                          # shrink; keep the recent tail
    assert rb.latest_index() == 49999
    x, chans = rb.snapshot()
    assert x[-1] == 49999 and x[0] >= 49000      # recent window, original indices
    assert np.array_equal(chans["a"], x)         # values still equal their index
    v = rb.view(float(x[0]), 49999.0, max_points=4)   # rebuilt pyramid is native too
    assert not v.exact and v.ymin["a"].dtype == np.int64


def test_sequence_gap_detection_logs():
    events = []
    c = UdpClient("127.0.0.1", on_log=lambda ev: events.append(ev))
    c._check_sequence(10)        # first, no log
    c._check_sequence(11)        # in order, no log
    c._check_sequence(15)        # gap of 4
    cats = [e.category for e in events]
    assert cats == ["seq_gap"]
    assert "lost" in events[0].message
