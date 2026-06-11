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

from . import __version__
from .client import LogEvent, UdpClient
from .discovery import Discovery, SsdpDiscovery
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
    devices = QtCore.pyqtSignal(object)     # List[Device] from a Search
    disco_log = QtCore.pyqtSignal(str, str)  # (level, message) discovery progress
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
        self._menu = None   # empty stand-in for the stripped ViewBox menu

    def getMenu(self, ev):
        # Strip the default ViewBox context menu (axis range, mouse mode, axis
        # linking): navigation here is owned by the wheel state machine and the
        # Start/Stop follow logic, so those entries only fight the app. Returning
        # an (empty) menu rather than None keeps raiseContextMenu alive so the
        # scene still appends its "Export…" action — the one item worth keeping.
        if self._menu is None:
            self._menu = QtWidgets.QMenu()
        return self._menu

    def wheelEvent(self, ev, axis=None):
        delta = ev.delta() if hasattr(ev, "delta") else ev.angleDelta().y()
        factor = 0.5 if delta > 0 else 2.0   # wheel up = zoom in = shorter window
        try:
            x = self.mapSceneToView(ev.scenePos()).x()
        except Exception:
            x = None
        self._win.on_wheel(factor, x)
        ev.accept()


class _DeviceCombo(QtWidgets.QComboBox):
    """Discovered-device dropdown.

    It prefers a wide box (so device labels rarely truncate) but is the control
    that gives up width first when the window is narrowed — the Address field
    beside it keeps a fixed width. This is achieved by a wide preferred size with
    a small minimum (the box elides the text and the per-item tooltip still shows
    the full label); a ``Preferred`` policy leaves slack to the trailing stretch
    rather than growing the box past its preferred width.
    """

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
        # SSDP is the GUI default; the Search button probes the LAN for devices.
        # Discovery logs go through the bridge (search runs off the GUI thread).
        self._discovery = discovery or SsdpDiscovery(
            on_log=lambda level, msg: self._bridge.disco_log.emit(level, msg))
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

        # Channel checkboxes drive two different mechanisms depending on the
        # negotiated version:
        #   * v2.0 (server negotiation, RFC §4) — a field-level box per channel
        #     tells the server to start/stop sending it; live only while stopped.
        #   * v1.0 (local visibility) — the server channel list is immutable, so
        #     the boxes instead show/hide curves on the graphs client-side. Both
        #     field-level boxes and per-bit ("flag") boxes are live, even while
        #     streaming, and removing every trace of a plot removes the plot.
        # The checkboxes are native tree-item check states (drawn by the item
        # delegate, so indentation/margins are handled by Qt), keyed by the same
        # field index / bit trace key as before; the values are the rows themselves.
        self._channel_checks: Dict[int, QtWidgets.QTreeWidgetItem] = {}  # field idx -> row
        self._bit_checks: Dict[str, QtWidgets.QTreeWidgetItem] = {}      # bit key -> row
        self._field_trace_keys: Dict[int, List[str]] = {}  # field index -> its trace keys
        self._trace_color: Dict[str, str] = {}             # trace key -> fixed colour
        self._hidden_keys: set = set()  # locally hidden trace keys (both versions)
        # Server-enabled channel set (v2.0 negotiation); None means "all on", which
        # is always the case in v1.0. Orthogonal to _hidden_keys (local show/hide).
        self._enabled_channels: Optional[List[bool]] = None
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

    # -- UI construction ------------------------------------------------------

    TREE_W = 280   # initial width of the telemetry list pane

    # Per-row metadata stored on column 0: what the row's checkbox controls.
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
        # Column 0 holds the show/hide checkbox as a native item check state, so the
        # delegate draws it with the correct indentation for nested (bit) rows and
        # ResizeToContents sizes the column to fit — no manual width/indent math.
        self._tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self._tree.setColumnWidth(1, 180)
        self._tree.itemChanged.connect(self._on_item_changed)
        tele_layout.addWidget(self._tree)
        splitter.addWidget(tele_box)

        self._graphs = pg.GraphicsLayoutWidget()
        self._graphs.setBackground("w")
        splitter.addWidget(self._graphs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        self._build_log_dock()

        # Default window width = the smallest width that shows the connection bar
        # without shortening any control (the device list is the only one that
        # gives up width when narrowed further). Height is a comfortable default.
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
        self._ip_edit.setFixedWidth(140)   # stays put; the device list yields width instead
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

        row.addStretch(1)   # keep the controls left-aligned
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
        # The SSDP probe blocks for a few seconds (multicast wait + HTTP fetches),
        # so it runs off the GUI thread; results return via the bridge.
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
            # Display name + address; the address is the item data the IP field
            # is filled from on selection (see _on_device_selected).
            label = "{} ({})".format(d.name, d.address)
            self._device_combo.addItem(label, d.address)
            # Full label as a per-item tooltip so a name truncated by the combo
            # width is still readable on hover (Qt shows it for the row).
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
        # Tear down any previous session.
        self._teardown_client()
        self._running = False
        self._start_btn.setText("Start")
        self._start_btn.setEnabled(False)
        # Neutralize channel selection until the new descriptor/version is known
        # (avoids a stale checkbox from the old session pushing SET_CHANNELS).
        self._selectable = False
        self._local_filter = False
        self._hidden_keys = set()
        self._enabled_channels = None
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
        # Server-side channel negotiation is a v2.0 feature; in v1.0 the list is
        # immutable on the wire, so the boxes drive local show/hide instead.
        v2 = (self._client is not None and self._client.version[0] >= 2)
        self._selectable = v2
        self._local_filter = not v2
        self._hidden_keys = set()
        self._enabled_channels = None   # server set arrives via get_channels (v2.0)
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
        # Connection lifecycle is reported through the log. Only states the log
        # does not already cover are surfaced here — an "error" state is skipped
        # because the matching raised exception is logged by _on_error, and
        # "negotiated protocol" is already logged by the client (the "connected"
        # line adds descriptor/field info).
        if state == "streaming":
            self._running = True
            self._start_btn.setText("Stop")
            self._set_checks_enabled(False)   # can't change channels mid-stream
            self._append_log("info", "Streaming")
        elif state in ("stopped", "disconnected", "error"):
            self._running = False
            self._start_btn.setText("Start")
            self._set_checks_enabled(True)     # selectable again once stopped
            if state == "stopped":
                self._append_log("info", "Stopped")
            elif state == "disconnected":
                self._append_log("info", "Disconnected")
            # "error": already logged via _on_error / the raised exception.
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
        """Build the channel tree (once) and render the plots for it.

        The tree carries every channel and its checkbox and is built once per
        connection; the plot area is (re)rendered from the currently-visible
        trace subset, so a v1.0 local show/hide only re-renders the plots.
        """
        self._build_channel_tree(descriptor)
        self._render_plots()

    def _build_channel_tree(self, descriptor: Descriptor) -> None:
        """Populate the telemetry tree: one top-level row per field, plus a child
        row per bit of each bitfield, each with a show/hide checkbox.

        Trace colours are fixed here (``_trace_color``) so they stay stable in the
        swatches and the curves no matter which subset is currently rendered.
        """
        # Setting check states / flags / icons emits itemChanged; suppress so the
        # build does not fire the toggle handlers.
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
        # Field name -> descriptor index (= channel index in the bitmap, RFC §4).
        index_by_name = {f.name: i for i, f in enumerate(descriptor.fields)}
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
        self._sync_bit_enables()   # flags follow their parent channel (both versions)

    def _channel_on(self, field_index: Optional[int]) -> bool:
        """Whether a descriptor channel is currently streaming data.

        True for every channel in v1.0 (and in v2.0 until the server's enabled set
        is known); in v2.0 it reflects the server-negotiated set. A channel that is
        off carries only NaN, so a plot fed solely by off channels is dropped.
        """
        enabled = self._enabled_channels
        if enabled is None or field_index is None:
            return True
        return field_index < len(enabled) and bool(enabled[field_index])

    def _render_plots(self, preserve_view: bool = True) -> None:
        """(Re)build the plot widgets for the currently-drawable traces.

        A trace is drawn unless it is locally hidden (``_hidden_keys`` — a field or
        flag checkbox the user unchecked). A plot is omitted when it has no drawable
        trace, or (v2.0) when every drawable trace's channel is disabled on the
        server, so hiding the last channel/flag of a group removes its graph.

        ``preserve_view`` keeps the current X pan/zoom across the rebuild (the new
        ViewBoxes would otherwise reset to defaults); it is the common case, since
        toggling a checkbox should not jump the view. Cheap enough to call on every
        toggle — a user action, not a per-frame cost.
        """
        if self._model is None:
            return
        # Capture the shared X range before tearing the old ViewBoxes down.
        saved_x = None
        if preserve_view and self._primary_plot is not None:
            try:
                saved_x = self._primary_plot.getViewBox().viewRange()[0]
            except Exception:  # noqa: BLE001 - plot already gone
                saved_x = None

        self._graphs.clear()
        self._plots = []
        self._group_fields = []   # parallel to _plots: channel indices feeding each
        self._curves = {}
        self._bit_offset = {}
        self._link_vb = None
        # Field name -> descriptor index (= channel index in the bitmap, RFC §4).
        index_by_name = {f.name: i for i, f in enumerate(self._model.descriptor.fields)}

        row = 0
        for group in self._model.groups:
            visible = [tr for tr in group.traces if tr.key not in self._hidden_keys]
            if not visible:
                continue   # every channel/flag of this group hidden locally
            # Descriptor channels feeding this plot: a bitfield plot is fed by its
            # one field; a numeric plot by each of its traces' fields.
            if group.kind == "bitfield":
                gfis = [index_by_name.get(group.title)]
            else:
                gfis = [index_by_name.get(tr.key) for tr in visible]
            gfis = [i for i in gfis if i is not None]
            if not any(self._channel_on(i) for i in gfis):
                continue   # v2.0: all feeding channels disabled on the server

            plot = self._graphs.addPlot(
                row=row, col=0, viewBox=TelemetryViewBox(self))
            row += 1
            # Drop the PlotItem ctrl menu (Transforms/Downsample/Average/Alpha/
            # Grid/Points) — decimation is owned by the min/max pyramid and the
            # transforms are meaningless on its envelope. The ViewBox menu is
            # stripped in TelemetryViewBox; together this leaves only "Export…".
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
            self._plots.append(plot)
            self._group_fields.append(gfis)

            if group.kind == "bitfield":
                # Stack visible bits on integer rows; label the y axis with names.
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

        # Everything built here is currently laid out; the X-axis follows the first.
        self._visible_plots = list(self._plots)
        self._primary_plot = self._plots[0] if self._plots else None
        # Restore the pre-rebuild pan/zoom (running redraw re-anchors next frame).
        if saved_x is not None and self._primary_plot is not None:
            self._primary_plot.getViewBox().setXRange(*saved_x, padding=0)

    def _add_field_row(self, name: str, type_str: str, color: Optional[str],
                       field_index: Optional[int]) -> QtWidgets.QTreeWidgetItem:
        """Add a top-level field row with its channel checkbox.

        In v2.0 the box negotiates the channel with the server (live only while
        stopped, RFC §4); in v1.0 it shows/hides the field's curve(s) locally
        (live always, even while streaming).
        """
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
        """Make a bitfield child row a per-bit ("flag") show/hide checkbox.

        Flags are always hidden locally — in both versions — because a bitfield is
        a single channel on the wire, so individual bits are never negotiated with
        the server. The box is live whenever its parent channel is streaming.
        """
        item.setData(0, self._ROLE_KIND, "bit")
        item.setData(0, self._ROLE_REF, key)
        item.setToolTip(0, "Show/hide this flag on the graph (local; not sent to the server)")
        self._set_checkable(item, checked=True, enabled=self._client is not None)
        self._bit_checks[key] = item

    # -- native check-state helpers -------------------------------------------

    @staticmethod
    def _checked(item: QtWidgets.QTreeWidgetItem) -> bool:
        return item.checkState(0) == QtCore.Qt.Checked

    def _set_checkable(self, item: QtWidgets.QTreeWidgetItem,
                       checked: Optional[bool] = None,
                       enabled: Optional[bool] = None) -> None:
        """Set a row's checkbox state/interactivity via native item flags.

        Disabling clears ``ItemIsEnabled`` (the row greys out and the box stops
        accepting clicks) rather than removing the box. Mutations emit
        ``itemChanged``; callers run under ``_suppress_check`` so the handler
        ignores programmatic changes.
        """
        flags = item.flags() | QtCore.Qt.ItemIsUserCheckable
        if enabled is not None:
            flags = (flags | QtCore.Qt.ItemIsEnabled if enabled
                     else flags & ~QtCore.Qt.ItemIsEnabled)
        item.setFlags(flags)
        if checked is not None:
            item.setCheckState(0, QtCore.Qt.Checked if checked else QtCore.Qt.Unchecked)

    # -- channel selection (v2.0 negotiation / v1.0 local show-hide) -----------

    def _field_check_enabled(self) -> bool:
        """Whether field checkboxes are interactive in the current session."""
        if self._selectable:        # v2.0: only while stopped (server round trip)
            return not self._running
        return self._local_filter   # v1.0: always (purely local)

    def _field_check_tip(self) -> str:
        if self._selectable:
            return "Enable/disable this channel (stop the stream to change)"
        if self._local_filter:
            return "Show/hide this channel on the graphs"
        return "Channel selection is available after connecting"

    def _set_checks_enabled(self, on: bool) -> None:
        """Reflect interactivity of the checkboxes.

        ``on`` carries the v2.0 stopped/running gate for the field boxes; v1.0
        field boxes ignore it (local hide is allowed even mid-stream). Flag boxes
        are always local, so they follow only their parent channel's state.
        """
        prev = self._suppress_check
        self._suppress_check = True         # flag mutations re-emit itemChanged
        try:
            field_on = bool(on) if self._selectable else self._local_filter
            for item in self._channel_checks.values():
                self._set_checkable(item, enabled=field_on)
            self._sync_bit_enables()
        finally:
            self._suppress_check = prev

    def _sync_bit_enables(self) -> None:
        """A flag box is live whenever connected and its parent channel is shown.

        Parent "shown" = its field box checked: in v1.0 that means not locally
        hidden, in v2.0 it tracks the server-enabled set (a disabled channel sends
        no data, so hiding its flags is moot).
        """
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
        """Rebuild the locally-hidden trace set from the checkbox states.

        A flag is hidden whenever its own box is unchecked (both versions). A field
        is hidden by its box only in v1.0; in v2.0 the field box drives server
        negotiation (reflected via ``_enabled_channels``), not a local hide.
        """
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
        """A tree row changed: route a user check-state toggle to its handler.

        ``itemChanged`` also fires for programmatic state/flag changes, which run
        under ``_suppress_check``; those are ignored here.
        """
        if self._suppress_check or column != 0:
            return
        kind = item.data(0, self._ROLE_KIND)
        if kind == "field":
            self._on_field_toggled()
        elif kind == "bit":
            self._on_bit_toggled()

    def _on_field_toggled(self) -> None:
        """A field (whole-channel) checkbox changed.

        v2.0: push the new field selection to the server with SET_CHANNELS.
        v1.0: recompute the locally-hidden set and re-render the plots.
        """
        if self._selectable:
            if self._running or self._client is None:
                return
            desired = [True] * self._n_channels
            for idx, item in self._channel_checks.items():
                desired[idx] = self._checked(item)
            self._set_checks_enabled(False)  # lock the boxes during the round trip
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
            self._sync_bit_enables()    # a hidden field greys out its flag boxes
            self._render_plots()

    def _on_bit_toggled(self) -> None:
        """A per-bit ("flag") checkbox changed: hide/show that bit locally.

        Always local in both versions — the bit is never negotiated with the
        server — so it only recomputes the hidden set and re-renders the plots.
        """
        self._recompute_hidden()
        self._render_plots()

    def _on_channels_result(self, enabled) -> None:
        """Reflect the server's authoritative enabled set (v2.0) and re-render."""
        self._enabled_channels = list(enabled)
        self._suppress_check = True         # programmatic update: don't re-emit
        try:
            for idx, item in self._channel_checks.items():
                item.setCheckState(0, QtCore.Qt.Checked
                                   if idx < len(enabled) and enabled[idx]
                                   else QtCore.Qt.Unchecked)
        finally:
            self._suppress_check = False
        if self._ring is not None:
            self._ring.clear()              # packet layout changed; drop stale data
        # Channel set (and so packet layout) changed and the buffer was cleared;
        # start the view fresh rather than preserving a now-empty range.
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
