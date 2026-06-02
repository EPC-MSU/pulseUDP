"""Plot model and rolling history for the pulseUDP client.

Two pieces:

* :class:`PlotModel` turns a :class:`~pulseudp.protocol.Descriptor` into the set
  of plot groups the GUI draws (units-grouped numeric plots, one-per-unitless
  numeric plot, one digital plot per bitfield) and knows how to flatten a
  decoded packet array into the flat per-trace channels the buffer stores.
* :class:`RingBuffer` keeps a rolling history of those channels in a fixed,
  preallocated ring (no per-frame concatenation), alongside a **min/max
  decimation pyramid** so the GUI can draw an arbitrarily long history at screen
  resolution. Its window length (number of samples) is user-selectable at
  runtime; changing it preserves the most recent samples.

The X axis is a synthetic per-sample counter the buffer generates on append —
the index of each sample in the stream.

Neither piece touches Qt, so both are unit-testable headless.
"""

from __future__ import annotations

import math
import threading
import warnings
from dataclasses import dataclass, field as _dc_field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .protocol import Descriptor


# --- Plot model --------------------------------------------------------------


@dataclass
class Trace:
    """One drawable line within a plot group."""

    key: str          # unique channel key in the RingBuffer
    label: str        # legend / list label
    is_bit: bool = False


@dataclass
class PlotGroup:
    """A single plot widget's worth of traces."""

    title: str
    units: Optional[str]
    kind: str                       # "numeric" | "bitfield"
    traces: List[Trace] = _dc_field(default_factory=list)


class PlotModel:
    """Maps a descriptor to plot groups and flattens decoded packets to traces.

    Grouping rules (see docs/gui-design.md):

    * numeric fields that share a ``units`` string → one plot per distinct unit;
    * numeric fields with no ``units`` → one plot each;
    * each ``bitfield`` → its own digital plot, one trace per non-``Reserved`` bit.

    Every field is plotted; the X axis is a synthetic per-sample counter the
    :class:`RingBuffer` generates.
    """

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
            else:  # unitless numeric -> its own plot
                grp = PlotGroup(title=f.name, units=None, kind="numeric")
                grp.traces.append(Trace(key=f.name, label=f.name))
                self.groups.append(grp)
                self._channel_keys.append(f.name)

    @property
    def channel_keys(self) -> List[str]:
        """All trace keys, in plot order — the RingBuffer's channel set."""
        return list(self._channel_keys)

    def extract(self, packets: np.ndarray) -> Tuple[int, Dict[str, np.ndarray]]:
        """Flatten a decoded packet array into (n_samples, {trace_key: values})."""
        return self.flatten(self.descriptor.channels(packets))

    def flatten(self, channels: Dict[str, np.ndarray]
                ) -> Tuple[int, Dict[str, np.ndarray]]:
        """Map per-field channel arrays to (n_samples, {trace_key: values}).

        ``channels`` is :meth:`Descriptor.channels` output — physical-unit arrays
        keyed by field name, possibly covering only a v2.0 *enabled* subset.
        Bitfields are expanded into 0/1 bit traces. Fields absent from
        ``channels`` (disabled channels) are skipped — the RingBuffer fills their
        keys with NaN, so they draw as gaps. ``n_samples`` is the packet count
        (the length of every channel array), or 0 if ``channels`` is empty; the
        RingBuffer generates the per-sample X index from it.
        """
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


# --- Rolling history ---------------------------------------------------------

# Pyramid decimation factor: each level summarises PYRAMID_FACTOR buckets of the
# level below into one (min, max) pair. 8 keeps the level count low (a level per
# ~3 binary octaves) while bounding the per-level memory overhead to ~1/7 of the
# raw store. MAX_LEVELS caps the pyramid height for tiny windows.
PYRAMID_FACTOR = 8
MAX_LEVELS = 12

# Raw samples and pyramid buckets are stored as float32: plotting precision only
# needs ~7 digits and it halves the footprint of a large history. The X axis is
# never stored — it is the integer sample index, regenerated as float64 on read
# (float32 cannot represent sample indices past 2**24 exactly).
_STORE_DTYPE = np.float32


@dataclass
class DecimatedView:
    """A screen-resolution slice of the history for one redraw.

    ``x`` are sample indices (bucket centres when decimated). When ``exact`` is
    True the view is raw samples and ``ymin``/``ymax`` are the same array per
    channel; otherwise each channel carries the per-bucket min/max envelope.
    """

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
    """Thread-safe rolling history of a fixed set of channels, with a pyramid.

    The X axis is a synthetic per-sample counter: every appended sample gets the
    next integer index in the stream, so the X value of a sample is its ordinal
    position since the last :meth:`clear`. A dropped datagram (silently in v1.0,
    logged via ``seq_gap`` in v2.0) just means the index skips the lost samples
    without a visible gap.

    Storage is a fixed, preallocated channel-major ring of shape
    ``(n_channels, capacity)`` plus a **min/max decimation pyramid**: level *L*
    keeps one (min, max) pair per ``PYRAMID_FACTOR**L`` raw samples. The pyramid
    is updated incrementally on :meth:`append` (amortised O(1) per sample) and is
    only *queried* by :meth:`view` — zooming and resizing pick a level and slice
    it, they never recompute from the full history. Render cost is therefore
    bounded by the pixel width of the plot, not the length of the history, so the
    buffer scales to a multi-GiB window. See ``docs/gui-design.md``.

    ``capacity`` is the requested ``window_n`` rounded up to a whole number of
    coarsest-level buckets; :meth:`snapshot` and :meth:`view` only ever expose the
    most recent ``window_n`` samples, so the rounding never leaks older data.
    """

    def __init__(self, channel_keys: List[str], window_n: int) -> None:
        self._keys = list(channel_keys)
        self._C = len(self._keys)
        self._lock = threading.Lock()
        self._allocate(int(window_n))

    # -- allocation / planning ------------------------------------------------

    @staticmethod
    def _plan(window_n: int) -> Tuple[int, int]:
        """Return (capacity, n_levels) for a requested window length."""
        w = max(1, int(window_n))
        # Tallest pyramid whose coarsest bucket still spans <= the window.
        levels = max(1, min(MAX_LEVELS, int(math.floor(math.log(w, PYRAMID_FACTOR)))))
        coarsest = PYRAMID_FACTOR ** levels
        capacity = ((w + coarsest - 1) // coarsest) * coarsest   # multiple of coarsest
        return capacity, levels

    @staticmethod
    def footprint_bytes(n_channels: int, window_n: int) -> int:
        """Bytes this buffer would allocate for ``window_n`` (raw + pyramid)."""
        cap, levels = RingBuffer._plan(window_n)
        itemsize = np.dtype(_STORE_DTYPE).itemsize
        total = n_channels * cap * itemsize                      # raw ring
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

    # -- ring position helpers ------------------------------------------------

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
            return np.array(arr[:, start:end])                   # contiguous copy
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
        """Fold newly appended samples into every pyramid level (ascending)."""
        for L in range(1, self._levels + 1):
            avail = self._n_total // (PYRAMID_FACTOR ** L)       # complete buckets now
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

    # -- public API -----------------------------------------------------------

    def set_window(self, window_n: int) -> None:
        """Change the retained window length, preserving the most recent samples.

        Reallocates the ring/pyramid to the new size and copies the retained tail
        back at its original sample indices (so the X axis is continuous), then
        rebuilds the pyramid over it. This is an O(retained) one-shot on a user
        action, not a per-frame cost.
        """
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
                self._scatter(0, "mn", lo, hi, tail)                 # restore at [lo,hi)
                self._n_total = hi
                self._rebuild(lo, hi)

    def clear(self) -> None:
        with self._lock:
            self._n_total = 0
            self._built = [0] * (self._levels + 1)

    def append(self, n: int, channels: Dict[str, np.ndarray]) -> None:
        """Append ``n`` samples, assigning them the next ``n`` X indices.

        ``channels`` maps trace keys to length-``n`` arrays; keys absent from it
        (disabled v2.0 channels) are filled with NaN so they draw as gaps.
        """
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
        """Oldest sample index currently exposed (logical window, lock held)."""
        return max(0, self._n_total - self._window_n)

    def snapshot(self) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
        """Return a copy of the full retained window: (x_index, channels).

        Kept for headless use/tests; the GUI redraw uses :meth:`view` instead so
        it never copies the whole window per frame.
        """
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
        """Decimated slice of [x0, x1] for ~``max_points`` pixels.

        Picks the coarsest pyramid level whose bucket is <= one pixel wide and
        returns its min/max envelope over the visible range; falls back to exact
        raw samples when zoomed in far enough (or when no complete bucket covers
        the range). Returns None when there is nothing resident to draw.
        """
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
