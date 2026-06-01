"""Plot model and rolling history for the pulseUDP client.

Two pieces:

* :class:`PlotModel` turns a :class:`~pulseudp.protocol.Descriptor` into the set
  of plot groups the GUI draws (units-grouped numeric plots, one-per-unitless
  numeric plot, one digital plot per bitfield) and knows how to flatten a
  decoded packet array into the flat per-trace channels the buffer stores.
* :class:`RingBuffer` keeps a rolling history of those channels, bounded by a
  count of the most recent samples. Its window length (number of samples) is
  user-selectable at runtime.

The X axis is a synthetic per-sample counter the buffer generates on append —
the index of each sample in the stream.

Neither piece touches Qt, so both are unit-testable headless.
"""

from __future__ import annotations

import threading
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


class RingBuffer:
    """Thread-safe rolling history of a fixed set of channels.

    The X axis is a synthetic per-sample counter: every appended sample gets the
    next integer index in the stream, so the X value of a sample is its ordinal
    position since the last :meth:`clear`. A dropped datagram (silently in v1.0,
    logged via ``seq_gap`` in v2.0) just means the index skips the lost samples
    without a visible gap.

    Stored as a list of per-append chunks so appends are O(1) and trimming
    drops whole chunks; the GUI concatenates the retained window on demand
    (once per redraw, not per packet). Retains roughly ``window_n`` of the most
    recent samples.
    """

    def __init__(self, channel_keys: List[str], window_n: int = 5000) -> None:
        self._keys = list(channel_keys)
        self._window_n = int(window_n)
        self._lock = threading.Lock()
        self._x_chunks: List[np.ndarray] = []
        self._chunks: Dict[str, List[np.ndarray]] = {k: [] for k in self._keys}
        self._n_total = 0          # samples seen since clear() == next X index

    @property
    def window_n(self) -> int:
        return self._window_n

    def set_window(self, window_n: int) -> None:
        """Change the retained window length (samples) and trim immediately."""
        with self._lock:
            self._window_n = int(window_n)
            self._trim_locked()

    def clear(self) -> None:
        with self._lock:
            self._x_chunks = []
            self._chunks = {k: [] for k in self._keys}
            self._n_total = 0

    def append(self, n: int, channels: Dict[str, np.ndarray]) -> None:
        """Append ``n`` samples, assigning them the next ``n`` X indices.

        ``channels`` maps trace keys to length-``n`` arrays; keys absent from it
        (disabled v2.0 channels) are filled with NaN so they draw as gaps.
        """
        if n <= 0:
            return
        with self._lock:
            x = np.arange(self._n_total, self._n_total + n, dtype=np.float64)
            self._n_total += n
            self._x_chunks.append(x)
            for k in self._keys:
                col = channels.get(k)
                if col is None:
                    col = np.full(n, np.nan, dtype=np.float64)
                self._chunks[k].append(np.asarray(col, dtype=np.float64))
            self._trim_locked()

    def _trim_locked(self) -> None:
        if not self._x_chunks:
            return
        cutoff = (self._n_total - 1) - self._window_n
        # Drop leading chunks whose samples are all older than the cutoff index.
        while len(self._x_chunks) > 1 and self._x_chunks[0][-1] < cutoff:
            self._x_chunks.pop(0)
            for k in self._keys:
                self._chunks[k].pop(0)

    def latest_index(self) -> Optional[int]:
        with self._lock:
            return self._n_total - 1 if self._n_total else None

    def snapshot(self) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
        """Return a concatenated copy of the retained window: (x_index, channels)."""
        with self._lock:
            if not self._x_chunks:
                empty = np.empty(0, dtype=np.float64)
                return empty, {k: np.empty(0, dtype=np.float64) for k in self._keys}
            x = np.concatenate(self._x_chunks)
            chans = {k: np.concatenate(self._chunks[k]) for k in self._keys}
            return x, chans
