"""Plot grouping and rolling history (Qt-free); design in docs/gui-design.md."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field as _dc_field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .protocol import Descriptor


@dataclass
class Trace:
    key: str          # unique trace key (RingBuffer view/snapshot)
    label: str
    is_bit: bool = False


@dataclass
class Column:
    """One stored wire field: native dtype, ``mult`` applied on read, bits expanded on read."""

    name: str
    dtype: np.dtype
    scale: Optional[float] = None
    is_bitfield: bool = False
    bits: List[Tuple[str, int]] = _dc_field(default_factory=list)   # (trace key, bit)


def _bit_key(field_name: str, bit_name: str) -> str:
    return "{}.{}".format(field_name, bit_name)


@dataclass
class PlotGroup:
    title: str
    units: Optional[str]
    kind: str                       # "numeric" | "bitfield"
    traces: List[Trace] = _dc_field(default_factory=list)


class PlotModel:
    """Maps a descriptor to plot groups (rules: docs/gui-design.md, Grouping)."""

    def __init__(self, descriptor: Descriptor) -> None:
        self.descriptor = descriptor
        self.groups: List[PlotGroup] = []
        self._channel_keys: List[str] = []

        self._formats: Dict[str, str] = {}
        for col in self.columns():
            if col.is_bitfield:
                self._formats.update((key, "%d") for key, _ in col.bits)
            elif col.dtype.kind in "iu" and col.scale is None:
                self._formats[col.name] = "%d"

        units_group: Dict[str, PlotGroup] = {}
        for f in descriptor.plot_fields:
            if f.is_bitfield:
                grp = PlotGroup(title=f.name, units=None, kind="bitfield")
                for bit, name in enumerate(f.bits or []):
                    if name == "Reserved":
                        continue
                    key = _bit_key(f.name, name)
                    grp.traces.append(Trace(key=key, label=name, is_bit=True))
                    self._channel_keys.append(key)
                self.groups.append(grp)
            elif f.units:
                grp = units_group.get(f.units)
                if grp is None:
                    grp = PlotGroup(title=f.units, units=f.units, kind="numeric")
                    units_group[f.units] = grp
                    self.groups.append(grp)
                grp.traces.append(Trace(key=f.name, label=f.name))
                self._channel_keys.append(f.name)
            else:
                grp = PlotGroup(title=f.name, units=None, kind="numeric")
                grp.traces.append(Trace(key=f.name, label=f.name))
                self.groups.append(grp)
                self._channel_keys.append(f.name)

    @property
    def channel_keys(self) -> List[str]:
        """All trace keys, in plot order."""
        return list(self._channel_keys)

    def columns(self, enabled: Optional[List[bool]] = None) -> List[Column]:
        """Storage columns for the enabled fields (all when ``enabled`` is None)."""
        cols: List[Column] = []
        for i, f in enumerate(self.descriptor.fields):
            if enabled is not None and not (i < len(enabled) and enabled[i]):
                continue
            if f.is_bitfield:
                bits = [(_bit_key(f.name, name), b)
                        for b, name in enumerate(f.bits or []) if name != "Reserved"]
                cols.append(Column(f.name, f.dtype, is_bitfield=True, bits=bits))
            else:
                cols.append(Column(f.name, f.dtype, scale=f.scale))
        return cols

    def trace_format(self, key: str) -> str:
        """Export format: ``%d`` for bits and unscaled integers (counters), else 5 digits."""
        return self._formats.get(key, "%.5g")


# Pyramid factor and height cap; rationale in docs/gui-design.md.
PYRAMID_FACTOR = 8
MAX_LEVELS = 12


@dataclass
class DecimatedView:
    """One redraw's slice: raw samples if ``exact``, else per-bucket min/max at bucket centres."""

    exact: bool
    x: np.ndarray
    ymin: Dict[str, np.ndarray]
    ymax: Dict[str, np.ndarray]


def _read(arr: np.ndarray, lo: int, hi: int) -> np.ndarray:
    """Copy samples/buckets [lo, hi) out of a 1-D ring."""
    cap = arr.shape[0]
    start = lo % cap
    end = start + (hi - lo)
    if end <= cap:
        return arr[start:end].copy()
    return np.concatenate((arr[start:], arr[:end - cap]))


def _write(arr: np.ndarray, lo: int, hi: int, data: np.ndarray) -> None:
    """Write ``data`` into samples/buckets [lo, hi) of a 1-D ring."""
    cap = arr.shape[0]
    start = lo % cap
    end = start + (hi - lo)
    if end <= cap:
        arr[start:end] = data
        return
    k = cap - start
    arr[start:] = data[:k]
    arr[:end - cap] = data[k:]


class RingBuffer:
    """Thread-safe native-typed ring + min/max pyramid; docs/gui-design.md (Rendering pipeline)."""

    def __init__(self, columns: List[Column], window_n: int) -> None:
        self._cols = list(columns)
        self._names = [c.name for c in self._cols]
        self._lock = threading.Lock()
        self._allocate(int(window_n))

    @staticmethod
    def _plan(window_n: int) -> Tuple[int, int]:
        """Return (capacity, n_levels) for a requested window length."""
        w = max(1, int(window_n))
        # Tallest pyramid whose coarsest bucket still spans <= the window.
        levels = max(1, min(MAX_LEVELS, int(math.floor(math.log(w, PYRAMID_FACTOR)))))
        coarsest = PYRAMID_FACTOR ** levels
        capacity = ((w + coarsest - 1) // coarsest) * coarsest
        return capacity, levels

    @staticmethod
    def footprint_bytes(columns: List[Column], window_n: int) -> int:
        """Bytes this buffer would allocate for ``window_n`` (raw + pyramid)."""
        cap, levels = RingBuffer._plan(window_n)
        per_sample = sum(c.dtype.itemsize for c in columns)
        total = cap * per_sample
        for L in range(1, levels + 1):
            total += 2 * (cap // (PYRAMID_FACTOR ** L)) * per_sample   # min + max per level
        return total

    def _allocate(self, window_n: int) -> None:
        """(Re)allocate the raw ring and pyramid for a window length. Resets data."""
        cap, levels = self._plan(window_n)
        self._window_n = int(window_n)
        self._cap = cap
        self._levels = levels
        self._raw = [np.zeros(cap, dtype=c.dtype) for c in self._cols]
        # Pyramid level rings, indexed 1..levels (index 0 is the raw ring).
        self._mn: List[List[np.ndarray]] = [self._raw]
        self._mx: List[List[np.ndarray]] = [self._raw]
        for L in range(1, levels + 1):
            cap_l = cap // (PYRAMID_FACTOR ** L)
            self._mn.append([np.zeros(cap_l, dtype=c.dtype) for c in self._cols])
            self._mx.append([np.zeros(cap_l, dtype=c.dtype) for c in self._cols])
        # Next bucket index to build at each level (== count of built buckets).
        self._built = [0] * (levels + 1)
        self._n_total = 0          # samples seen since clear() == next X index

    @property
    def window_n(self) -> int:
        return self._window_n

    @property
    def capacity(self) -> int:
        return self._cap

    @property
    def trace_keys(self) -> List[str]:
        keys: List[str] = []
        for c in self._cols:
            if c.is_bitfield:
                keys.extend(k for k, _ in c.bits)
            else:
                keys.append(c.name)
        return keys

    def _fill_level(self, L: int, b_lo: int, b_hi: int) -> None:
        """Compute buckets [b_lo, b_hi) of level L by reducing level L-1 by F."""
        F = PYRAMID_FACTOR
        count = b_hi - b_lo
        for c, col in enumerate(self._cols):
            child_mn = _read(self._mn[L - 1][c], b_lo * F, b_hi * F).reshape(count, F)
            child_mx = _read(self._mx[L - 1][c], b_lo * F, b_hi * F).reshape(count, F)
            if col.is_bitfield:            # per-bit envelope: min = AND, max = OR
                mn = np.bitwise_and.reduce(child_mn, axis=1)
                mx = np.bitwise_or.reduce(child_mx, axis=1)
            else:                          # fmin/fmax skip a NaN sent on the wire
                mn = np.fmin.reduce(child_mn, axis=1)
                mx = np.fmax.reduce(child_mx, axis=1)
            _write(self._mn[L][c], b_lo, b_hi, mn)
            _write(self._mx[L][c], b_lo, b_hi, mx)

    def _build_incremental(self) -> None:
        for L in range(1, self._levels + 1):
            avail = self._n_total // (PYRAMID_FACTOR ** L)
            if avail > self._built[L]:
                self._fill_level(L, self._built[L], avail)
                self._built[L] = avail

    def _rebuild(self, lo: int, hi: int) -> None:
        """Rebuild the whole pyramid over resident samples [lo, hi) from scratch."""
        child_first = lo                       # first resident index at level L-1
        for L in range(1, self._levels + 1):
            F = PYRAMID_FACTOR
            b_lo = -(-child_first // F)         # ceil: first bucket fully inside [lo,hi)
            b_hi = hi // (F ** L)
            if b_hi > b_lo:
                self._fill_level(L, b_lo, b_hi)
            self._built[L] = b_hi
            child_first = b_lo

    def set_window(self, window_n: int) -> None:
        """Change the window length, keeping the latest samples at their original indices."""
        with self._lock:
            new_w = int(window_n)
            new_cap, _ = self._plan(new_w)
            hi = self._n_total
            resident = min(hi, self._cap)
            keep = min(resident, new_cap)
            lo = hi - keep
            tails = [_read(r, lo, hi) for r in self._raw] if keep else []   # OLD ring
            self._allocate(new_w)                                          # resets to empty
            if keep:
                for r, tail in zip(self._raw, tails):
                    _write(r, lo, hi, tail)
                self._n_total = hi
                self._rebuild(lo, hi)

    def clear(self) -> None:
        with self._lock:
            self._n_total = 0
            self._built = [0] * (self._levels + 1)

    def append(self, channels: Dict[str, np.ndarray]) -> None:
        """Append one batch of raw columns; a batch whose fields differ from the columns
        (decoded under a stale channel layout) is dropped."""
        if not self._names or set(channels) != set(self._names):
            return
        n = len(channels[self._names[0]])
        if n <= 0:
            return
        with self._lock:
            a = self._n_total
            for r, name in zip(self._raw, self._names):
                _write(r, a, a + n, channels[name])
            self._n_total = a + n
            self._build_incremental()

    def latest_index(self) -> Optional[int]:
        with self._lock:
            return self._n_total - 1 if self._n_total else None

    def _resident_lo_locked(self) -> int:
        return max(0, self._n_total - self._window_n)

    def _traces(self, arrays: List[np.ndarray]) -> Dict[str, np.ndarray]:
        """Per-column arrays -> per-trace arrays (mult applied, bits split)."""
        out: Dict[str, np.ndarray] = {}
        for col, a in zip(self._cols, arrays):
            if col.is_bitfield:
                for key, bit in col.bits:
                    out[key] = ((a >> bit) & 1).astype(np.uint8)
            else:
                out[col.name] = a if col.scale is None else a * col.scale
        return out

    def snapshot(self) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
        """Copy of the full window (export, tests); the GUI redraw uses :meth:`view`."""
        with self._lock:
            hi = self._n_total
            lo = self._resident_lo_locked()
            x = np.arange(lo, hi, dtype=np.int64)
            return x, self._traces([_read(r, lo, hi) for r in self._raw])

    def view(self, x0: float, x1: float, max_points: int) -> Optional[DecimatedView]:
        """Decimated slice of [x0, x1] for ~``max_points`` pixels; None if nothing resident."""
        with self._lock:
            if self._n_total == 0:
                return None
            hi_idx = self._n_total - 1
            lo_idx = self._resident_lo_locked()
            i0 = max(lo_idx, int(math.floor(x0)))
            i1 = min(hi_idx, int(math.ceil(x1)))
            if i1 < i0:
                return None
            span = i1 - i0 + 1
            mp = max(1, int(max_points))
            spp = span / mp                                  # samples per pixel
            L = 0 if spp < 1 else int(math.floor(math.log(spp, PYRAMID_FACTOR)))
            L = max(0, min(L, self._levels))
            while L >= 1 and self._built[L] == 0:
                L -= 1

            if L >= 1:
                s = PYRAMID_FACTOR ** L
                b0 = max(lo_idx // s, i0 // s)
                b1 = min((self._built[L] - 1), i1 // s)
                if b1 >= b0:
                    ymin = self._traces([_read(a, b0, b1 + 1) for a in self._mn[L]])
                    ymax = self._traces([_read(a, b0, b1 + 1) for a in self._mx[L]])
                    for col in self._cols:          # a negative mult flips the envelope
                        if col.scale is not None and col.scale < 0:
                            ymin[col.name], ymax[col.name] = ymax[col.name], ymin[col.name]
                    # s is a power of 8, so the bucket centre is an exact integer.
                    x = np.arange(b0, b1 + 1, dtype=np.int64) * s + s // 2
                    return DecimatedView(exact=False, x=x, ymin=ymin, ymax=ymax)

            # Exact: raw samples (envelope min == max).
            cols = self._traces([_read(r, i0, i1 + 1) for r in self._raw])
            x = np.arange(i0, i1 + 1, dtype=np.int64)
            return DecimatedView(exact=True, x=x, ymin=cols, ymax=cols)
