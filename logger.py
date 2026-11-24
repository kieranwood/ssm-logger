"""Live Subaru SSM logger for the Tactrix OpenPort 2.0 on macOS.

Usage:
    .venv/bin/python logger.py                  # connect to the car
    .venv/bin/python logger.py --demo           # simulated data, no car needed
    .venv/bin/python logger.py --open FILE.csv  # review a saved log (laptop or SD card)

Every parameter the ECU supports is polled continuously; tick channels in the
list to chart them. Record writes all channels to logs/*.csv. Press M (or the
Mark button) to drop a numbered marker into the charts and the CSV - useful
while wiggling connectors. "Fault codes…" reads and clears OBD-II codes.

"Analyse…" runs fixed checks (fuel trim vs temperature and airflow, front vs
rear O2 sensors, MAF glitches, charging voltage, misfires) and lists findings;
double-click one to jump to it. Also: .venv/bin/python analyse.py FILE.csv

Reviewing: press Space (or Pause) to freeze the live charts and look back while
recording carries on, or use "Open recording…" for a saved log. While reviewing:
drag or swipe sideways to move through time, ⌘+scroll to zoom time, and the
cursor line shows every channel's value in the list on the left.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

import analyse
import defs
import dtc
import recording
from recording import Channel
from sources import DemoSource, EcuSource, Source

HISTORY_ROWS = 80_000        # live history kept in memory (~1 hour at 22 samples/s)
LOG_DIR = Path(__file__).resolve().parent / "logs"

DEFAULT_CHANNELS = [
    "Total Fuel Trim", "Air/Fuel Correction #1", "Air/Fuel Learning #1",
    "Air/Fuel Sensor #1 AFR", "Rear O2 Sensor", "Air/Fuel Correction #3",
    "Air/Fuel Sensor #1 Current", "Air/Fuel Sensor #1 Resistance",
    "Air Flow Sensor Voltage", "Engine Speed", "Coolant Temperature",
    "Battery Voltage", "CPC Valve Duty Ratio",
]

COLORS = ["#4e9fff", "#ff8a3d", "#3ecf8e", "#e5484d", "#b97cff", "#f5c542",
          "#2ec4d6", "#ff6fae", "#9aa5b1"]


def build_channels(params: list[defs.Parameter]) -> list[Channel]:
    chans = [Channel(p.name, p.units, min(p.decimals, 3), p.switch,
                     (lambda n: lambda v: v[n])(p.name)) for p in params]
    names = {p.name for p in params}
    derived = []
    if {"Air/Fuel Correction #1", "Air/Fuel Learning #1"} <= names:
        derived.append(Channel("Total Fuel Trim", "%", 2, False,
                               lambda v: v["Air/Fuel Correction #1"] + v["Air/Fuel Learning #1"]))
    if "Air/Fuel Sensor #1" in names:
        derived.append(Channel("Air/Fuel Sensor #1 AFR", "AFR", 2, False,
                               lambda v: v["Air/Fuel Sensor #1"] * 14.7))
    return derived + chans


class CsvRecorder:
    def __init__(self, channels: list[Channel], ecu_id: str):
        LOG_DIR.mkdir(exist_ok=True)
        self.path = LOG_DIR / time.strftime("ssm_%Y%m%d_%H%M%S.csv")
        self.f = open(self.path, "w", newline="")
        self.w = csv.writer(self.f)
        self.f.write(f"# ECU {ecu_id}\n")
        self.w.writerow(["time_s", "mark"] + [c.label for c in channels])
        self.t_start: float | None = None
        self.rows = 0

    def write(self, t: float, values: list[float], mark: int | None) -> None:
        if self.t_start is None:
            self.t_start = t
        self.w.writerow([f"{t - self.t_start:.3f}", mark or ""] +
                        [f"{v:.4g}" for v in values])
        self.rows += 1

    def close(self) -> None:
        self.f.close()


class SeriesBuffer:
    """Growing time-series store; drops the oldest quarter when full."""

    def __init__(self, n_channels: int, capacity: int = HISTORY_ROWS):
        self.t = np.empty(capacity)
        self.y = np.empty((n_channels, capacity))
        self.n = 0

    def append(self, t: float, row: list[float]) -> None:
        if self.n == len(self.t):
            drop = len(self.t) // 4
            self.t[:-drop] = self.t[drop:]
            self.y[:, :-drop] = self.y[:, drop:]
            self.n -= drop
        self.t[self.n] = t
        self.y[:, self.n] = row
        self.n += 1

    @classmethod
    def from_arrays(cls, t: np.ndarray, y: np.ndarray) -> "SeriesBuffer":
        b = cls.__new__(cls)
        b.t, b.y, b.n = t, y, len(t)
        return b

    @property
    def times(self) -> np.ndarray:
        return self.t[:self.n]

    def series(self, i: int) -> np.ndarray:
        return self.y[i, :self.n]


class ScrollingPlots(pg.GraphicsLayoutWidget):
    """Plot area where vertical scrolling moves through the page of charts.

    ⌘ (or Ctrl) + scroll zooms the chart under the cursor instead. When
    reviewing, a sideways trackpad swipe or shift+scroll moves through time.
    """

    def __init__(self, scroll_area: QtWidgets.QScrollArea, on_pan_time):
        super().__init__()
        self.scroll_area = scroll_area
        self.on_pan_time = on_pan_time     # callable(pixels) -> bool (True if handled)

    def wheelEvent(self, ev: QtGui.QWheelEvent) -> None:
        mods = ev.modifiers()
        if mods & (QtCore.Qt.ControlModifier | QtCore.Qt.MetaModifier):
            super().wheelEvent(ev)
            return
        px, ang = ev.pixelDelta(), ev.angleDelta()
        dx = px.x() or ang.x() // 2
        dy = px.y() or ang.y() // 2
        if mods & QtCore.Qt.ShiftModifier and not dx:
            dx, dy = dy, 0
        if abs(dx) > abs(dy) and self.on_pan_time(dx):
            ev.accept()
            return
        bar = self.scroll_area.verticalScrollBar()
        bar.setValue(bar.value() - dy)
        ev.accept()


class DtcDialog(QtWidgets.QDialog):
    """Read and clear OBD-II fault codes through the running source."""

    def __init__(self, source: Source, parent=None):
        super().__init__(parent)
        self.source = source
        self.setWindowTitle("Fault codes")
        self.resize(760, 620)
        lay = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        lay.addWidget(self.tabs)

        def tree(headers, stretch_col):
            t = QtWidgets.QTreeWidget()
            t.setHeaderLabels(headers)
            t.setRootIsDecorated(False)
            t.header().setSectionResizeMode(stretch_col, QtWidgets.QHeaderView.Stretch)
            return t

        self.tree = tree(["Type", "Code", "Description"], 2)
        self.tabs.addTab(self.tree, "Codes")

        ff_page = QtWidgets.QWidget()
        ffl = QtWidgets.QVBoxLayout(ff_page)
        self.ff_label = QtWidgets.QLabel()
        self.ff_label.setWordWrap(True)
        ffl.addWidget(self.ff_label)
        self.ff = tree(["Item", "Value"], 1)
        self.ff.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        ffl.addWidget(self.ff)
        self.tabs.addTab(ff_page, "Freeze frame")

        ready = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(ready)
        rl.addWidget(QtWidgets.QLabel("<b>Since codes were last cleared</b>"))
        self.info = tree(["", ""], 1)
        self.info.setHeaderHidden(True)
        self.info.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        rl.addWidget(self.info, 1)
        rl.addWidget(QtWidgets.QLabel(
            "<b>Readiness monitors</b> - the ECU's self-tests. A fault can only set a "
            "code once the test that checks it has run."))
        self.monitors = tree(["Monitor", "Since cleared", "This drive"], 0)
        rl.addWidget(self.monitors, 2)
        self.tabs.addTab(ready, "Readiness")

        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)
        row = QtWidgets.QHBoxLayout()
        self.read_btn = QtWidgets.QPushButton("Read codes")
        self.read_btn.clicked.connect(self.read)
        self.clear_btn = QtWidgets.QPushButton("Clear codes…")
        self.clear_btn.clicked.connect(self.clear)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.close)
        row.addWidget(self.read_btn)
        row.addWidget(self.clear_btn)
        row.addStretch()
        row.addWidget(close_btn)
        lay.addLayout(row)
        self._future = None
        self._on_done = None
        self._deadline = 0.0
        self._timer = QtCore.QTimer(self, interval=100, timeout=self._poll)
        self.read()

    def _start(self, job, on_done, label: str, timeout_s: float) -> None:
        self.read_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)
        self.msg.setText(label)
        self._future = self.source.submit(job)
        self._on_done = on_done
        self._deadline = time.monotonic() + timeout_s
        self._timer.start()

    def _poll(self) -> None:
        fut = self._future
        if not fut.done():
            if time.monotonic() > self._deadline:
                fut.cancel()
                self._finish()
                self.msg.setText("Timed out - the logger is not connected to the ECU "
                                 f"({self.source.status}).")
            return
        self._finish()
        try:
            self._on_done(fut.result())
        except Exception as e:
            self.msg.setText(f"Error: {e}")

    def _finish(self) -> None:
        self._timer.stop()
        self.read_btn.setEnabled(True)
        self.clear_btn.setEnabled(True)

    def read(self) -> None:
        self._start(lambda req: (dtc.read_all(req), dtc.read_freeze_frame(req), dtc.read_status(req)),
                    self._show, "Reading codes, freeze frame and monitor status…", 45)

    def _show(self, result: tuple[list[dtc.DtcResult], dtc.FreezeFrame, dtc.ObdStatus]) -> None:
        results, ff, status = result
        self.ff.clear()
        if ff.error:
            self.ff_label.setText(f"({ff.error})")
        elif not ff.code:
            self.ff_label.setText("No freeze frame stored. One is saved automatically when a code sets.")
        else:
            self.ff_label.setText(f"Conditions when <b>{ff.code}</b> {dtc.describe(ff.code)} set:")
            for name, value in ff.values:
                QtWidgets.QTreeWidgetItem(self.ff, [name, value])
        self.tabs.setTabText(1, "Freeze frame ●" if ff.code else "Freeze frame")
        self.info.clear()
        for label, value in dtc.status_summary(status):
            QtWidgets.QTreeWidgetItem(self.info, [label, value])
        for err in status.errors or []:
            QtWidgets.QTreeWidgetItem(self.info, ["", f"({err})"])
        self.monitors.clear()
        warn = QtGui.QBrush(QtGui.QColor("#d9822b"))
        for m in status.monitors or []:
            item = QtWidgets.QTreeWidgetItem(self.monitors, [m.name, m.since_clear, m.this_drive])
            for col, val in ((1, m.since_clear), (2, m.this_drive)):
                if val == "NOT complete":
                    item.setForeground(col, warn)
        self.tree.clear()
        total = 0
        for r in results:
            if r.error:
                QtWidgets.QTreeWidgetItem(self.tree, [r.title, "", f"({r.error})"])
            for code in r.codes:
                QtWidgets.QTreeWidgetItem(self.tree, [r.title, code, dtc.describe(code)])
                total += 1
            if not r.error and not r.codes:
                QtWidgets.QTreeWidgetItem(self.tree, [r.title, "", "none"])
        note = ""
        if total or ff.code:
            # Keep a copy: clearing codes erases these for good.
            LOG_DIR.mkdir(exist_ok=True)
            path = LOG_DIR / time.strftime("codes_%Y%m%d_%H%M%S.txt")
            path.write_text(f"ECU {self.source.ecu_id}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                            f"{dtc.format_results(results)}\n\n{dtc.format_freeze_frame(ff)}\n\n"
                            f"{dtc.format_status(status)}\n")
            note = f" Saved a copy to {path.name}."
        self.msg.setText(f"{total} code(s) found. Read at {time.strftime('%H:%M:%S')}.{note}")

    def clear(self) -> None:
        answer = QtWidgets.QMessageBox.warning(
            self, "Clear fault codes?", dtc.CLEAR_WARNING,
            QtWidgets.QMessageBox.Cancel | QtWidgets.QMessageBox.Yes,
            QtWidgets.QMessageBox.Cancel)
        if answer != QtWidgets.QMessageBox.Yes:
            return

        def done(err: str) -> None:
            if err:
                self.msg.setText(f"Clear failed: {err}")
            else:
                self.read()
                self.msg.setText("Codes cleared. Re-reading…")
        self._start(dtc.clear, done, "Clearing codes…", 15)


_open_windows: list[QtWidgets.QMainWindow] = []   # keep review windows alive


def open_recording_dialog(parent: QtWidgets.QWidget | None) -> None:
    start = Path("/Volumes/OPENPORT") if Path("/Volumes/OPENPORT").is_dir() else LOG_DIR
    path, _ = QtWidgets.QFileDialog.getOpenFileName(
        parent, "Open recording", str(start), "CSV logs (*.csv);;All files (*)")
    if path:
        open_recording(path, parent)


def open_recording(path: str | Path, parent: QtWidgets.QWidget | None = None) -> "MainWindow | None":
    try:
        rec = recording.load_csv(path)
    except (OSError, ValueError) as e:
        QtWidgets.QMessageBox.warning(parent, "Could not open recording", f"{path}\n\n{e}")
        return None
    win = MainWindow(rec=rec)
    _open_windows.append(win)
    win.show()
    return win


class AnalyseDialog(QtWidgets.QDialog):
    """Rule-based checks on the data in a window; click a finding to jump to it."""

    COLORS = {analyse.PROBLEM: "#e5484d", analyse.WARNING: "#f5a524",
              analyse.INFO: "#8a94a0", analyse.OK: "#3ecf8e"}

    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Analysis - " + win.windowTitle().split(" - ", 1)[-1])
        self.resize(760, 620)
        lay = QtWidgets.QVBoxLayout(self)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.currentItemChanged.connect(self._selected)
        self.tree.itemDoubleClicked.connect(lambda *_: self._jump())
        split.addWidget(self.tree)
        self.detail = QtWidgets.QTextBrowser()
        split.addWidget(self.detail)
        split.setSizes([380, 200])
        lay.addWidget(split, 1)
        note = QtWidgets.QLabel("Rules of thumb, not a diagnosis. Double-click a finding (or press Show) "
                                "to jump the charts to the times it refers to.")
        note.setStyleSheet("color:#8a94a0;")
        note.setWordWrap(True)
        lay.addWidget(note)
        row = QtWidgets.QHBoxLayout()
        self.show_btn = QtWidgets.QPushButton("Show in charts")
        self.show_btn.clicked.connect(self._jump)
        rerun = QtWidgets.QPushButton("Re-run")
        rerun.clicked.connect(self.run)
        rerun.setVisible(win.live)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.close)
        row.addWidget(self.show_btn)
        row.addWidget(rerun)
        row.addStretch()
        row.addWidget(close)
        lay.addLayout(row)
        self.run()

    def run(self) -> None:
        t, series = self.win.data_snapshot()
        self.findings = analyse.analyse(t, series)
        self.tree.clear()
        for f in self.findings:
            item = QtWidgets.QTreeWidgetItem([f"{analyse.LEVEL_MARK[f.level]}  {f.title}"])
            item.setForeground(0, QtGui.QBrush(QtGui.QColor(self.COLORS[f.level])))
            self.tree.addTopLevelItem(item)
        counts = {lv: sum(f.level == lv for f in self.findings) for lv in analyse.LEVEL_ORDER}
        dur = (t[-1] - t[0]) / 60 if len(t) > 1 else 0
        self.summary.setText(f"<b>{counts['problem']} problem(s), {counts['warning']} warning(s)</b> "
                             f"from {dur:.1f} min of data"
                             + (" (everything in memory, recording or not)" if self.win.live else ""))
        if self.findings:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def _current(self) -> analyse.Finding | None:
        i = self.tree.indexOfTopLevelItem(self.tree.currentItem()) if self.tree.currentItem() else -1
        return self.findings[i] if 0 <= i < len(self.findings) else None

    def _selected(self, *_):
        f = self._current()
        if not f:
            self.detail.clear()
            return
        when = ("<p><b>When:</b> " + analyse._fmt_ranges(f.times, limit=12) + "</p>") if f.times else ""
        self.detail.setHtml(f"<h3 style='color:{self.COLORS[f.level]}'>{f.title}</h3><p>{f.detail}</p>{when}")
        self.show_btn.setEnabled(bool(f.times))

    def _jump(self) -> None:
        f = self._current()
        if f and f.times:
            self.win.show_range(f.times[0][0], f.times[-1][1] if len(f.times) <= 3 else f.times[0][1])


class MainWindow(QtWidgets.QMainWindow):
    """Charts for either a live source or a saved recording."""

    def __init__(self, source: Source | None = None, rec: recording.Recording | None = None):
        super().__init__()
        self.source = source
        self.rec = rec
        self.live = source is not None
        if self.live:
            self.channels = build_channels(source.params)
            self.param_names = [p.name for p in source.params]
            self.buf = SeriesBuffer(len(self.channels))
            self.marks: list[tuple[float, int]] = []
            self.setWindowTitle(f"SSM Logger - ECU {source.ecu_id}")
        else:
            self.channels = rec.channels
            self.buf = SeriesBuffer.from_arrays(rec.t, rec.y)
            self.marks = list(rec.marks)
            self.setWindowTitle(f"SSM Logger - {rec.path.name}")
        self.reviewing = not self.live
        self.pending_mark: int | None = None
        self.recorder: CsvRecorder | None = None
        self.plots: dict[int, tuple[pg.PlotItem, pg.PlotDataItem, pg.InfiniteLine]] = {}
        self._bar_guard = False
        self.cursor_t: float | None = None

        self.resize(1400, 900)
        self._build_ui()
        selected = [i for i, c in enumerate(self.channels) if c.name in DEFAULT_CHANNELS]
        selected.sort(key=lambda i: DEFAULT_CHANNELS.index(self.channels[i].name))
        if not selected:
            selected = [i for i, c in enumerate(self.channels) if not c.is_switch][:8]
        self.selected = selected
        self.table.blockSignals(True)
        for i in selected:
            self.table.item(self.row_of[i], 0).setCheckState(QtCore.Qt.Checked)
        self.table.blockSignals(False)
        self._rebuild_plots()

        if self.live:
            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self._tick)
            self.timer.start(50)
        else:
            self._set_reviewing(True)
            self._fit_all()
            r = self.rec
            dur = r.t[-1] - r.t[0]
            self.status_lbl.setText(f"  {r.path.name}  |  {r.info or 'log'}  |  "
                                    f"{dur / 60:.1f} min, {len(r.t)} rows, {len(self.channels)} channels")

    # ---------------------------------------------------------------- UI setup
    def _build_ui(self) -> None:
        tb = self.addToolBar("Main")
        tb.setMovable(False)
        if self.live:
            self.rec_btn = QtWidgets.QPushButton("● Record")
            self.rec_btn.setCheckable(True)
            self.rec_btn.toggled.connect(self._toggle_record)
            tb.addWidget(self.rec_btn)
            mark_btn = QtWidgets.QPushButton("Mark (M)")
            mark_btn.clicked.connect(self._mark)
            tb.addWidget(mark_btn)
            QtGui.QShortcut(QtGui.QKeySequence("M"), self, activated=self._mark)
            self.pause_btn = QtWidgets.QPushButton("❚❚ Pause (Space)")
            self.pause_btn.setCheckable(True)
            self.pause_btn.toggled.connect(self._set_reviewing)
            tb.addWidget(self.pause_btn)
            QtGui.QShortcut(QtGui.QKeySequence("Space"), self, activated=self.pause_btn.toggle)
            dtc_btn = QtWidgets.QPushButton("Fault codes…")
            dtc_btn.clicked.connect(lambda: DtcDialog(self.source, self).exec())
            tb.addWidget(dtc_btn)
            tb.addSeparator()
        open_btn = QtWidgets.QPushButton("Open recording…")
        open_btn.clicked.connect(lambda: open_recording_dialog(self))
        tb.addWidget(open_btn)
        analyse_btn = QtWidgets.QPushButton("Analyse…")
        analyse_btn.setToolTip("Run fixed checks for fuel trim, sensor and charging problems")
        analyse_btn.clicked.connect(self._analyse)
        tb.addWidget(analyse_btn)
        tb.addSeparator()
        tb.addWidget(QtWidgets.QLabel(" Window: "))
        self.win_combo = QtWidgets.QComboBox()
        for label, secs in (("30 s", 30), ("1 min", 60), ("2 min", 120), ("5 min", 300),
                            ("15 min", 900), ("30 min", 1800), ("1 hour", 3600), ("All", 0)):
            self.win_combo.addItem(label, secs)
        self.win_combo.setCurrentIndex(1 if self.live else self.win_combo.count() - 1)
        self.window_s = self.win_combo.currentData()
        self.win_combo.currentIndexChanged.connect(self._window_changed)
        tb.addWidget(self.win_combo)
        QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Left), self, activated=lambda: self._pan_fraction(-0.25))
        QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Right), self, activated=lambda: self._pan_fraction(0.25))
        tb.addSeparator()
        self.status_lbl = QtWidgets.QLabel()
        tb.addWidget(self.status_lbl)

        # channel table
        side = QtWidgets.QWidget()
        sl = QtWidgets.QVBoxLayout(side)
        sl.setContentsMargins(4, 4, 4, 4)
        self.filter = QtWidgets.QLineEdit(placeholderText="Filter channels…")
        self.filter.textChanged.connect(self._apply_filter)
        sl.addWidget(self.filter)
        self.table = QtWidgets.QTableWidget(len(self.channels), 2)
        self.table.setHorizontalHeaderLabels(["Channel", "Value"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        order = sorted(range(len(self.channels)), key=lambda i: self.channels[i].is_switch)
        self.row_of = {}
        for row, i in enumerate(order):
            c = self.channels[i]
            item = QtWidgets.QTableWidgetItem(c.label)
            item.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Unchecked)
            item.setData(QtCore.Qt.UserRole, i)
            self.table.setItem(row, 0, item)
            val = QtWidgets.QTableWidgetItem("")
            val.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.table.setItem(row, 1, val)
            self.row_of[i] = row
        self.table.itemChanged.connect(self._check_changed)
        sl.addWidget(self.table)

        # plots + time scrollbar
        right = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(2)
        self.plot_scroll = QtWidgets.QScrollArea()
        self.plot_scroll.setWidgetResizable(True)
        self.glw = ScrollingPlots(self.plot_scroll, self._pan_pixels)
        self.glw.setBackground("#111418")
        self.glw.scene().sigMouseMoved.connect(self._mouse_moved)
        self.plot_scroll.setWidget(self.glw)
        rl.addWidget(self.plot_scroll)
        self.time_bar = QtWidgets.QScrollBar(QtCore.Qt.Horizontal)
        self.time_bar.valueChanged.connect(self._bar_moved)
        rl.addWidget(self.time_bar)
        self.hint = QtWidgets.QLabel(
            "  Reviewing - drag or swipe sideways to move through time, ⌘+scroll to zoom, "
            "←/→ to step. The list on the left shows values at the cursor line.")
        self.hint.setStyleSheet("color:#8a94a0;")
        rl.addWidget(self.hint)

        split = QtWidgets.QSplitter()
        split.addWidget(side)
        split.addWidget(right)
        split.setSizes([380, 1020])
        self.setCentralWidget(split)

    def _apply_filter(self, text: str) -> None:
        text = text.lower()
        for row in range(self.table.rowCount()):
            self.table.setRowHidden(row, text not in self.table.item(row, 0).text().lower())

    def _check_changed(self, item: QtWidgets.QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        i = item.data(QtCore.Qt.UserRole)
        checked = item.checkState() == QtCore.Qt.Checked
        if checked and i not in self.selected:
            self.selected.append(i)
        elif not checked and i in self.selected:
            self.selected.remove(i)
        else:
            return
        self._rebuild_plots()

    @property
    def _first(self) -> pg.PlotItem | None:
        return next(iter(self.plots.values()))[0] if self.plots else None

    def _rebuild_plots(self) -> None:
        old_range = self._first.viewRange()[0] if self._first else None
        self.glw.clear()
        self.plots.clear()
        first = None
        for k, i in enumerate(self.selected):
            c = self.channels[i]
            p = self.glw.addPlot(row=k, col=0)
            p.showGrid(x=True, y=True, alpha=0.2)
            p.setMenuEnabled(False)
            p.getAxis("left").setWidth(55)
            p.getAxis("left").enableAutoSIPrefix(False)
            p.setLabel("left", c.units)
            if first is None:
                first = p
                p.sigXRangeChanged.connect(self._xrange_changed)
            else:
                p.setXLink(first)
            if k < len(self.selected) - 1:
                p.getAxis("bottom").setStyle(showValues=False)
            else:
                p.setLabel("bottom", "time", "s")
            curve = p.plot(pen=pg.mkPen(COLORS[k % len(COLORS)], width=1.5), connect="finite")
            curve.setClipToView(True)
            curve.setDownsampling(auto=True, method="peak")
            title = pg.TextItem(anchor=(0, 0), color="#d0d6de")
            title.setParentItem(p.vb)
            title.setPos(6, 2)
            p._title_item = title
            if c.name in ("Air/Fuel Correction #1", "Total Fuel Trim"):
                p.addLine(y=10, pen=pg.mkPen("#e5484d", style=QtCore.Qt.DashLine))
                p.addLine(y=-10, pen=pg.mkPen("#e5484d", style=QtCore.Qt.DashLine))
            for t, m in self.marks:
                self._add_mark_line(p, t, m)
            cursor = pg.InfiniteLine(angle=90, pen=pg.mkPen("#d0d6de", width=1, style=QtCore.Qt.DotLine))
            cursor.setVisible(False)
            p.addItem(cursor, ignoreBounds=True)
            self.plots[i] = (p, curve, cursor)
            self._apply_mode(p, cursor)
        self.glw.setMinimumHeight(max(1, len(self.selected)) * 110)
        self._refresh_curves()
        if old_range and self._first:
            self._first.setXRange(*old_range, padding=0)
        self._update_values()

    def _apply_mode(self, p: pg.PlotItem, cursor: pg.InfiniteLine) -> None:
        if self.reviewing:
            p.setMouseEnabled(x=True, y=False)     # drag = move through time, ⌘+scroll = zoom time
            p.enableAutoRange(axis="y")
            p.setAutoVisible(y=True)               # fit y to the visible time span
        else:
            p.setMouseEnabled(x=False, y=True)     # time follows live data; ⌘+scroll zooms y
            p.enableAutoRange(axis="y")
            p.setAutoVisible(y=False)
        cursor.setVisible(self.reviewing and self.cursor_t is not None)

    def _add_mark_line(self, plot: pg.PlotItem, t: float, m: int) -> None:
        plot.addItem(pg.InfiniteLine(t, angle=90, label=str(m),
                                     pen=pg.mkPen("#f5c542", width=1),
                                     labelOpts={"position": 0.9, "color": "#f5c542"}))

    # ---------------------------------------------------------------- review mode
    def _set_reviewing(self, on: bool) -> None:
        self.reviewing = on
        if not on:
            self.cursor_t = None
        for p, _, cursor in self.plots.values():
            self._apply_mode(p, cursor)
        self.time_bar.setVisible(on)
        self.hint.setVisible(on)
        if self.live:
            self.pause_btn.setText("▶ Back to live (Space)" if on else "❚❚ Pause (Space)")
            self.pause_btn.setStyleSheet("background:#f5c542;color:black;" if on else "")
        self._sync_bar()
        self._update_values()

    def _view(self) -> tuple[float, float]:
        return tuple(self._first.viewRange()[0]) if self._first else (0.0, 1.0)

    def _set_view(self, x0: float, x1: float) -> None:
        if self._first:
            self._first.setXRange(x0, x1, padding=0)

    def _bounds(self) -> tuple[float, float]:
        t = self.buf.times
        return (float(t[0]), float(t[-1])) if len(t) else (0.0, 1.0)

    def _fit_all(self) -> None:
        lo, hi = self._bounds()
        self._set_view(lo, max(hi, lo + 1))

    def _window_changed(self, i: int) -> None:
        self.window_s = self.win_combo.itemData(i)
        if not self.window_s:                       # "All"
            self._fit_all()
        elif self.reviewing:
            x0, x1 = self._view()
            mid = self.cursor_t if self.cursor_t is not None else (x0 + x1) / 2
            self._set_view(mid - self.window_s / 2, mid + self.window_s / 2)

    def _pan_seconds(self, dt: float) -> None:
        x0, x1 = self._view()
        lo, hi = self._bounds()
        width = x1 - x0
        if width >= hi - lo:          # everything is already in view
            return
        x0 = min(max(x0 + dt, lo - width * 0.05), hi - width * 0.95)
        self._set_view(x0, x0 + width)

    def _pan_pixels(self, dx: int) -> bool:
        if not self.reviewing or not self._first:
            return False
        x0, x1 = self._view()
        px = max(1.0, self._first.vb.width())
        self._pan_seconds(-dx * (x1 - x0) / px)
        return True

    def _pan_fraction(self, f: float) -> None:
        if self.reviewing:
            x0, x1 = self._view()
            self._pan_seconds(f * (x1 - x0))

    def _xrange_changed(self, *_):
        self._sync_bar()
        if self.reviewing and self.cursor_t is None:
            self._update_values()

    def _sync_bar(self) -> None:
        if not self.reviewing:
            return
        lo, hi = self._bounds()
        x0, x1 = self._view()
        width = x1 - x0
        self._bar_guard = True
        self.time_bar.setRange(0, max(0, int((hi - lo - width) * 10)))
        self.time_bar.setPageStep(max(1, int(width * 10)))
        self.time_bar.setSingleStep(max(1, int(width)))
        self.time_bar.setValue(int((x0 - lo) * 10))
        self._bar_guard = False

    def _bar_moved(self, v: int) -> None:
        if self._bar_guard or not self.reviewing:
            return
        x0, x1 = self._view()
        lo, _ = self._bounds()
        self._set_view(lo + v / 10, lo + v / 10 + (x1 - x0))

    def _mouse_moved(self, pos: QtCore.QPointF) -> None:
        if not self.reviewing or not self.plots:
            return
        if not any(p.sceneBoundingRect().contains(pos) for p, _, _ in self.plots.values()):
            return
        self.cursor_t = self._first.vb.mapSceneToView(pos).x()
        for _, _, cursor in self.plots.values():
            cursor.setPos(self.cursor_t)
            cursor.setVisible(True)
        self._update_values()

    # ---------------------------------------------------------------- display
    def _refresh_curves(self) -> None:
        t = self.buf.times
        for i, (p, curve, _) in self.plots.items():
            curve.setData(t, self.buf.series(i))

    def _value_index(self) -> int | None:
        t = self.buf.times
        if not len(t):
            return None
        if not self.reviewing:
            return len(t) - 1
        at = self.cursor_t if self.cursor_t is not None else self._view()[1]
        return int(min(max(np.searchsorted(t, at), 0), len(t) - 1))

    def _update_values(self) -> None:
        idx = self._value_index()
        if idx is None:
            return
        self.table.blockSignals(True)
        for i, c in enumerate(self.channels):
            v = self.buf.y[i, idx]
            text = "" if np.isnan(v) else (("ON" if v else "off") if c.is_switch else f"{v:.{c.decimals}f}")
            self.table.item(self.row_of[i], 1).setText(text)
        self.table.blockSignals(False)
        for i, (p, _, _) in self.plots.items():
            c = self.channels[i]
            v = self.buf.y[i, idx]
            p._title_item.setText(f"{c.name}   {'' if np.isnan(v) else f'{v:.{c.decimals}f}'} {c.units}")
        if self.reviewing:
            self.statusBar().showMessage(f"Cursor at {self.buf.times[idx]:.2f} s")

    # ---------------------------------------------------------------- analysis
    def data_snapshot(self) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        t = self.buf.times.copy()
        return t, {c.name: self.buf.series(i).copy() for i, c in enumerate(self.channels)}

    def _analyse(self) -> None:
        if not self.buf.n:
            self.statusBar().showMessage("No data yet", 5000)
            return
        dlg = AnalyseDialog(self)
        dlg.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        dlg.show()                                  # non-modal: charts stay usable

    def show_range(self, a: float, b: float) -> None:
        """Switch to review mode and frame the time range a..b."""
        if self.live and not self.reviewing:
            self.pause_btn.setChecked(True)
        pad = max(5.0, (b - a) * 0.15)
        self._set_view(a - pad, b + pad)
        self.cursor_t = a
        for _, _, cursor in self.plots.values():
            cursor.setPos(a)
            cursor.setVisible(True)
        self._update_values()

    # ---------------------------------------------------------------- live actions
    def _toggle_record(self, on: bool) -> None:
        if on:
            self.recorder = CsvRecorder(self.channels, self.source.ecu_id)
            self.rec_btn.setText("■ Stop")
            self.rec_btn.setStyleSheet("background:#e5484d;color:white;")
        elif self.recorder:
            self.recorder.close()
            self.statusBar().showMessage(f"Saved {self.recorder.rows} rows to {self.recorder.path}", 15000)
            self.recorder = None
            self.rec_btn.setText("● Record")
            self.rec_btn.setStyleSheet("")

    def _mark(self) -> None:
        if not self.live or not self.buf.n:
            return
        m = len(self.marks) + 1
        self.pending_mark = m
        t = float(self.buf.times[-1])
        self.marks.append((t, m))
        for p, _, _ in self.plots.values():
            self._add_mark_line(p, t, m)
        self.statusBar().showMessage(f"Mark {m} at {t:.1f} s", 5000)

    def _tick(self) -> None:
        samples = self.source.drain()
        for t, values in samples:
            named = dict(zip(self.param_names, values))
            row = [c.compute(named) for c in self.channels]
            self.buf.append(t, row)
            if self.recorder:
                self.recorder.write(t, row, self.pending_mark)
            self.pending_mark = None

        rec = f"  |  REC {self.recorder.rows} rows" if self.recorder else ""
        paused = "  |  PAUSED" if self.reviewing else ""
        self.status_lbl.setText(f"  ECU {self.source.ecu_id}  |  {self.source.status}  |  "
                                f"{self.source.rate_hz:.0f} samples/s{rec}{paused}")
        if not samples:
            return
        self._refresh_curves()
        if self.reviewing:
            self._sync_bar()          # the recording keeps growing behind the paused view
        else:
            if self.window_s:
                t_end = float(self.buf.times[-1])
                self._set_view(t_end - self.window_s, t_end)
            else:
                self._fit_all()
            self._update_values()

    def closeEvent(self, e: QtGui.QCloseEvent) -> None:
        if self.recorder:
            self.recorder.close()
        if self.live:
            self.source.stop()
            self.source.join(timeout=3)
        elif self in _open_windows:
            _open_windows.remove(self)
        super().closeEvent(e)


class ConnectDialog(QtWidgets.QDialog):
    """Shown while waiting for the ECU; lets you review a saved log instead."""

    OPENED_RECORDING = 2

    def __init__(self, source: Source):
        super().__init__()
        self.source = source
        self.setWindowTitle("SSM Logger")
        self.setMinimumWidth(460)
        lay = QtWidgets.QVBoxLayout(self)
        self.label = QtWidgets.QLabel("Connecting…")
        self.label.setWordWrap(True)
        self.label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        self.label.setMinimumHeight(self.label.fontMetrics().lineSpacing() * 3 + 4)
        lay.addWidget(self.label)
        busy = QtWidgets.QProgressBar()
        busy.setRange(0, 0)
        lay.addWidget(busy)
        hint = QtWidgets.QLabel("No car? You can still open a saved recording from the laptop or SD card.")
        hint.setStyleSheet("color:#8a94a0;")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        row = QtWidgets.QHBoxLayout()
        open_btn = QtWidgets.QPushButton("Open recording…")
        open_btn.setDefault(True)
        open_btn.clicked.connect(self._open)
        quit_btn = QtWidgets.QPushButton("Quit")
        quit_btn.clicked.connect(self.reject)
        row.addWidget(open_btn)
        row.addStretch()
        row.addWidget(quit_btn)
        lay.addLayout(row)
        self._timer = QtCore.QTimer(self, interval=100, timeout=self._poll)
        self._timer.start()

    def _poll(self) -> None:
        self.label.setText(f"Waiting for the car…\n{self.source.status}")
        if self.source.ready.is_set():
            self.accept()

    def _open(self) -> None:
        start = Path("/Volumes/OPENPORT") if Path("/Volumes/OPENPORT").is_dir() else LOG_DIR
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open recording", str(start), "CSV logs (*.csv);;All files (*)")
        if path and open_recording(path, self):
            self.done(self.OPENED_RECORDING)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="simulated data, no car needed")
    ap.add_argument("--open", metavar="CSV", help="review a saved log instead of connecting")
    ap.add_argument("--port", help="serial port (default: first /dev/cu.usbmodem*)")
    ap.add_argument("--defs", help="path to RomRaider log_defs.xml (default: the copy from fetch_defs.py)")
    args = ap.parse_args()

    if not args.open:
        try:
            defs.find_defs() if not args.defs else None
        except defs.DefinitionsMissing as e:
            print(e, file=sys.stderr)
            return 1

    app = QtWidgets.QApplication(sys.argv)
    pg.setConfigOptions(antialias=True)

    if args.open:
        win = open_recording(args.open)
        return app.exec() if win else 1

    source = DemoSource(args.defs) if args.demo else EcuSource(args.port, args.defs)
    source.start()

    # Wait for the first ECU connection so we know which channels exist,
    # or let the user review a saved recording instead.
    result = ConnectDialog(source).exec()
    if result != QtWidgets.QDialog.Accepted:
        source.stop()
        return app.exec() if result == ConnectDialog.OPENED_RECORDING else 1

    win = MainWindow(source)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
