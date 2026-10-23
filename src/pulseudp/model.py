"""Plot grouping and rolling history (Qt-free); design in docs/gui-design.md."""

from __future__ import annotations

import math
import threading
import warnings
from dataclasses import dataclass, field as _dc_field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .protocol import Descriptor


@dataclass
class Trace:
    key: str          # unique channel key in the RingBuffer
    label: str
    is_bit: bool = False


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

        units_group: Dict[str, PlotGroup] = {}
        for f in descriptor.plot_fields:
            if f.is_bitfield:
                grp = PlotGroup(title=f.name, units=None, kind="bitfield")
                for bit, name in enumerate(f.bits or []):
                    if name == "Reserved":
                        continue
                    key = "{}.{}".format(f.name, name)
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
        """All trace keys, in plot order — the RingBuffer's channel set."""
        return list(self._channel_keys)

    def extract(self, packets: np.ndarray) -> Tuple[int, Dict[str, np.ndarray]]:
        return self.flatten(self.descriptor.channels(packets))

    def flatten(self, channels: Dict[str, np.ndarray]
                ) -> Tuple[int, Dict[str, np.ndarray]]:
        """Map :meth:`Descriptor.channels` output to (n_samples, {trace_key: values})."""
        n = 0
        flat: Dict[str, np.ndarray] = {}
        for f in self.descriptor.plot_fields:
            col = channels.get(f.name)
            if col is None:
                continue   # disabled channel (v2.0): omitted -> NaN in the buffer
            n = len(col)
            if f.is_bitfield:
                for name, arr in Descriptor.bit_traces(f, col).items():
                    flat["{}.{}".format(f.name, name)] = arr.astype(np.float64)
            else:
                flat[f.name] = col
        return n, flat


# Pyramid factor and height cap; rationale in docs/gui-design.md.
PYRAMID_FACTOR = 8
MAX_LEVELS = 12

# float32 halves the footprint; X is never stored, it is regenerated as float64.
_STORE_DTYPE = np.float32


@dataclass
class DecimatedView:
    """One redraw's slice: raw samples if ``exact``, else per-bucket min/max at bucket centres."""

    exact: bool
    x: np.ndarray
    ymin: Dict[str, np.ndarray]
    ymax: Dict[str, np.ndarray]


def _nanmin(a: np.ndarray, axis: int) -> np.ndarray:
    with warnings.catch_warnings():          # all-NaN bucket -> NaN (a gap), no warning
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmin(a, axis=axis)


def _nanmax(a: np.ndarray, axis: int) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmax(a, axis=axis)


class RingBuffer:
    """Thread-safe ring + min/max pyramid; docs/gui-design.md (Rendering pipeline)."""

    def __init__(self, channel_keys: List[str], window_n: int) -> None:
        self._keys = list(channel_keys)
        self._C = len(self._keys)
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
    def footprint_bytes(n_channels: int, window_n: int) -> int:
        """Bytes this buffer would allocate for ``window_n`` (raw + pyramid)."""
        cap, levels = RingBuffer._plan(window_n)
        itemsize = np.dtype(_STORE_DTYPE).itemsize
        total = n_channels * cap * itemsize
        for L in range(1, levels + 1):
            cap_l = cap // (PYRAMID_FACTOR ** L)
            total += 2 * n_channels * cap_l * itemsize           # min + max per level
        return total

    def _allocate(self, window_n: int) -> None:
        """(Re)allocate the raw ring and pyramid for a window length. Resets data."""
        cap, levels = self._plan(window_n)
        self._window_n = int(window_n)
        self._cap = cap
        self._levels = levels
        self._raw = np.zeros((self._C, cap), dtype=_STORE_DTYPE)
        # Pyramid level arrays, indexed 1..levels (index 0 is the raw ring).
        self._mn: List[Optional[np.ndarray]] = [None] * (levels + 1)
        self._mx: List[Optional[np.ndarray]] = [None] * (levels + 1)
        for L in range(1, levels + 1):
            cap_l = cap // (PYRAMID_FACTOR ** L)
            self._mn[L] = np.zeros((self._C, cap_l), dtype=_STORE_DTYPE)
            self._mx[L] = np.zeros((self._C, cap_l), dtype=_STORE_DTYPE)
        # Next bucket index to build at each level (== count of built buckets).
        self._built = [0] * (levels + 1)
        self._n_total = 0          # samples seen since clear() == next X index

    @property
    def window_n(self) -> int:
        return self._window_n

    @property
    def capacity(self) -> int:
        return self._cap

    def _store(self, level: int) -> Tuple[np.ndarray, np.ndarray, int]:
        """Return (min_array, max_array, capacity) for a level (raw is level 0)."""
        if level == 0:
            return self._raw, self._raw, self._cap
        return self._mn[level], self._mx[level], self._mn[level].shape[1]

    def _gather(self, level: int, which: str, lo: int, hi: int) -> np.ndarray:
        """Read buckets/samples [lo, hi) of a level into a contiguous (C, hi-lo)."""
        mn, mx, cap_l = self._store(level)
        arr = mx if which == "mx" else mn
        count = hi - lo
        start = lo % cap_l
        end = start + count
        if end <= cap_l:
            return np.array(arr[:, start:end])
        k = cap_l - start
        out = np.empty((self._C, count), dtype=arr.dtype)
        out[:, :k] = arr[:, start:cap_l]
        out[:, k:] = arr[:, 0:end - cap_l]
        return out

    def _scatter(self, level: int, which: str, lo: int, hi: int,
                 data: np.ndarray) -> None:
        """Write ``data`` (C, hi-lo) into buckets/samples [lo, hi) of a level."""
        mn, mx, cap_l = self._store(level)
        arr = mx if which == "mx" else mn
        count = hi - lo
        start = lo % cap_l
        end = start + count
        if end <= cap_l:
            arr[:, start:end] = data
            return
        k = cap_l - start
        arr[:, start:cap_l] = data[:, :k]
        arr[:, 0:end - cap_l] = data[:, k:]

    def _fill_level(self, L: int, b_lo: int, b_hi: int) -> None:
        """Compute buckets [b_lo, b_hi) of level L by reducing level L-1 by F."""
        F = PYRAMID_FACTOR
        count = b_hi - b_lo
        child_mn = self._gather(L - 1, "mn", b_lo * F, b_hi * F)
        child_mx = self._gather(L - 1, "mx", b_lo * F, b_hi * F)
        self._scatter(L, "mn", b_lo, b_hi,
                      _nanmin(child_mn.reshape(self._C, count, F), axis=2))
        self._scatter(L, "mx", b_lo, b_hi,
                      _nanmax(child_mx.reshape(self._C, count, F), axis=2))

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
            tail = self._gather(0, "mn", lo, hi) if keep else None   # read on OLD ring
            self._allocate(new_w)                                    # resets to empty
            if keep:
                self._n_total = lo
                self._scatter(0, "mn", lo, hi, tail)
                self._n_total = hi
                self._rebuild(lo, hi)

    def clear(self) -> None:
        with self._lock:
            self._n_total = 0
            self._built = [0] * (self._levels + 1)

    def append(self, n: int, channels: Dict[str, np.ndarray]) -> None:
        """Append ``n`` samples at the next X indices; absent keys are stored as NaN."""
        if n <= 0:
            return
        with self._lock:
            block = np.empty((self._C, n), dtype=_STORE_DTYPE)
            for ci, k in enumerate(self._keys):
                col = channels.get(k)
                if col is None:
                    block[ci, :] = np.nan
                else:
                    block[ci, :] = np.asarray(col, dtype=_STORE_DTYPE)
            a = self._n_total
            self._scatter(0, "mn", a, a + n, block)
            self._n_total = a + n
            self._build_incremental()

    def latest_index(self) -> Optional[int]:
        with self._lock:
            return self._n_total - 1 if self._n_total else None

    def _resident_lo_locked(self) -> int:
        return max(0, self._n_total - self._window_n)

    def snapshot(self) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
        """Copy of the full window (tests); the GUI uses :meth:`view`."""
        with self._lock:
            if self._n_total == 0:
                empty = np.empty(0, dtype=np.float64)
                return empty, {k: np.empty(0, dtype=np.float64) for k in self._keys}
            hi = self._n_total
            lo = self._resident_lo_locked()
            x = np.arange(lo, hi, dtype=np.float64)
            raw = self._gather(0, "mn", lo, hi)
            chans = {k: raw[i].astype(np.float64) for i, k in enumerate(self._keys)}
            return x, chans

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
                    ymin = self._gather(L, "mn", b0, b1 + 1)
                    ymax = self._gather(L, "mx", b0, b1 + 1)
                    x = (np.arange(b0, b1 + 1, dtype=np.float64) + 0.5) * s
                    return DecimatedView(
                        exact=False, x=x,
                        ymin={k: ymin[i] for i, k in enumerate(self._keys)},
                        ymax={k: ymax[i] for i, k in enumerate(self._keys)})

            # Exact: raw samples (envelope min == max).
            raw = self._gather(0, "mn", i0, i1 + 1)
            x = np.arange(i0, i1 + 1, dtype=np.float64)
            cols = {k: raw[i] for i, k in enumerate(self._keys)}
            return DecimatedView(exact=True, x=x, ymin=cols, ymax=cols)
