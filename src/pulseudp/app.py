"""pulseUDP GUI client (PyQt5 + pyqtgraph); design in docs/gui-design.md."""

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

from . import __version__
from .client import LogEvent, UdpClient
from .discovery import Discovery, SsdpDiscovery
from .model import PlotModel, RingBuffer
from .protocol import Descriptor

DEFAULT_PORT = 2102
REDRAW_HZ = 30
DEFAULT_VIEW_N = 500            # samples per screen
DEFAULT_HISTORY_N = 1_000_000   # retained samples

HISTORY_OPTIONS = [
    ("1K", 1_000), ("10K", 10_000), ("100K", 100_000), ("1M", 1_000_000),
    ("10M", 10_000_000), ("100M", 100_000_000), ("1G", 1_000_000_000),
]
RAM_BUDGET_FRACTION = 0.8


def _available_ram_bytes() -> Optional[int]:
    """Free RAM in bytes; None (RAM guard inert) if psutil cannot read it."""
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except Exception:  # noqa: BLE001 - psutil missing or failed
        return None

_PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b",
    "#e377c2", "#7f7f7f", "#bcbd22", "#17becf", "#aec7e8", "#ffbb78",
]


def _load_schema() -> Optional[dict]:
    """Bundled package-data schema, so it works in wheels and frozen builds too."""
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
    devices = QtCore.pyqtSignal(object)     # List[Device] from a Search
    disco_log = QtCore.pyqtSignal(str, str)  # (level, message) discovery progress
    error = QtCore.pyqtSignal(str)


class TelemetryViewBox(pg.ViewBox):
    """Replaces the default ViewBox menu with "Export all…" (docs/gui-design.md, Context menu)."""

    def __init__(self, window: "MainWindow", **kwargs) -> None:
        super().__init__(**kwargs)
        self._win = window
        self._menu = None

    def getMenu(self, ev):
        # raiseContextMenu appends the scene exporter after this item.
        if self._menu is None:
            self._menu = QtWidgets.QMenu()
            act = self._menu.addAction("Export all…")
            act.triggered.connect(self._win.export_all_csv)
        return self._menu


class _DeviceCombo(QtWidgets.QComboBox):
    """Wide preferred, small minimum: the first control to yield width."""

    PREFERRED_W = 320
    MINIMUM_W = 120

    def __init__(self) -> None:
        super().__init__()
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                           QtWidgets.QSizePolicy.Fixed)

    def sizeHint(self) -> QtCore.QSize:
        sh = super().sizeHint()
        sh.setWidth(self.PREFERRED_W)
        return sh

    def minimumSizeHint(self) -> QtCore.QSize:
        msh = super().minimumSizeHint()
        msh.setWidth(self.MINIMUM_W)
        return msh


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, host: str = "127.0.0.1",
                 discovery: Optional[Discovery] = None) -> None:
        super().__init__()
        self.setWindowTitle("pulseUDP client v{}".format(__version__))

        self._schema = _load_schema()
        # Search runs off the GUI thread, so its logs go through the bridge.
        self._discovery = discovery or SsdpDiscovery(
            on_log=lambda level, msg: self._bridge.disco_log.emit(level, msg))
        self._client: Optional[UdpClient] = None
        self._model: Optional[PlotModel] = None
        self._ring: Optional[RingBuffer] = None
        self._running = False
        # Retention only; the view width is pyqtgraph's X zoom.
        self._history_n = DEFAULT_HISTORY_N

        self._plots: List[pg.PlotItem] = []
        self._visible_plots: List[pg.PlotItem] = []
        self._primary_plot: Optional[pg.PlotItem] = None  # X-axis anchor
        self._group_fields: List[List[int]] = []      # channel indices per plot
        self._link_vb: Optional[pg.ViewBox] = None
        self._curves: Dict[str, pg.PlotDataItem] = {}
        self._bit_offset: Dict[str, int] = {}   # bit trace key -> stack row

        # Checkbox semantics per version: docs/gui-design.md (UI layout, Telemetry list).
        self._channel_checks: Dict[int, QtWidgets.QTreeWidgetItem] = {}  # field idx -> row
        self._bit_checks: Dict[str, QtWidgets.QTreeWidgetItem] = {}      # bit key -> row
        self._field_trace_keys: Dict[int, List[str]] = {}  # field index -> its trace keys
        self._trace_color: Dict[str, str] = {}             # trace key -> fixed colour
        self._hidden_keys: set = set()  # locally hidden trace keys (both versions)
        self._enabled_channels: Optional[List[bool]] = None   # v2.0 server set; None = all on
        self._selectable = False        # True once a v2.0 session is negotiated
        self._local_filter = False      # True on a v1.0 session (local field hide)
        self._suppress_check = False    # guard against re-entrant checkbox updates
        self._n_channels = 0

        self._bridge = _Bridge()
        self._bridge.log.connect(self._on_log)
        self._bridge.state.connect(self._on_state)
        self._bridge.descriptor.connect(self._on_descriptor)
        self._bridge.channels.connect(self._on_channels_result)
        self._bridge.devices.connect(self._on_devices_result)
        self._bridge.disco_log.connect(self._append_log)
        self._bridge.error.connect(self._on_error)

        self._build_ui()

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._redraw)
        self._timer.start(int(1000 / REDRAW_HZ))

    TREE_W = 280

    _ROLE_KIND = QtCore.Qt.UserRole          # "field" | "bit"
    _ROLE_REF = QtCore.Qt.UserRole + 1       # field index (int) | bit key (str)

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)

        self._conn_box = self._build_connection_bar()
        outer.addWidget(self._conn_box)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        outer.addWidget(splitter, 1)

        tele_box = QtWidgets.QGroupBox("Telemetry")
        tele_layout = QtWidgets.QVBoxLayout(tele_box)
        self._tree = QtWidgets.QTreeWidget()
        self._tree.setHeaderLabels(["", "Name", "Type"])
        # Column 0: native check state, so Qt handles nested-row indentation.
        self._tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self._tree.setColumnWidth(1, 180)
        self._tree.itemChanged.connect(self._on_item_changed)
        tele_layout.addWidget(self._tree)
        splitter.addWidget(tele_box)

        self._graphs = pg.GraphicsLayoutWidget()
        self._graphs.setBackground("w")
        for act in self._graphs.scene().contextMenu:
            if act.text().startswith("Export"):
                act.setText("qtgraph export…")
        splitter.addWidget(self._graphs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        self._build_log_dock()

        # Narrowest width that shows the whole connection bar.
        m = outer.contentsMargins()
        width = self._conn_box.sizeHint().width() + m.left() + m.right()
        self.resize(width, 760)
        splitter.setSizes([self.TREE_W, max(width - self.TREE_W, 400)])

    def _build_connection_bar(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("Connection")
        row = QtWidgets.QHBoxLayout(box)

        self._search_btn = QtWidgets.QPushButton("Search")
        self._search_btn.clicked.connect(self._on_search)
        row.addWidget(self._search_btn)

        self._device_combo = _DeviceCombo()
        self._device_combo.currentIndexChanged.connect(self._on_device_selected)
        row.addWidget(self._device_combo)

        row.addWidget(QtWidgets.QLabel("Address:"))
        self._ip_edit = QtWidgets.QLineEdit("127.0.0.1")
        self._ip_edit.setFixedWidth(140)
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

    def _run_async(self, fn) -> None:
        """Run a (blocking) client transaction off the GUI thread."""
        threading.Thread(target=fn, daemon=True).start()

    def _on_search(self) -> None:
        self._search_btn.setEnabled(False)
        self._search_btn.setText("Searching…")
        self._append_log("info", "Searching for devices (SSDP)…")
        discovery = self._discovery

        def work():
            try:
                devices = discovery.search(timeout=3.0)
                self._bridge.devices.emit(devices)
            except Exception as exc:  # noqa: BLE001 - surface to the log/UI
                self._bridge.error.emit("Search failed: " + str(exc))
                self._bridge.devices.emit([])

        self._run_async(work)

    def _on_devices_result(self, devices) -> None:
        self._search_btn.setEnabled(True)
        self._search_btn.setText("Search")
        self._device_combo.clear()
        if not devices:
            self._append_log("info", "Search found no devices.")
            return
        for d in devices:
            label = "{} ({})".format(d.name, d.address)
            self._device_combo.addItem(label, d.address)
            # Tooltip shows a label the narrowed combo elides.
            self._device_combo.setItemData(
                self._device_combo.count() - 1, label, QtCore.Qt.ToolTipRole)
        self._append_log("info", "Search found {} device(s).".format(len(devices)))

    def _on_device_selected(self, index: int) -> None:
        addr = self._device_combo.itemData(index)
        if addr:
            self._ip_edit.setText(str(addr))

    def _on_connect(self) -> None:
        host = self._ip_edit.text().strip()
        if not host:
            return
        self._teardown_client()
        self._running = False
        self._start_btn.setText("Start")
        self._start_btn.setEnabled(False)
        # Until the new version is known, so a stale box can't push SET_CHANNELS.
        self._selectable = False
        self._local_filter = False
        self._hidden_keys = set()
        self._enabled_channels = None
        self._set_checks_enabled(False)

        # Version is auto-negotiated by the client (RFC §6.1).
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
        """RAM guard; optimistic until the channel count is known (docs/gui-design.md)."""
        avail = _available_ram_bytes()
        if self._model is None or avail is None:
            return True
        need = RingBuffer.footprint_bytes(self._model.columns(), window_n)   # all channels
        return need <= RAM_BUDGET_FRACTION * avail

    def _select_history_value(self, value: int) -> None:
        """Reflect an accepted history size in the combo without re-triggering."""
        idx = next((i for i, (_, v) in enumerate(HISTORY_OPTIONS) if v == value), -1)
        if idx >= 0:
            self._history_combo.blockSignals(True)
            self._history_combo.setCurrentIndex(idx)
            self._history_combo.blockSignals(False)

    def _on_history_changed(self, index: int) -> None:
        value = int(self._history_combo.itemData(index))
        if not self._history_fits(value):
            channels = self._channel_count() or 0
            need = RingBuffer.footprint_bytes(
                self._model.columns() if self._model is not None else [], value)
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

    def _on_descriptor(self, descriptor: Descriptor) -> None:
        self._model = PlotModel(descriptor)
        # The real channel count is known now: clamp history to what fits.
        if not self._history_fits(self._history_n):
            for _, value in reversed(HISTORY_OPTIONS):
                if value <= self._history_n and self._history_fits(value):
                    self._append_log(
                        "info", "History clamped to {} samples to fit free RAM "
                        "({} channels).".format(value, self._channel_count()))
                    self._history_n = value
                    break
            self._select_history_value(self._history_n)
        self._ring = RingBuffer(self._model.columns(), window_n=self._history_n)
        self._n_channels = len(descriptor.fields)
        v2 = (self._client is not None and self._client.version[0] >= 2)
        self._selectable = v2
        self._local_filter = not v2
        self._hidden_keys = set()
        self._enabled_channels = None   # server set arrives via get_channels (v2.0)
        self._build_plots(descriptor)
        self._start_btn.setEnabled(True)
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
        # "error" is not logged here: _on_error already logs the exception.
        if state == "streaming":
            self._running = True
            self._start_btn.setText("Stop")
            self._set_checks_enabled(False)
            self._append_log("info", "Streaming")
        elif state in ("stopped", "disconnected", "error"):
            self._running = False
            self._start_btn.setText("Start")
            self._set_checks_enabled(True)
            if state == "stopped":
                self._append_log("info", "Stopped")
            elif state == "disconnected":
                self._append_log("info", "Disconnected")
        elif state == "connecting":
            self._append_log("info",
                             "Connecting to {}".format(detail) if detail else "Connecting")
        elif state == "connected":
            self._append_log("info",
                             "Connected — {}".format(detail) if detail else "Connected")

    def _on_error(self, message: str) -> None:
        self._append_log("error", message)

    def _on_log(self, ev: LogEvent) -> None:
        self._append_log(ev.level, "[{}] {}".format(ev.category, ev.message))

    def _append_log(self, level: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._log.appendPlainText("{} {:<7} {}".format(stamp, level.upper(), message))

    def _on_telemetry(self, channels: Dict[str, np.ndarray]) -> None:
        # Receiver thread; RingBuffer is thread-safe.
        ring = self._ring
        if ring is not None:
            ring.append(channels)

    def _build_plots(self, descriptor: Descriptor) -> None:
        """Tree once per connection; plots are re-rendered on every show/hide."""
        self._build_channel_tree(descriptor)
        self._render_plots()

    def _build_channel_tree(self, descriptor: Descriptor) -> None:
        """Field rows + per-bit child rows; trace colours are fixed here."""
        prev_suppress = self._suppress_check
        self._suppress_check = True
        try:
            self._build_channel_tree_rows(descriptor)
        finally:
            self._suppress_check = prev_suppress

    def _build_channel_tree_rows(self, descriptor: Descriptor) -> None:
        self._tree.clear()
        self._channel_checks = {}
        self._bit_checks = {}
        self._field_trace_keys = {}
        self._trace_color = {}
        color_i = 0
        index_by_name = {f.name: i for i, f in enumerate(descriptor.fields)}   # = RFC §4 bitmap index
        field_by_name = {f.name: f for f in descriptor.fields}

        for group in self._model.groups:
            if group.kind == "bitfield":
                fi = index_by_name.get(group.title)
                fld = field_by_name.get(group.title)
                bit_keys = []
                for tr in group.traces:
                    self._trace_color[tr.key] = _PALETTE[color_i % len(_PALETTE)]
                    color_i += 1
                    bit_keys.append(tr.key)
                if fi is not None:
                    self._field_trace_keys[fi] = bit_keys
                parent = self._add_field_row(
                    group.title, fld.type if fld else "bitfield", None, fi)
                for tr in group.traces:
                    child = QtWidgets.QTreeWidgetItem(["", tr.label, "bit"])
                    child.setIcon(1, self._swatch(self._trace_color[tr.key]))
                    parent.addChild(child)
                    self._add_bit_check(child, tr.key)
                parent.setExpanded(False)
            else:
                for tr in group.traces:
                    color = _PALETTE[color_i % len(_PALETTE)]
                    color_i += 1
                    self._trace_color[tr.key] = color
                    fi = index_by_name.get(tr.key)
                    if fi is not None:
                        self._field_trace_keys[fi] = [tr.key]
                    fld = field_by_name.get(tr.key)
                    self._add_field_row(tr.label, fld.type if fld else "", color, fi)

        self._tree.expandToDepth(0)
        self._sync_bit_enables()

    def _channel_on(self, field_index: Optional[int]) -> bool:
        """Streaming per the v2.0 server set; always True in v1.0 or before the set is known."""
        enabled = self._enabled_channels
        if enabled is None or field_index is None:
            return True
        return field_index < len(enabled) and bool(enabled[field_index])

    def _render_plots(self, preserve_view: bool = True) -> None:
        """(Re)build plots for drawn traces only; ``preserve_view`` keeps the X pan/zoom."""
        if self._model is None:
            return
        saved_x = None
        if preserve_view and self._primary_plot is not None:
            try:
                saved_x = self._primary_plot.getViewBox().viewRange()[0]
            except Exception:  # noqa: BLE001 - plot already gone
                saved_x = None

        self._graphs.clear()
        self._plots = []
        self._group_fields = []
        self._curves = {}
        self._bit_offset = {}
        self._link_vb = None
        index_by_name = {f.name: i for i, f in enumerate(self._model.descriptor.fields)}

        row = 0
        for group in self._model.groups:
            # Bit keys map to no index; a disabled bitfield is dropped by the gfis check.
            visible = [tr for tr in group.traces
                       if tr.key not in self._hidden_keys
                       and self._channel_on(index_by_name.get(tr.key))]
            if not visible:
                continue
            if group.kind == "bitfield":
                gfis = [index_by_name.get(group.title)]
            else:
                gfis = [index_by_name.get(tr.key) for tr in visible]
            gfis = [i for i in gfis if i is not None]
            if not any(self._channel_on(i) for i in gfis):
                continue

            plot = self._graphs.addPlot(
                row=row, col=0, viewBox=TelemetryViewBox(self))
            row += 1
            plot.setMenuEnabled(False, None)
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
            vb.setMouseEnabled(x=True, y=False)
            self._plots.append(plot)
            self._group_fields.append(gfis)

            if group.kind == "bitfield":
                ticks = []
                for bi, tr in enumerate(visible):
                    self._bit_offset[tr.key] = bi
                    curve = plot.plot(
                        pen=pg.mkPen(self._trace_color[tr.key], width=2),
                        name=tr.label)
                    self._curves[tr.key] = curve
                    ticks.append((bi, tr.label))
                plot.getAxis("left").setTicks([ticks])
                vb.enableAutoRange(x=False, y=False)
                vb.setYRange(-0.5, max(1, len(visible)) - 0.5 + 0.8)
            else:
                for tr in visible:
                    curve = plot.plot(
                        pen=pg.mkPen(self._trace_color[tr.key], width=2),
                        name=tr.label)
                    self._curves[tr.key] = curve

        self._visible_plots = list(self._plots)
        self._primary_plot = self._plots[0] if self._plots else None
        if self._primary_plot is not None:
            x_range = saved_x if saved_x is not None else (0, DEFAULT_VIEW_N)
            self._primary_plot.getViewBox().setXRange(*x_range, padding=0)

    def _add_field_row(self, name: str, type_str: str, color: Optional[str],
                       field_index: Optional[int]) -> QtWidgets.QTreeWidgetItem:
        item = QtWidgets.QTreeWidgetItem(["", name, type_str])
        if color:
            item.setIcon(1, self._swatch(color))
        self._tree.addTopLevelItem(item)
        item.setData(0, self._ROLE_KIND, "field")
        item.setData(0, self._ROLE_REF, field_index)
        item.setToolTip(0, self._field_check_tip())
        self._set_checkable(item, checked=True, enabled=self._field_check_enabled())
        if field_index is not None:
            self._channel_checks[field_index] = item
        return item

    def _add_bit_check(self, item: QtWidgets.QTreeWidgetItem, key: str) -> None:
        item.setData(0, self._ROLE_KIND, "bit")
        item.setData(0, self._ROLE_REF, key)
        item.setToolTip(0, "Show/hide this flag on the graph (local; not sent to the server)")
        self._set_checkable(item, checked=True, enabled=self._client is not None)
        self._bit_checks[key] = item

    @staticmethod
    def _checked(item: QtWidgets.QTreeWidgetItem) -> bool:
        return item.checkState(0) == QtCore.Qt.Checked

    def _set_checkable(self, item: QtWidgets.QTreeWidgetItem,
                       checked: Optional[bool] = None,
                       enabled: Optional[bool] = None) -> None:
        """Callers hold ``_suppress_check``: these mutations emit ``itemChanged``."""
        flags = item.flags() | QtCore.Qt.ItemIsUserCheckable
        if enabled is not None:
            flags = (flags | QtCore.Qt.ItemIsEnabled if enabled
                     else flags & ~QtCore.Qt.ItemIsEnabled)
        item.setFlags(flags)
        if checked is not None:
            item.setCheckState(0, QtCore.Qt.Checked if checked else QtCore.Qt.Unchecked)

    def _field_check_enabled(self) -> bool:
        if self._selectable:
            return not self._running
        return self._local_filter

    def _field_check_tip(self) -> str:
        if self._selectable:
            return "Enable/disable this channel (stop the stream to change)"
        if self._local_filter:
            return "Show/hide this channel on the graphs"
        return "Channel selection is available after connecting"

    def _set_checks_enabled(self, on: bool) -> None:
        """``on`` gates v2.0 field boxes only; v1.0 and flag boxes ignore it."""
        prev = self._suppress_check
        self._suppress_check = True
        try:
            field_on = bool(on) if self._selectable else self._local_filter
            for item in self._channel_checks.values():
                self._set_checkable(item, enabled=field_on)
            self._sync_bit_enables()
        finally:
            self._suppress_check = prev

    def _sync_bit_enables(self) -> None:
        """A flag box is live while connected and its parent field box is checked."""
        prev = self._suppress_check
        self._suppress_check = True
        try:
            connected = self._client is not None
            for fi, keys in self._field_trace_keys.items():
                parent = self._channel_checks.get(fi)
                parent_on = self._checked(parent) if parent is not None else True
                for key in keys:
                    item = self._bit_checks.get(key)
                    if item is not None:
                        self._set_checkable(item, enabled=connected and parent_on)
        finally:
            self._suppress_check = prev

    def _recompute_hidden(self) -> None:
        hidden: set = set()
        if self._local_filter:
            for fi, item in self._channel_checks.items():
                if not self._checked(item):
                    hidden.update(self._field_trace_keys.get(fi, []))
        for key, item in self._bit_checks.items():
            if not self._checked(item):
                hidden.add(key)
        self._hidden_keys = hidden

    def _on_item_changed(self, item: QtWidgets.QTreeWidgetItem, column: int) -> None:
        if self._suppress_check or column != 0:
            return
        kind = item.data(0, self._ROLE_KIND)
        if kind == "field":
            self._on_field_toggled()
        elif kind == "bit":
            self._on_bit_toggled()

    def _on_field_toggled(self) -> None:
        if self._selectable:
            if self._running or self._client is None:
                return
            desired = [True] * self._n_channels
            for idx, item in self._channel_checks.items():
                desired[idx] = self._checked(item)
            self._set_checks_enabled(False)  # locked during the round trip
            client = self._client

            def work():
                try:
                    accepted = client.set_channels(desired, timeout=1.0, retries=3)
                    self._bridge.channels.emit(accepted)
                except Exception as exc:  # noqa: BLE001
                    self._bridge.error.emit(str(exc))

            self._run_async(work)
        elif self._local_filter:
            self._recompute_hidden()
            self._sync_bit_enables()
            self._render_plots()

    def _on_bit_toggled(self) -> None:
        self._recompute_hidden()
        self._render_plots()

    def _on_channels_result(self, enabled) -> None:
        """Reflect the server's authoritative v2.0 enabled set."""
        self._enabled_channels = list(enabled)
        self._suppress_check = True
        try:
            for idx, item in self._channel_checks.items():
                item.setCheckState(0, QtCore.Qt.Checked
                                   if idx < len(enabled) and enabled[idx]
                                   else QtCore.Qt.Unchecked)
        finally:
            self._suppress_check = False
        if self._model is not None:     # packet layout changed: store only the enabled set
            self._ring = RingBuffer(self._model.columns(enabled), window_n=self._history_n)
        self._render_plots(preserve_view=False)
        self._set_checks_enabled(not self._running)

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
        # Pixel width bounds the decimated point count.
        px = vb.width()
        px = int(px) if px and px > 1 else 1000
        if self._running:
            # Follow latest, keeping the wheel-zoomed width.
            (vx0, vx1), _ = vb.viewRange()
            width = max(1.0, vx1 - vx0)
            x1 = float(latest)
            x0 = x1 - width
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
                    # Envelope: min then max per bucket, so spikes survive.
                    hi = view.ymax[key]
                    xx = np.repeat(view.x, 2)
                    yy = np.empty(lo.size * 2, dtype=np.float64)
                    yy[0::2] = lo
                    yy[1::2] = hi
                    if key in self._bit_offset:
                        yy = yy * 0.8 + self._bit_offset[key]
                    curve.setData(xx, yy)

        if self._running:
            vb.setXRange(x0, x1, padding=0)

    def export_all_csv(self) -> None:
        """Drawn traces' whole retained window to CSV (format: docs/gui-design.md)."""
        if self._ring is None or not self._curves:
            self._append_log("info", "Nothing to export yet.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export all visible data", "pulseudp_export.csv",
            "CSV files (*.csv);;All files (*)")
        if not path:
            return
        ring = self._ring
        model = self._model
        drawn = list(self._curves.keys())

        def work():
            try:
                x, chans = ring.snapshot()
                keys = [k for k in drawn if k in chans]
                # Structured rows keep each column's native dtype (exact int64 counters).
                data = np.empty(x.size, dtype=[("sample", x.dtype)] + [
                    ("c{}".format(i), chans[k].dtype) for i, k in enumerate(keys)])
                data["sample"] = x
                for i, k in enumerate(keys):
                    data["c{}".format(i)] = chans[k]
                fmt = ["%d"] + [model.trace_format(k) for k in keys]
                np.savetxt(path, data, fmt=fmt, delimiter="\t",
                           header="\t".join(["sample"] + keys), comments="")
                self._bridge.log.emit(LogEvent(
                    "export",
                    "Exported {} samples × {} channel(s) to {}".format(
                        data.shape[0], len(keys), path),
                    "info"))
            except Exception as exc:  # noqa: BLE001 - surface to the log/UI
                self._bridge.error.emit("Export failed: " + str(exc))

        self._run_async(work)

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
    """Launch the GUI; returns the exit code."""
    pg.setConfigOptions(antialias=False)
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(run())
