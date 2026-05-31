"""Plot model and rolling history for the pulseUDP client.

Two pieces:

* :class:`PlotModel` turns a :class:`~pulseudp.protocol.Descriptor` into the set
  of plot groups the GUI draws (units-grouped numeric plots, one-per-unitless
  numeric plot, one digital plot per bitfield) and knows how to flatten a
  decoded packet array into the flat per-trace channels the buffer stores.
* :class:`RingBuffer` keeps a rolling, time-bounded history of those channels.
  Its window length (seconds) is user-selectable at runtime.

Neither piece touches Qt, so both are unit-testable headless.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field as _dc_field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .protocol import Descriptor, Field


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

    The time-base field (``descriptor.timestamp_field``) is never plotted.
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

    def extract(self, packets: np.ndarray) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
        """Flatten a decoded packet array into (time, {trace_key: values}).

        Decoded against the full descriptor, so the time base is always present.
        """
        t, flat = self.flatten(self.descriptor.channels(packets))
        assert t is not None        # full descriptor always carries the time base
        return t, flat

    def flatten(self, channels: Dict[str, np.ndarray]
                ) -> Tuple[Optional[np.ndarray], Dict[str, np.ndarray]]:
        """Map per-field channel arrays to (time, {trace_key: values}).

        ``channels`` is :meth:`Descriptor.channels` output — physical-unit arrays
        keyed by field name, possibly covering only a v2.0 *enabled* subset. Time
        is the time-base field in seconds; bitfields are expanded into 0/1 bit
        traces. Fields absent from ``channels`` (disabled channels) are skipped —
        the RingBuffer fills their keys with NaN, so they draw as gaps. Returns
        ``(None, {})`` if the time-base channel itself is absent.
        """
        t = channels.get(self.descriptor.timestamp_field.name)
        if t is None:
            return None, {}
        t = t.astype(np.float64)

        flat: Dict[str, np.ndarray] = {}
        for f in self.descriptor.plot_fields:
            col = channels.get(f.name)
            if col is None:
                continue   # disabled channel (v2.0): omitted -> NaN in the buffer
            if f.is_bitfield:
                for name, arr in Descriptor.bit_traces(f, col).items():
                    flat["{}.{}".format(f.name, name)] = arr.astype(np.float64)
            else:
                flat[f.name] = col
        return t, flat


# --- Rolling history ---------------------------------------------------------


class RingBuffer:
    """Thread-safe rolling history of a fixed set of channels.

    Stored as a list of per-append chunks so appends are O(1) and trimming
    drops whole chunks; the GUI concatenates the retained window on demand
    (once per redraw, not per packet). Retains roughly ``window_s`` seconds of
    the most recent data, keyed off the latest sample's timestamp.
    """

    def __init__(self, channel_keys: List[str], window_s: float = 5.0) -> None:
        self._keys = list(channel_keys)
        self._window_s = float(window_s)
        self._lock = threading.Lock()
        self._t_chunks: List[np.ndarray] = []
        self._chunks: Dict[str, List[np.ndarray]] = {k: [] for k in self._keys}
        self._latest_t: Optional[float] = None

    @property
    def window_s(self) -> float:
        return self._window_s

    def set_window(self, window_s: float) -> None:
        """Change the retained window length (seconds) and trim immediately."""
        with self._lock:
            self._window_s = float(window_s)
            self._trim_locked()

    def clear(self) -> None:
        with self._lock:
            self._t_chunks = []
            self._chunks = {k: [] for k in self._keys}
            self._latest_t = None

    def append(self, t: np.ndarray, channels: Dict[str, np.ndarray]) -> None:
        """Append a chunk of samples. ``channels`` must cover every channel key."""
        if t.size == 0:
            return
        with self._lock:
            self._t_chunks.append(np.asarray(t, dtype=np.float64))
            for k in self._keys:
                col = channels.get(k)
                if col is None:
                    col = np.full(t.size, np.nan, dtype=np.float64)
                self._chunks[k].append(np.asarray(col, dtype=np.float64))
            self._latest_t = float(t[-1])
            self._trim_locked()

    def _trim_locked(self) -> None:
        if self._latest_t is None:
            return
        cutoff = self._latest_t - self._window_s
        # Drop leading chunks entirely older than the cutoff.
        while len(self._t_chunks) > 1 and self._t_chunks[0][-1] < cutoff:
            self._t_chunks.pop(0)
            for k in self._keys:
                self._chunks[k].pop(0)

    def latest_time(self) -> Optional[float]:
        with self._lock:
            return self._latest_t

    def snapshot(self) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
        """Return a concatenated copy of the retained window: (time, channels)."""
        with self._lock:
            if not self._t_chunks:
                empty = np.empty(0, dtype=np.float64)
                return empty, {k: np.empty(0, dtype=np.float64) for k in self._keys}
            t = np.concatenate(self._t_chunks)
            chans = {k: np.concatenate(self._chunks[k]) for k in self._keys}
            return t, chans
