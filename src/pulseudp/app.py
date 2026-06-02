"""pulseUDP GUI client.

A single-window PyQt5 + pyqtgraph application that:

  1. addresses / (later) discovers a server on UDP port 2102,
  2. requests the JSON descriptor (``DESCRIPTION``) and builds the plot layout,
  3. starts the telemetry stream (``TELEMETRY``) and plots it live,
  4. stops the stream (``STOP``).

Architecture (see docs/gui-design.md): a :class:`~pulseudp.client.UdpClient`
receiver thread decodes datagrams and appends to a thread-safe
:class:`~pulseudp.model.RingBuffer`; a ``QTimer`` on the GUI thread redraws the
curves at a fixed rate. Network log/state callbacks are marshalled onto the GUI
thread through Qt signals.
"""

from __future__ import annotations

import json
import pkgutil
import sys
import threading
import time
from typing import Dict, List, Optional

import numpy as np

try:
    from PyQt5 import QtCore, QtGui, QtWidgets
    import pyqtgraph as pg
except ImportError as exc:  # pragma: no cover - import-time guard
    raise SystemExit(
        "The GUI needs the 'gui' extra: pip install -e .[gui] "
        "(PyQt5 + pyqtgraph + numpy)\n" + str(exc))

from .client import LogEvent, UdpClient
from .discovery import Discovery, NullDiscovery
from .model import PlotModel, RingBuffer
from .protocol import Descriptor

DEFAULT_PORT = 2102
REDRAW_HZ = 30
# The X axis counts samples, not seconds: windows are sample counts.
DEFAULT_VIEW_N = 500        # initial zoom: samples per screen (wheel-controlled)
DEFAULT_HISTORY_N = 1_000_000   # initial rolling-history retention (combo-controlled)
MIN_WINDOW_N = 2
MAX_WINDOW_N = 1_000_000_000

# Selectable rolling-history sizes (samples). Retention is picked from this list,
# not free-typed, because the only meaningful axis is order of magnitude and a
# big value must be checked against free RAM before it is allocated.
HISTORY_OPTIONS = [
    ("1K", 1_000), ("10K", 10_000), ("100K", 100_000), ("1M", 1_000_000),
    ("10M", 10_000_000), ("100M", 100_000_000), ("1G", 1_000_000_000),
]
# Fraction of currently-available RAM a new history buffer may claim.
RAM_BUDGET_FRACTION = 0.8


def _available_ram_bytes() -> Optional[int]:
    """Best-effort free RAM in bytes; None if it cannot be determined.

    Cross-platform with no required dependency: ``psutil`` if installed, else a
    per-OS probe (``GlobalMemoryStatusEx`` on Windows, ``/proc/meminfo`` on
    Linux). Returns None when none apply, which leaves the RAM guard inert rather
    than guessing.
    """
    try:
        import psutil  # optional dependency
        return int(psutil.virtual_memory().available)
    except Exception:  # noqa: BLE001 - psutil missing or failed
        pass
    if sys.platform.startswith("win"):
        try:
            import ctypes

            class _MemStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MemStatusEx()
            stat.dwLength = ctypes.sizeof(stat)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullAvailPhys)
        except Exception:  # noqa: BLE001 - unexpected ctypes/platform failure
            pass
        return None
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024     # kB -> bytes
    except OSError:
        pass
    return None

# Trace colour palette (cycled across all plots).
_PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b",
    "#e377c2", "#7f7f7f", "#bcbd22", "#17becf", "#aec7e8", "#ffbb78",
]


def _load_schema() -> Optional[dict]:
    """Load the bundled descriptor schema (validation is optional).

    The schema ships as package data (``pulseudp/data/Schema.json``), so it is
    available in installed wheels and frozen builds, not just source checkouts.
    """
    try:
        raw = pkgutil.get_data("pulseudp", "data/Schema.json")
        return json.loads(raw.decode("utf-8")) if raw else None
    except (OSError, ValueError):
        return None


class _Bridge(QtCore.QObject):
    """Carries receiver-thread callbacks onto the GUI thread."""

    log = QtCore.pyqtSignal(object)         # LogEvent
    state = QtCore.pyqtSignal(str, str)     # (state, detail)
    descriptor = QtCore.pyqtSignal(object)  # Descriptor (on connect)
    channels = QtCore.pyqtSignal(object)    # List[bool] enabled-channel set (v2.0)
    error = QtCore.pyqtSignal(str)


class TelemetryViewBox(pg.ViewBox):
    """ViewBox whose wheel zoom obeys the running/stopped state machine.

    The wheel always changes the sample window by a factor of two. While the
    stream is running the window stays anchored to the latest sample (the main
    window keeps following); while stopped it zooms around the mouse pointer.
    """

    def __init__(self, window: "MainWindow", **kwargs) -> None:
        super().__init__(**kwargs)
        self._win = window

    def wheelEvent(self, ev, axis=None):
        delta = ev.delta() if hasattr(ev, "delta") else ev.angleDelta().y()
        factor = 0.5 if delta > 0 else 2.0   # wheel up = zoom in = shorter window
        try:
            x = self.mapSceneToView(ev.scenePos()).x()
        except Exception:
            x = None
        self._win.on_wheel(factor, x)
        ev.accept()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, host: str = "127.0.0.1",
                 discovery: Optional[Discovery] = None) -> None:
        super().__init__()
        self.setWindowTitle("pulseUDP client")
        self.resize(1100, 760)

        self._schema = _load_schema()
        self._discovery = discovery or NullDiscovery()
        self._client: Optional[UdpClient] = None
        self._model: Optional[PlotModel] = None
        self._ring: Optional[RingBuffer] = None
        self._running = False
        # Two independent sample windows: how much data we KEEP (history, set by
        # the spin box) vs how much we SHOW (view, set by the wheel), both counted
        # in samples. The wheel never touches retention, so zooming cannot discard
        # data.
        self._history_n = DEFAULT_HISTORY_N
        self._view_n = float(DEFAULT_VIEW_N)

        self._plots: List[pg.PlotItem] = []
        self._visible_plots: List[pg.PlotItem] = []   # currently laid-out subset
        self._primary_plot: Optional[pg.PlotItem] = None  # X-axis anchor
        self._group_fields: List[List[int]] = []      # channel indices per plot
        self._link_vb: Optional[pg.ViewBox] = None
        self._curves: Dict[str, pg.PlotDataItem] = {}
        self._bit_offset: Dict[str, int] = {}   # bit trace key -> stack row

        # v2.0 channel selection: a checkbox per selectable channel, keyed by its
        # descriptor field index. Selection is only possible in v2.0 and only while
        # the stream is stopped (RFC §4); v1.0 keeps the boxes checked and greyed.
        self._channel_checks: Dict[int, QtWidgets.QCheckBox] = {}
        self._selectable = False        # True once a v2.0 session is negotiated
        self._suppress_check = False    # guard against re-entrant checkbox updates
        self._n_channels = 0

        self._bridge = _Bridge()
        self._bridge.log.connect(self._on_log)
        self._bridge.state.connect(self._on_state)
        self._bridge.descriptor.connect(self._on_descriptor)
        self._bridge.channels.connect(self._on_channels_result)
        self._bridge.error.connect(self._on_error)

        self._build_ui()

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._redraw)
        self._timer.start(int(1000 / REDRAW_HZ))

    # -- UI construction ------------------------------------------------------

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)

        outer.addWidget(self._build_connection_bar())

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        outer.addWidget(splitter, 1)

        tele_box = QtWidgets.QGroupBox("Telemetry")
        tele_layout = QtWidgets.QVBoxLayout(tele_box)
        self._tree = QtWidgets.QTreeWidget()
        self._tree.setHeaderLabels(["", "Name", "Type"])
        self._tree.setColumnWidth(0, 36)
        self._tree.setColumnWidth(1, 180)
        tele_layout.addWidget(self._tree)
        splitter.addWidget(tele_box)

        self._graphs = pg.GraphicsLayoutWidget()
        self._graphs.setBackground("w")
        splitter.addWidget(self._graphs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 820])

        self._build_log_dock()

    def _build_connection_bar(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("Connection")
        row = QtWidgets.QHBoxLayout(box)

        self._search_btn = QtWidgets.QPushButton("Search")
        self._search_btn.clicked.connect(self._on_search)
        row.addWidget(self._search_btn)

        self._device_combo = QtWidgets.QComboBox()
        self._device_combo.setMinimumWidth(160)
        self._device_combo.currentIndexChanged.connect(self._on_device_selected)
        row.addWidget(self._device_combo)

        row.addWidget(QtWidgets.QLabel("Address:"))
        self._ip_edit = QtWidgets.QLineEdit("127.0.0.1")
        self._ip_edit.setMaximumWidth(140)
        row.addWidget(self._ip_edit)

        self._connect_btn = QtWidgets.QPushButton("Connect")
        self._connect_btn.clicked.connect(self._on_connect)
        row.addWidget(self._connect_btn)

        self._start_btn = QtWidgets.QPushButton("Start")
        self._start_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._on_start_stop)
        row.addWidget(self._start_btn)

        row.addWidget(QtWidgets.QLabel("History (samples):"))
        self._history_combo = QtWidgets.QComboBox()
        for label, value in HISTORY_OPTIONS:
            self._history_combo.addItem(label, value)
        self._history_combo.setCurrentIndex(
            [v for _, v in HISTORY_OPTIONS].index(DEFAULT_HISTORY_N))
        self._history_combo.setToolTip(
            "How many recent samples to keep (rolling history). Independent of "
            "the wheel zoom — zooming never discards data. A size that would not "
            "fit in free RAM is rejected.")
        self._history_combo.currentIndexChanged.connect(self._on_history_changed)
        row.addWidget(self._history_combo)

        row.addStretch(1)
        self._status = QtWidgets.QLabel("Not connected")
        row.addWidget(self._status)
        return box

    def _build_log_dock(self) -> None:
        dock = QtWidgets.QDockWidget("Log", self)
        dock.setAllowedAreas(QtCore.Qt.BottomDockWidgetArea | QtCore.Qt.TopDockWidgetArea)
        self._log = QtWidgets.QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(5000)
        dock.setWidget(self._log)
        self.addDockWidget(QtCore.Qt.BottomDockWidgetArea, dock)
        self._log_dock = dock

    # -- connection / streaming controls --------------------------------------

    def _run_async(self, fn) -> None:
        """Run a (blocking) client transaction off the GUI thread."""
        threading.Thread(target=fn, daemon=True).start()

    def _on_search(self) -> None:
        self._device_combo.clear()
        devices = self._discovery.search(timeout=1.0)
        if not devices:
            self._append_log("info", "Search found no devices "
                             "(no discovery backend configured).")
            return
        for d in devices:
            self._device_combo.addItem(d.name, d.address)

    def _on_device_selected(self, index: int) -> None:
        addr = self._device_combo.itemData(index)
        if addr:
            self._ip_edit.setText(str(addr))

    def _on_connect(self) -> None:
        host = self._ip_edit.text().strip()
        if not host:
            return
        # Tear down any previous session.
        self._teardown_client()
        self._running = False
        self._start_btn.setText("Start")
        self._start_btn.setEnabled(False)
        # Neutralize channel selection until the new descriptor/version is known
        # (avoids a stale checkbox from the old session pushing SET_CHANNELS).
        self._selectable = False
        self._set_checks_enabled(False)

        # No version selector: the client probes at v2.0 and adopts whatever
        # version the server reveals in its DESCRIPTION reply (RFC §6.1).
        client = UdpClient(
            host, DEFAULT_PORT, schema=self._schema,
            on_telemetry=self._on_telemetry,
            on_log=lambda ev: self._bridge.log.emit(ev),
            on_state=lambda s, d: self._bridge.state.emit(s, d))
        self._client = client
        client.open()

        def work():
            try:
                desc = client.request_descriptor(timeout=1.0, retries=4)
                self._bridge.descriptor.emit(desc)
            except Exception as exc:  # noqa: BLE001 - surface to the log/UI
                self._bridge.error.emit(str(exc))

        self._run_async(work)

    def _on_start_stop(self) -> None:
        if self._client is None:
            return
        if not self._running:
            def work_start():
                try:
                    self._client.start_stream(timeout=1.0, retries=4)
                except Exception as exc:  # noqa: BLE001
                    self._bridge.error.emit(str(exc))
            self._run_async(work_start)
        else:
            client = self._client
            self._run_async(lambda: client.stop_stream(timeout=1.0))

    def _channel_count(self) -> Optional[int]:
        """Channels the buffer holds, or None until a descriptor is known."""
        return len(self._model.channel_keys) if self._model is not None else None

    def _history_fits(self, window_n: int) -> bool:
        """True if a ``window_n`` buffer fits the RAM budget.

        Optimistic when the answer cannot be known yet — no descriptor (channel
        count unknown) or no RAM reading available. The authoritative check runs
        in :meth:`_on_descriptor` once the real channel count is in hand.
        """
        channels = self._channel_count()
        avail = _available_ram_bytes()
        if channels is None or avail is None:
            return True
        return RingBuffer.footprint_bytes(channels, window_n) <= RAM_BUDGET_FRACTION * avail

    def _select_history_value(self, value: int) -> None:
        """Reflect an accepted history size in the combo without re-triggering."""
        idx = next((i for i, (_, v) in enumerate(HISTORY_OPTIONS) if v == value), -1)
        if idx >= 0:
            self._history_combo.blockSignals(True)
            self._history_combo.setCurrentIndex(idx)
            self._history_combo.blockSignals(False)

    def _on_history_changed(self, index: int) -> None:
        # The combo controls data RETENTION only — never the zoom.
        value = int(self._history_combo.itemData(index))
        if not self._history_fits(value):
            # _history_fits only rejects when the channel count is known.
            channels = self._channel_count() or 0
            need = RingBuffer.footprint_bytes(channels, value)
            self._append_log(
                "error",
                "History {} (~{:.1f} GiB for {} channels) exceeds free RAM; "
                "keeping {} samples.".format(
                    self._history_combo.itemText(index), need / 2**30,
                    channels, self._history_n))
            self._select_history_value(self._history_n)   # revert to last accepted
            return
        self._history_n = value
        if self._ring is not None:
            self._ring.set_window(self._history_n)

    # -- bridge slots (GUI thread) --------------------------------------------

    def _on_descriptor(self, descriptor: Descriptor) -> None:
        self._model = PlotModel(descriptor)
        # Now that the real channel count is known, the chosen history may no
        # longer fit; clamp down to the largest option that does.
        if not self._history_fits(self._history_n):
            for _, value in reversed(HISTORY_OPTIONS):
                if value <= self._history_n and self._history_fits(value):
                    self._append_log(
                        "info", "History clamped to {} samples to fit free RAM "
                        "({} channels).".format(value, self._channel_count()))
                    self._history_n = value
                    break
            self._select_history_value(self._history_n)
        self._ring = RingBuffer(self._model.channel_keys, window_n=self._history_n)
        self._n_channels = len(descriptor.fields)
        # Channel selection is a v2.0 feature; in v1.0 the list is immutable.
        self._selectable = (self._client is not None
                            and self._client.version[0] >= 2)
        self._build_plots(descriptor)
        self._start_btn.setEnabled(True)
        # v2.0: read the server's current enabled set and reflect it (RFC §4).
        if self._selectable and self._client is not None:
            client = self._client

            def work():
                try:
                    enabled = client.get_channels(timeout=1.0, retries=3)
                    self._bridge.channels.emit(enabled)
                except Exception as exc:  # noqa: BLE001
                    self._bridge.error.emit(str(exc))

            self._run_async(work)

    def _on_state(self, state: str, detail: str) -> None:
        label = state.capitalize()
        if detail:
            label += " — " + detail
        self._status.setText(label)
        if state == "streaming":
            self._running = True
            self._start_btn.setText("Stop")
            self._set_checks_enabled(False)   # can't change channels mid-stream
        elif state in ("stopped", "disconnected", "error"):
            self._running = False
            self._start_btn.setText("Start")
            self._set_checks_enabled(True)     # selectable again once stopped

    def _on_error(self, message: str) -> None:
        self._append_log("error", message)
        self._status.setText("Error — " + message)

    def _on_log(self, ev: LogEvent) -> None:
        self._append_log(ev.level, "[{}] {}".format(ev.category, ev.message))

    def _append_log(self, level: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._log.appendPlainText("{} {:<7} {}".format(stamp, level.upper(), message))

    # -- telemetry ingest (receiver thread) -----------------------------------

    def _on_telemetry(self, channels: Dict[str, np.ndarray]) -> None:
        # Runs on the client's receiver thread; RingBuffer is thread-safe.
        # ``channels`` covers only the enabled fields (v2.0 subset); flatten maps
        # them to trace keys and the RingBuffer fills disabled ones with NaN.
        if self._model is None or self._ring is None:
            return
        n, flat = self._model.flatten(channels)
        self._ring.append(n, flat)

    # -- plot building & redraw -----------------------------------------------

    def _build_plots(self, descriptor: Descriptor) -> None:
        self._graphs.clear()
        self._tree.clear()
        self._plots = []
        self._group_fields = []   # parallel to _plots: channel indices feeding each
        self._curves = {}
        self._bit_offset = {}
        self._channel_checks = {}
        self._link_vb = None
        color_i = 0
        # Field name -> descriptor index (= channel index in the bitmap, RFC §4).
        index_by_name = {f.name: i for i, f in enumerate(descriptor.fields)}

        # Telemetry tree: one row per field.
        field_by_name = {f.name: f for f in descriptor.fields}
        row = 0
        for gi, group in enumerate(self._model.groups):
            plot = self._graphs.addPlot(
                row=gi, col=0, viewBox=TelemetryViewBox(self))
            plot.showGrid(x=True, y=True, alpha=0.3)
            plot.setLabel("bottom", "sample")
            if group.units:
                plot.setLabel("left", group.units)
            else:
                plot.setLabel("left", group.title)
            plot.addLegend(offset=(10, 5))
            vb = plot.getViewBox()
            if self._link_vb is None:
                self._link_vb = vb
            else:
                plot.setXLink(self._plots[0])
            vb.setAutoVisible(y=True)
            vb.enableAutoRange(x=False, y=True)
            self._plots.append(plot)
            # Descriptor channels feeding this plot (for show/hide on selection):
            # a bitfield plot is fed by its one field; a numeric plot by each of
            # its traces' fields.
            if group.kind == "bitfield":
                gfis = [index_by_name.get(group.title)]
            else:
                gfis = [index_by_name.get(tr.key) for tr in group.traces]
            self._group_fields.append([i for i in gfis if i is not None])

            if group.kind == "bitfield":
                # Stack bits on integer rows; label the y axis with bit names.
                ticks = []
                for bi, tr in enumerate(group.traces):
                    color = _PALETTE[color_i % len(_PALETTE)]
                    color_i += 1
                    self._bit_offset[tr.key] = bi
                    curve = plot.plot(pen=pg.mkPen(color, width=2), name=tr.label)
                    self._curves[tr.key] = curve
                    ticks.append((bi, tr.label))
                plot.getAxis("left").setTicks([ticks])
                vb.enableAutoRange(x=False, y=False)
                vb.setYRange(-0.5, max(1, len(group.traces)) - 0.5 + 0.8)
                # Tree: bitfield field with its bits as children.
                fld = field_by_name.get(group.title)
                parent = self._add_field_row(group.title,
                                             fld.type if fld else "bitfield", None,
                                             index_by_name.get(group.title))
                for bi, tr in enumerate(group.traces):
                    child = QtWidgets.QTreeWidgetItem(["", tr.label, "bit"])
                    child.setIcon(1, self._swatch(_PALETTE[
                        (color_i - len(group.traces) + bi) % len(_PALETTE)]))
                    parent.addChild(child)
                parent.setExpanded(False)
            else:
                for tr in group.traces:
                    color = _PALETTE[color_i % len(_PALETTE)]
                    color_i += 1
                    curve = plot.plot(pen=pg.mkPen(color, width=2), name=tr.label)
                    self._curves[tr.key] = curve
                    fld = field_by_name.get(tr.key)
                    self._add_field_row(tr.label, fld.type if fld else "", color,
                                        index_by_name.get(tr.key))

        self._tree.expandToDepth(0)
        # All groups visible initially; channel selection narrows this (v2.0).
        self._visible_plots = list(self._plots)
        self._primary_plot = self._plots[0] if self._plots else None

    def _add_field_row(self, name: str, type_str: str, color: Optional[str],
                       field_index: Optional[int]) -> QtWidgets.QTreeWidgetItem:
        """Add a top-level field row with its channel-enable checkbox.

        The checkbox is live only in v2.0 and only while the stream is stopped
        (RFC §4 channel selection); in v1.0 it stays checked and greyed because
        the channel list is immutable.
        """
        item = QtWidgets.QTreeWidgetItem(["", name, type_str])
        if color:
            item.setIcon(1, self._swatch(color))
        self._tree.addTopLevelItem(item)
        check = QtWidgets.QCheckBox()
        check.setChecked(True)
        check.setEnabled(self._selectable and not self._running)
        check.setToolTip(
            "Enable/disable this channel (stop the stream to change)"
            if self._selectable else
            "Channel selection is a v2.0 feature; v1.0 channels are fixed")
        check.stateChanged.connect(self._on_channel_toggled)
        self._tree.setItemWidget(item, 0, check)
        if field_index is not None:
            self._channel_checks[field_index] = check
        return item

    # -- channel selection (v2.0, RFC §4) -------------------------------------

    def _set_checks_enabled(self, on: bool) -> None:
        """Enable/disable every channel checkbox (no-op unless v2.0)."""
        enabled = bool(on) and self._selectable
        for chk in self._channel_checks.values():
            chk.setEnabled(enabled)

    def _on_channel_toggled(self, _state: int = 0) -> None:
        """A channel checkbox changed: push the new selection with SET_CHANNELS."""
        if (self._suppress_check or not self._selectable or self._running
                or self._client is None):
            return
        desired = [True] * self._n_channels
        for idx, chk in self._channel_checks.items():
            desired[idx] = chk.isChecked()
        self._set_checks_enabled(False)      # lock the boxes during the round trip
        client = self._client

        def work():
            try:
                accepted = client.set_channels(desired, timeout=1.0, retries=3)
                self._bridge.channels.emit(accepted)
            except Exception as exc:  # noqa: BLE001
                self._bridge.error.emit(str(exc))

        self._run_async(work)

    def _on_channels_result(self, enabled) -> None:
        """Reflect the server's authoritative enabled set in the checkboxes."""
        self._suppress_check = True         # programmatic update: don't re-emit
        try:
            for idx, chk in self._channel_checks.items():
                chk.setChecked(bool(idx < len(enabled) and enabled[idx]))
        finally:
            self._suppress_check = False
        if self._ring is not None:
            self._ring.clear()              # packet layout changed; drop stale data
        self._relayout_plots(enabled)       # hide plots of disabled channels
        self._set_checks_enabled(not self._running)

    def _relayout_plots(self, enabled) -> None:
        """Show only plots with an enabled channel; reflow them to fill the space.

        Disabled channels (v2.0) carry no data, so their plots are pulled out of
        the graphics layout and the rest pack upward. The full channel list stays
        in the tree, so any channel can be re-enabled. The X-axis follows the
        first visible plot.
        """
        if not self._plots:
            return
        n = len(enabled)

        def visible(gi: int) -> bool:
            fis = self._group_fields[gi]
            return any(i < n and enabled[i] for i in fis) if fis else True

        shown = [self._plots[gi] for gi in range(len(self._plots)) if visible(gi)]
        # Remove every plot, then re-add the visible ones so rows pack with no gaps.
        for plot in self._plots:
            try:
                self._graphs.removeItem(plot)
            except Exception:  # noqa: BLE001 - already out of the layout
                pass
        self._visible_plots = []
        self._primary_plot = None
        for r, plot in enumerate(shown):
            self._graphs.addItem(plot, row=r, col=0)
            if self._primary_plot is None:
                self._primary_plot = plot
                plot.setXLink(None)              # primary anchors the X axis
            else:
                plot.setXLink(self._primary_plot)
            self._visible_plots.append(plot)

    @staticmethod
    def _swatch(color: str) -> QtGui.QIcon:
        pix = QtGui.QPixmap(12, 12)
        pix.fill(QtGui.QColor(color))
        return QtGui.QIcon(pix)

    def _redraw(self) -> None:
        if self._ring is None or self._model is None or self._primary_plot is None:
            return
        latest = self._ring.latest_index()
        if latest is None:
            return
        vb = self._primary_plot.getViewBox()
        # Pixel width of the plot bounds how many points are worth drawing; the
        # pyramid decimates to that, so render cost is independent of history size.
        px = vb.width()
        px = int(px) if px and px > 1 else 1000
        if self._running:
            # Following the latest sample at the current zoom width.
            x1 = float(latest)
            x0 = x1 - self._view_n
        else:
            (x0, x1), _ = vb.viewRange()

        view = self._ring.view(x0, x1, px)
        if view is not None and view.x.size:
            for key, curve in self._curves.items():
                lo = view.ymin.get(key)
                if lo is None:
                    continue
                if view.exact:
                    y = lo
                    if key in self._bit_offset:
                        y = y * 0.8 + self._bit_offset[key]
                    curve.setData(view.x, y)
                else:
                    # Draw the per-bucket envelope: two points per bucket (min then
                    # max) at the bucket's X, so spikes survive decimation.
                    hi = view.ymax[key]
                    xx = np.repeat(view.x, 2)
                    yy = np.empty(lo.size * 2, dtype=np.float64)
                    yy[0::2] = lo
                    yy[1::2] = hi
                    if key in self._bit_offset:
                        yy = yy * 0.8 + self._bit_offset[key]
                    curve.setData(xx, yy)

        if self._running:
            vb.setXRange(float(latest) - self._view_n, float(latest), padding=0)

    # -- wheel state machine --------------------------------------------------

    def on_wheel(self, factor: float, x: Optional[float]) -> None:
        # The wheel changes the VIEW width only; it never trims the RingBuffer, so
        # data outside the visible range is retained (up to the history size) and
        # reappears when zooming back out.
        self._view_n = min(MAX_WINDOW_N, max(MIN_WINDOW_N, self._view_n * factor))
        if not self._running and self._primary_plot is not None and x is not None:
            # Stopped: zoom around the pointer (X is linked across plots).
            vb = self._primary_plot.getViewBox()
            (x0, x1), _ = vb.viewRange()
            nx0 = x - (x - x0) * factor
            nx1 = x + (x1 - x) * factor
            vb.setXRange(nx0, nx1, padding=0)
        # Running: _redraw() keeps following the latest sample at the new width.

    # -- teardown -------------------------------------------------------------

    def _teardown_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def closeEvent(self, ev) -> None:
        self._timer.stop()
        self._teardown_client()
        super().closeEvent(ev)


def run() -> int:
    """Launch the GUI. Returns a process exit code."""
    # Antialiasing is costly on dense curves; the pyramid already caps point
    # counts near the pixel width, so leave it off for smooth high-rate redraws.
    pg.setConfigOptions(antialias=False)
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(run())
