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
DEFAULT_VIEW_S = 1.0        # initial zoom: ~1 s per screen (wheel-controlled)
DEFAULT_HISTORY_S = 10.0    # initial rolling-history retention (spin-box-controlled)
MIN_WINDOW_S = 0.01
MAX_WINDOW_S = 600.0

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
    error = QtCore.pyqtSignal(str)


class TelemetryViewBox(pg.ViewBox):
    """ViewBox whose wheel zoom obeys the running/stopped state machine.

    The wheel always changes the time window by a factor of two. While the
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
        # Two independent time windows: how much data we KEEP (history, set by the
        # spin box) vs how much we SHOW (view, set by the wheel). The wheel never
        # touches retention, so zooming cannot discard data.
        self._history_s = DEFAULT_HISTORY_S
        self._view_s = DEFAULT_VIEW_S

        self._plots: List[pg.PlotItem] = []
        self._link_vb: Optional[pg.ViewBox] = None
        self._curves: Dict[str, pg.PlotDataItem] = {}
        self._bit_offset: Dict[str, int] = {}   # bit trace key -> stack row

        self._bridge = _Bridge()
        self._bridge.log.connect(self._on_log)
        self._bridge.state.connect(self._on_state)
        self._bridge.descriptor.connect(self._on_descriptor)
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

        row.addWidget(QtWidgets.QLabel("History (s):"))
        self._history_spin = QtWidgets.QDoubleSpinBox()
        self._history_spin.setRange(MIN_WINDOW_S, MAX_WINDOW_S)
        self._history_spin.setDecimals(2)
        self._history_spin.setValue(DEFAULT_HISTORY_S)
        self._history_spin.setToolTip(
            "How much recent data to keep (rolling history). "
            "Independent of the wheel zoom — zooming never discards data.")
        self._history_spin.valueChanged.connect(self._on_history_changed)
        row.addWidget(self._history_spin)

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

        # No version selector: the client probes at v1.0 and adopts whatever
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

    def _on_history_changed(self, value: float) -> None:
        # The spin box controls data RETENTION only — never the zoom.
        self._history_s = float(value)
        if self._ring is not None:
            self._ring.set_window(self._history_s)

    # -- bridge slots (GUI thread) --------------------------------------------

    def _on_descriptor(self, descriptor: Descriptor) -> None:
        self._model = PlotModel(descriptor)
        self._ring = RingBuffer(self._model.channel_keys, window_s=self._history_s)
        self._build_plots(descriptor)
        self._start_btn.setEnabled(True)

    def _on_state(self, state: str, detail: str) -> None:
        label = state.capitalize()
        if detail:
            label += " — " + detail
        self._status.setText(label)
        if state == "streaming":
            self._running = True
            self._start_btn.setText("Stop")
        elif state in ("stopped", "disconnected", "error"):
            self._running = False
            self._start_btn.setText("Start")

    def _on_error(self, message: str) -> None:
        self._append_log("error", message)
        self._status.setText("Error — " + message)

    def _on_log(self, ev: LogEvent) -> None:
        self._append_log(ev.level, "[{}] {}".format(ev.category, ev.message))

    def _append_log(self, level: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._log.appendPlainText("{} {:<7} {}".format(stamp, level.upper(), message))

    # -- telemetry ingest (receiver thread) -----------------------------------

    def _on_telemetry(self, packets: np.ndarray) -> None:
        # Runs on the client's receiver thread; RingBuffer is thread-safe.
        if self._model is None or self._ring is None:
            return
        t, flat = self._model.extract(packets)
        self._ring.append(t, flat)

    # -- plot building & redraw -----------------------------------------------

    def _build_plots(self, descriptor: Descriptor) -> None:
        self._graphs.clear()
        self._tree.clear()
        self._plots = []
        self._curves = {}
        self._bit_offset = {}
        self._link_vb = None
        color_i = 0
        ts_name = descriptor.timestamp_field.name

        # Telemetry tree: timestamp first (as time base, no checkbox), then fields.
        ts_item = QtWidgets.QTreeWidgetItem(
            ["", ts_name + "  (time base)", descriptor.timestamp_field.type])
        ts_item.setForeground(1, QtGui.QBrush(QtGui.QColor("#888")))
        self._tree.addTopLevelItem(ts_item)

        field_by_name = {f.name: f for f in descriptor.fields}
        row = 0
        for gi, group in enumerate(self._model.groups):
            plot = self._graphs.addPlot(
                row=gi, col=0, viewBox=TelemetryViewBox(self))
            plot.showGrid(x=True, y=True, alpha=0.3)
            plot.setLabel("bottom", "time", units="s")
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
                                             fld.type if fld else "bitfield", None)
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
                    self._add_field_row(tr.label, fld.type if fld else "", color)

        self._tree.expandToDepth(0)

    def _add_field_row(self, name: str, type_str: str,
                       color: Optional[str]) -> QtWidgets.QTreeWidgetItem:
        """Add a top-level field row with a disabled (greyed) checkbox.

        Only the checkbox is disabled — it is reserved for the future
        field-selection feature — while the name/type text stays enabled and
        normally coloured.
        """
        item = QtWidgets.QTreeWidgetItem(["", name, type_str])
        if color:
            item.setIcon(1, self._swatch(color))
        self._tree.addTopLevelItem(item)
        check = QtWidgets.QCheckBox()
        check.setChecked(True)
        check.setEnabled(False)      # greyed; the row text remains enabled
        check.setToolTip("Reserved for future telemetry-field selection")
        self._tree.setItemWidget(item, 0, check)
        return item

    @staticmethod
    def _swatch(color: str) -> QtGui.QIcon:
        pix = QtGui.QPixmap(12, 12)
        pix.fill(QtGui.QColor(color))
        return QtGui.QIcon(pix)

    def _redraw(self) -> None:
        if self._ring is None or self._model is None:
            return
        t, chans = self._ring.snapshot()
        if t.size == 0:
            return
        for key, curve in self._curves.items():
            y = chans.get(key)
            if y is None or y.size != t.size:
                continue
            if key in self._bit_offset:
                curve.setData(t, y * 0.8 + self._bit_offset[key])
            else:
                curve.setData(t, y)
        if self._running and self._plots:
            latest = float(t[-1])
            self._plots[0].getViewBox().setXRange(
                latest - self._view_s, latest, padding=0)

    # -- wheel state machine --------------------------------------------------

    def on_wheel(self, factor: float, x: Optional[float]) -> None:
        # The wheel changes the VIEW width only; it never trims the RingBuffer, so
        # data outside the visible range is retained (up to the history size) and
        # reappears when zooming back out.
        self._view_s = min(MAX_WINDOW_S, max(MIN_WINDOW_S, self._view_s * factor))
        if not self._running and self._plots and x is not None:
            # Stopped: zoom around the pointer (X is linked across plots).
            vb = self._plots[0].getViewBox()
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
    pg.setConfigOptions(antialias=True)
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(run())
