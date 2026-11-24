"""Live Subaru SSM logger for the Tactrix OpenPort 2.0 (macOS; Linux untested).

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
drag or swipe sideways to move through time, ⌘/Ctrl+scroll to zoom time, and the
cursor line shows every channel's value in the list on the left.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

import analyse
import gauge_view
import profiles
import capture
import defs
import dtc
import guided
import report
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


from html import escape as html_escape


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
        self.monitors = tree(["Monitor", "Since cleared", "This drive", "How to complete"], 3)
        self.monitors.setWordWrap(True)
        for col in range(3):
            self.monitors.header().setSectionResizeMode(col, QtWidgets.QHeaderView.ResizeToContents)
        rl.addWidget(self.monitors, 2)
        self.recheck = QtWidgets.QCheckBox("Recheck every 30 s while this window is open - watch monitors "
                                           "complete as you drive")
        self.recheck.toggled.connect(self._recheck_toggled)
        rl.addWidget(self.recheck)
        self.recheck_lbl = QtWidgets.QLabel("")
        self.recheck_lbl.setWordWrap(True)
        rl.addWidget(self.recheck_lbl)
        self.tabs.addTab(ready, "Readiness")
        self._last_monitors: dict[str, str] = {}
        self.recheck_timer = QtCore.QTimer(self, interval=30_000, timeout=self._recheck)

        tests_page = QtWidgets.QWidget()
        tl = QtWidgets.QVBoxLayout(tests_page)
        self.tests_label = QtWidgets.QLabel()
        self.tests_label.setWordWrap(True)
        tl.addWidget(self.tests_label)
        self.tests = QtWidgets.QTreeWidget()
        self.tests.setHeaderLabels(["Monitor / test", "Value", "Limits", "Result"])
        self.tests.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        tl.addWidget(self.tests)
        self.tabs.addTab(tests_page, "Test results")

        veh = QtWidgets.QWidget()
        vl = QtWidgets.QVBoxLayout(veh)
        self.vehicle = tree(["", ""], 1)
        self.vehicle.setHeaderHidden(True)
        self.vehicle.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        vl.addWidget(self.vehicle, 1)
        iupr_lbl = QtWidgets.QLabel(
            "<b>How often each monitor gets to run</b> - completed: times the monitor ran; chances: "
            "drives where its conditions were met. A low ratio is why a readiness monitor can take a "
            "while to complete.")
        iupr_lbl.setWordWrap(True)
        vl.addWidget(iupr_lbl)
        self.iupr = tree(["Monitor", "Completed", "Chances", "Ratio"], 0)
        vl.addWidget(self.iupr, 1)
        self.tabs.addTab(veh, "Vehicle")

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
        self._start(lambda req: (dtc.read_all(req), dtc.read_freeze_frame(req), dtc.read_status(req),
                                 dtc.read_test_results(req), dtc.read_vehicle_info(req)),
                    self._show, "Reading codes, freeze frame, monitors, test results and vehicle info…", 75)

    def _recheck_toggled(self, on: bool) -> None:
        if on:
            self.recheck_timer.start()
            self._recheck()
        else:
            self.recheck_timer.stop()

    def _recheck(self) -> None:
        if self._future is not None and not self._future.done():
            return                                   # a read is still running
        self._start(dtc.read_status, self._show_status_only, "Checking readiness…", 25)

    def _show_status_only(self, status: dtc.ObdStatus) -> None:
        self._show_monitors(status)
        self.msg.setText(f"Readiness checked at {time.strftime('%H:%M:%S')}.")

    def _show_monitors(self, status: dtc.ObdStatus) -> None:
        self.info.clear()
        for label, value in dtc.status_summary(status):
            QtWidgets.QTreeWidgetItem(self.info, [label, value])
        for err in status.errors or []:
            QtWidgets.QTreeWidgetItem(self.info, ["", f"({err})"])
        self.monitors.clear()
        warn = QtGui.QBrush(QtGui.QColor("#d9822b"))
        done = QtGui.QBrush(QtGui.QColor("#3ecf8e"))
        newly = []
        for m in status.monitors or []:
            hint = dtc.MONITOR_HINTS.get(m.name, "") if m.since_clear == "NOT complete" else ""
            item = QtWidgets.QTreeWidgetItem(self.monitors, [m.name, m.since_clear, m.this_drive, hint])
            item.setToolTip(3, hint)
            for col, val in ((1, m.since_clear), (2, m.this_drive)):
                if val == "NOT complete":
                    item.setForeground(col, warn)
            before = self._last_monitors.get(m.name)
            if before == "NOT complete" and m.since_clear == "complete":
                newly.append(m.name)
                item.setForeground(1, done)
        if newly:
            self.recheck_lbl.setText(f"Completed at {time.strftime('%H:%M')}: {', '.join(newly)}")
        self._last_monitors = {m.name: m.since_clear for m in status.monitors or []}

    def _show_vehicle(self, vi: dtc.VehicleInfo) -> None:
        self.vehicle.clear()
        self.iupr.clear()
        if vi.error:
            QtWidgets.QTreeWidgetItem(self.vehicle, ["", f"({vi.error})"])
            return
        rows = [("VIN", vi.vin), ("ECU", vi.ecu_name)]
        for i, c in enumerate(vi.calibration_ids or []):
            rows.append(("Calibration ID", c + (f"  (CVN {vi.cvns[i]})" if vi.cvns and i < len(vi.cvns) else "")))
        if vi.obd_conditions is not None:
            rows.append(("Qualifying drives / ignition cycles", f"{vi.obd_conditions} / {vi.ignition_cycles}"))
        for a, b in rows:
            if b:
                QtWidgets.QTreeWidgetItem(self.vehicle, [a, b])
        for name, comp, cond in vi.iupr or []:
            QtWidgets.QTreeWidgetItem(self.iupr, [name, str(comp), str(cond), f"{comp / cond:.2f}" if cond else "-"])

    def _show_tests(self, tr: dtc.TestResults) -> None:
        self.tests.clear()
        failed = sum(not t.passed for t in tr.tests)
        near = sum(t.near_limit for t in tr.tests)
        if tr.error:
            self.tests_label.setText(f"({tr.error})")
        elif not tr.tests:
            self.tests_label.setText("No test results reported yet - the ECU runs its tests while driving.")
        else:
            self.tests_label.setText(
                f"The measured value and limits of each self-test the ECU has run (OBD mode 06): "
                f"<b>{len(tr.tests)}</b> tests, <b>{failed}</b> failed, <b>{near}</b> within "
                f"{dtc.NEAR_LIMIT:.0%} of a limit. A value close to its limit is an early warning, "
                "before the test fails and sets a code. Manufacturer tests have no published names.")
        colors = {"FAILED": QtGui.QColor("#e5484d"), "near limit": QtGui.QColor("#d9822b")}
        parents: dict[str, QtWidgets.QTreeWidgetItem] = {}
        for t in tr.tests:
            parent = parents.get(t.monitor)
            if parent is None:
                parent = parents[t.monitor] = QtWidgets.QTreeWidgetItem(self.tests, [t.monitor])
                parent.setExpanded(True)
            item = QtWidgets.QTreeWidgetItem(parent, [t.test, t.fmt(t.value), t.limits, t.status])
            if t.status in colors:
                for col in range(4):
                    item.setForeground(col, QtGui.QBrush(colors[t.status]))
        self.tabs.setTabText(3, "Test results ●" if failed or near else "Test results")

    def _show(self, result) -> None:
        results, ff, status, tests, vehicle = result
        self._show_tests(tests)
        self._show_vehicle(vehicle)
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
        self._show_monitors(status)
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
                            f"{dtc.format_status(status)}\n\n{dtc.format_test_results(tests)}\n\n"
                            f"{dtc.format_vehicle_info(vehicle)}\n")
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
        self.findings = analyse.analyse(t, series, load_profile())
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


def load_capture_settings() -> capture.Settings:
    raw = QtCore.QSettings("ssm-logger", "logger").value("capture", "")
    try:
        return capture.Settings.from_dict(json.loads(raw)) if raw else capture.Settings()
    except (ValueError, TypeError):
        return capture.Settings()


def save_capture_settings(st: capture.Settings) -> None:
    QtCore.QSettings("ssm-logger", "logger").setValue("capture", json.dumps(st.to_dict()))


def load_profile() -> str:
    """The chosen engine profile key, or "auto"."""
    v = QtCore.QSettings("ssm-logger", "logger").value("profile", profiles.AUTO)
    return v if v == profiles.AUTO or v in profiles.BY_KEY else profiles.AUTO


def save_profile(key: str) -> None:
    QtCore.QSettings("ssm-logger", "logger").setValue("profile", key)


def load_alert_settings() -> capture.AlertSettings:
    raw = QtCore.QSettings("ssm-logger", "logger").value("alerts", "")
    try:
        return capture.AlertSettings.from_dict(json.loads(raw)) if raw else capture.AlertSettings()
    except (ValueError, TypeError):
        return capture.AlertSettings()


def save_alert_settings(st: capture.AlertSettings) -> None:
    QtCore.QSettings("ssm-logger", "logger").setValue("alerts", json.dumps(st.to_dict()))


class RulesDialog(QtWidgets.QDialog):
    """Edit a list of channel rules plus a few extra options (auto-capture or alerts)."""

    def __init__(self, title: str, intro: str, rules: list[capture.Rule], channel_names: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 460)
        self.names = channel_names
        lay = QtWidgets.QVBoxLayout(self)
        label = QtWidgets.QLabel(intro)
        label.setWordWrap(True)
        lay.addWidget(label)
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["On", "Channel", "", "Value", "Hold (s)"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        lay.addWidget(self.table)
        for r in rules:
            self._add_row(r)
        row = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton("Add rule")
        add.clicked.connect(lambda: self._add_row(capture.Rule(self.names[0] if self.names else "", ">", 0)))
        rm = QtWidgets.QPushButton("Remove rule")
        rm.clicked.connect(lambda: self.table.removeRow(self.table.currentRow()))
        row.addWidget(add)
        row.addWidget(rm)
        row.addStretch()
        lay.addLayout(row)
        self.form = QtWidgets.QFormLayout()
        lay.addLayout(self.form)
        self._buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        lay.addWidget(self._buttons)

    def _add_row(self, rule: capture.Rule) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        on = QtWidgets.QTableWidgetItem()
        on.setCheckState(QtCore.Qt.Checked if rule.enabled else QtCore.Qt.Unchecked)
        self.table.setItem(r, 0, on)
        ch = QtWidgets.QComboBox()
        ch.setEditable(True)
        ch.addItems(self.names)
        ch.setCurrentText(rule.channel)
        self.table.setCellWidget(r, 1, ch)
        op = QtWidgets.QComboBox()
        op.addItems([">", "<"])
        op.setCurrentText(rule.op)
        self.table.setCellWidget(r, 2, op)
        val = QtWidgets.QDoubleSpinBox(minimum=-1e6, maximum=1e6, decimals=2)
        val.setValue(rule.value)
        self.table.setCellWidget(r, 3, val)
        hold = QtWidgets.QDoubleSpinBox(minimum=0, maximum=600, decimals=1)
        hold.setValue(rule.hold_s)
        self.table.setCellWidget(r, 4, hold)

    def rules(self) -> list[capture.Rule]:
        out = []
        for r in range(self.table.rowCount()):
            name = self.table.cellWidget(r, 1).currentText().strip()
            if name:
                out.append(capture.Rule(channel=name, op=self.table.cellWidget(r, 2).currentText(),
                                        value=self.table.cellWidget(r, 3).value(),
                                        hold_s=self.table.cellWidget(r, 4).value(),
                                        enabled=self.table.item(r, 0).checkState() == QtCore.Qt.Checked))
        return out


class CaptureDialog(RulesDialog):
    """Edit auto-capture triggers and the capture window."""

    def __init__(self, settings: capture.Settings, channel_names: list[str], parent=None):
        super().__init__("Auto-capture",
                         "While auto-capture is on, a trigger saves the data from <b>before</b> it fired "
                         "until <b>after</b> it, as a separate recording in logs/ (…_capture.csv) - "
                         "whether or not you are recording. <b>C</b> captures now.",
                         settings.rules, channel_names, parent)
        self.on_code = QtWidgets.QCheckBox("New stored or pending fault code (checked every "
                                           f"{capture.POLL_CODES_S:.0f} s)")
        self.on_code.setChecked(settings.on_new_code)
        self.form.addRow(self.on_code)
        self.running = QtWidgets.QCheckBox("Ignore channel rules while the engine is off (below 400 rpm)")
        self.running.setChecked(settings.running_only)
        self.form.addRow(self.running)

        def secs(v):
            sp = QtWidgets.QDoubleSpinBox(minimum=1, maximum=600, decimals=0, suffix=" s")
            sp.setValue(v)
            return sp
        self.before = secs(settings.before_s)
        self.after = secs(settings.after_s)
        self.form.addRow("Keep before the trigger", self.before)
        self.form.addRow("Keep after the trigger", self.after)

    def settings(self) -> capture.Settings:
        return capture.Settings(rules=self.rules(), on_new_code=self.on_code.isChecked(),
                                before_s=self.before.value(), after_s=self.after.value(),
                                running_only=self.running.isChecked())


class AlertDialog(RulesDialog):
    def __init__(self, settings: capture.AlertSettings, channel_names: list[str], parent=None):
        super().__init__("Alerts",
                         "While alerts are on, a red banner shows any rule that is currently true "
                         "(after its hold time), with a beep when it starts.",
                         settings.rules, channel_names, parent)
        self.sound = QtWidgets.QCheckBox("Beep when an alert starts")
        self.sound.setChecked(settings.sound)
        self.form.addRow(self.sound)
        self.running = QtWidgets.QCheckBox("Ignore rules while the engine is off (below 400 rpm)")
        self.running.setChecked(settings.running_only)
        self.form.addRow(self.running)

    def settings(self) -> capture.AlertSettings:
        return capture.AlertSettings(rules=self.rules(), sound=self.sound.isChecked(),
                                     running_only=self.running.isChecked())


class GuidedDialog(QtWidgets.QDialog):
    """Pick a guided test, follow its steps, then check the data from Start to Finish."""

    COLORS = AnalyseDialog.COLORS

    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Guided tests")
        self.resize(680, 640)
        self.t_start: float | None = None
        lay = QtWidgets.QVBoxLayout(self)
        self.combo = QtWidgets.QComboBox()
        for test in guided.TESTS:
            self.combo.addItem(test.name, test.key)
        self.combo.currentIndexChanged.connect(self._describe)
        lay.addWidget(self.combo)
        self.text = QtWidgets.QTextBrowser()
        self.text.setMaximumHeight(210)
        lay.addWidget(self.text)
        row = QtWidgets.QHBoxLayout()
        if win.live:
            self.start_btn = QtWidgets.QPushButton("Start")
            self.start_btn.clicked.connect(self._start)
            self.finish_btn = QtWidgets.QPushButton("Finish and check")
            self.finish_btn.setEnabled(False)
            self.finish_btn.clicked.connect(self._finish)
            row.addWidget(self.start_btn)
            row.addWidget(self.finish_btn)
        else:
            check = QtWidgets.QPushButton("Check the visible part of the recording")
            check.clicked.connect(self._check_visible)
            row.addWidget(check)
        self.elapsed = QtWidgets.QLabel("")
        row.addWidget(self.elapsed)
        row.addStretch()
        lay.addLayout(row)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.currentItemChanged.connect(self._selected)
        self.tree.itemDoubleClicked.connect(lambda *_: self._jump())
        lay.addWidget(self.tree, 1)
        self.detail = QtWidgets.QTextBrowser()
        self.detail.setMaximumHeight(140)
        lay.addWidget(self.detail)
        self.findings: list[analyse.Finding] = []
        self.timer = QtCore.QTimer(self, interval=500, timeout=self._tick)
        self._describe()

    def _test(self) -> guided.GuidedTest:
        return guided.BY_KEY[self.combo.currentData()]

    def _describe(self, *_) -> None:
        t = self._test()
        steps = "".join(f"<li>{html_escape(s)}</li>" for s in t.steps)
        self.text.setHtml(f"<p>{html_escape(t.purpose)}</p><ol>{steps}</ol>"
                          f"<p style='color:#8a94a0'>Needs: {html_escape(', '.join(t.needs))}</p>")

    def _start(self) -> None:
        if not self.win.buf.n:
            return
        self.t_start = float(self.win.buf.times[-1])
        self.combo.setEnabled(False)
        self.start_btn.setText("Restart")
        self.finish_btn.setEnabled(True)
        self.tree.clear()
        self.detail.clear()
        self.timer.start()

    def _tick(self) -> None:
        if self.t_start is not None and self.win.buf.n:
            self.elapsed.setText(f"  running for {float(self.win.buf.times[-1]) - self.t_start:.0f} s")

    def _finish(self) -> None:
        self.timer.stop()
        self.combo.setEnabled(True)
        self.start_btn.setText("Start")
        self.finish_btn.setEnabled(False)
        t_end = float(self.win.buf.times[-1])
        self._run(self.t_start, t_end)
        self.t_start = None

    def _check_visible(self) -> None:
        a, b = self.win._view()
        self._run(a, b)

    def _run(self, a: float, b: float) -> None:
        t, series = self.win.data_snapshot()
        m = (t >= a) & (t <= b)
        marks = [mt for mt, _ in self.win.marks if a <= mt <= b]
        self.findings = guided.run(self._test().key, t[m], {k: v[m] for k, v in series.items()}, marks,
                                   load_profile())
        self.elapsed.setText(f"  checked {b - a:.0f} s of data")
        self.tree.clear()
        for f in self.findings:
            item = QtWidgets.QTreeWidgetItem([f"{analyse.LEVEL_MARK[f.level]}  {f.title}"])
            item.setForeground(0, QtGui.QBrush(QtGui.QColor(self.COLORS[f.level])))
            self.tree.addTopLevelItem(item)
        if self.findings:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def _selected(self, *_) -> None:
        i = self.tree.indexOfTopLevelItem(self.tree.currentItem()) if self.tree.currentItem() else -1
        if 0 <= i < len(self.findings):
            f = self.findings[i]
            self.detail.setHtml(f"<b style='color:{self.COLORS[f.level]}'>{html_escape(f.title)}</b>"
                                f"<p>{html_escape(f.detail)}</p>")

    def _jump(self) -> None:
        i = self.tree.indexOfTopLevelItem(self.tree.currentItem()) if self.tree.currentItem() else -1
        if 0 <= i < len(self.findings) and self.findings[i].times:
            ts = self.findings[i].times
            self.win.show_range(ts[0][0], ts[-1][1])


class ReportDialog(QtWidgets.QDialog):
    """Build a shareable HTML report and open it in the browser."""

    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Report")
        lay = QtWidgets.QFormLayout(self)
        self.title = QtWidgets.QLineEdit("Vehicle report")
        lay.addRow("Title", self.title)
        self.ecu = QtWidgets.QCheckBox("Fault codes, freeze frame, monitors, test results and vehicle info from the ECU")
        self.ecu.setChecked(win.live)
        self.ecu.setEnabled(win.live)
        lay.addRow(self.ecu)
        self.vin = QtWidgets.QCheckBox("Include the VIN (left out by default since reports are usually shared)")
        lay.addRow(self.vin)
        self.data = QtWidgets.QCheckBox("Analysis and charts of the logged data")
        self.data.setChecked(True)
        lay.addRow(self.data)
        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
        lay.addRow(self.msg)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel)
        self.go = self.buttons.addButton("Create report", QtWidgets.QDialogButtonBox.AcceptRole)
        self.buttons.accepted.connect(self._create)
        self.buttons.rejected.connect(self.reject)
        lay.addRow(self.buttons)
        self._future = None
        self._deadline = 0.0
        self.timer = QtCore.QTimer(self, interval=100, timeout=self._poll)

    def _create(self) -> None:
        self.go.setEnabled(False)
        if self.ecu.isChecked():
            self.msg.setText("Reading the ECU…")
            self._future = self.win.source.submit(report.gather_ecu)
            self._deadline = time.monotonic() + 90
            self.timer.start()
        else:
            self._write(None)

    def _poll(self) -> None:
        fut = self._future
        if not fut.done():
            if time.monotonic() > self._deadline:
                fut.cancel()
                self.timer.stop()
                self.msg.setText("Timed out - not connected to the ECU.")
                self.go.setEnabled(True)
            return
        self.timer.stop()
        try:
            ecu = fut.result()
        except Exception as e:
            self.msg.setText(f"Error reading the ECU: {e}")
            self.go.setEnabled(True)
            return
        self._write(ecu)

    def _write(self, ecu: dict | None) -> None:
        w = self.win
        kw = {}
        if self.data.isChecked() and w.buf.n:
            t, series = w.data_snapshot()
            kw = dict(t=t, series=series, units={c.name: c.units for c in w.channels},
                      findings=analyse.analyse(t, series, load_profile()))
        ecu_id = w.source.ecu_id if w.live else (w.rec.info.replace("ECU ", "") if w.rec.info.startswith("ECU ") else "")
        src = "live session" if w.live else w.rec.path.name
        page = report.build(self.title.text().strip() or "Vehicle report", ecu_id=ecu_id, source=src, ecu=ecu,
                            include_vin=self.vin.isChecked(), **kw)
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / time.strftime("report_%Y%m%d_%H%M%S.html")
        path.write_text(page, encoding="utf-8")
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))
        w.statusBar().showMessage(f"Report saved to {path}", 15000)
        self.accept()


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
        if self.live:
            self.capture_armed = False
            self.capturer = capture.Capturer(load_capture_settings(), [c.name for c in self.channels])
            self._codes_future = None
            self._codes_next = 0.0
            self.alerter = capture.Alerter(load_alert_settings(), [c.name for c in self.channels])
            self.alerts_on = True
        self.compare: recording.Recording | None = None
        self.compare_shift = 0.0
        self._dtc_dialog = None
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
        self.gauge_panel.load(load_profile())

        if self.live:
            self.hint.setVisible(False)
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
            self.cap_btn = QtWidgets.QPushButton("◎ Auto-capture")
            self.cap_btn.setCheckable(True)
            self.cap_btn.setToolTip("Save the data around a fault automatically (settings with the ⚙ button)")
            self.cap_btn.toggled.connect(self._arm_capture)
            tb.addWidget(self.cap_btn)
            cap_set = QtWidgets.QPushButton("⚙")
            cap_set.setToolTip("Auto-capture triggers and window")
            cap_set.clicked.connect(self._capture_settings)
            tb.addWidget(cap_set)
            cap_now = QtWidgets.QPushButton("Capture now (C)")
            cap_now.clicked.connect(self._capture_now)
            tb.addWidget(cap_now)
            QtGui.QShortcut(QtGui.QKeySequence("C"), self, activated=self._capture_now)
            self.alert_btn = QtWidgets.QPushButton("⚠ Alerts on")
            self.alert_btn.setCheckable(True)
            self.alert_btn.setChecked(True)
            self.alert_btn.setToolTip("Warn when a value crosses a limit (rules under Tools ▸ Alert settings)")
            self.alert_btn.toggled.connect(self._toggle_alerts)
            tb.addWidget(self.alert_btn)
            dtc_btn = QtWidgets.QPushButton("Fault codes…")
            dtc_btn.clicked.connect(self._show_dtc)
            tb.addWidget(dtc_btn)
            tb.addSeparator()
        open_btn = QtWidgets.QPushButton("Open recording…")
        open_btn.clicked.connect(lambda: open_recording_dialog(self))
        tb.addWidget(open_btn)
        analyse_btn = QtWidgets.QPushButton("Analyse…")
        analyse_btn.setToolTip("Run fixed checks for fuel trim, sensor and charging problems")
        analyse_btn.clicked.connect(self._analyse)
        tb.addWidget(analyse_btn)
        self.gauge_btn = QtWidgets.QPushButton("◔ Gauges (G)")
        self.gauge_btn.setCheckable(True)
        self.gauge_btn.setToolTip("Switch between charts and gauges")
        self.gauge_btn.toggled.connect(self._set_gauges)
        tb.addWidget(self.gauge_btn)
        QtGui.QShortcut(QtGui.QKeySequence("G"), self, activated=self.gauge_btn.toggle)
        tools = QtWidgets.QToolButton()
        tools.setText("Tools ▾")
        tools.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        menu = QtWidgets.QMenu(tools)
        menu.addAction("Guided tests…", lambda: GuidedDialog(self).show())
        menu.addAction("Compare with another recording…", self._compare_open)
        menu.addAction("Report…", lambda: ReportDialog(self).exec())
        engine = menu.addMenu("Engine profile")
        group = QtGui.QActionGroup(engine)
        current = load_profile()
        for key, label in profiles.choices():
            act = engine.addAction(label)
            act.setCheckable(True)
            act.setChecked(key == current)
            act.triggered.connect(lambda _=False, k=key: self._set_profile(k))
            group.addAction(act)
        if self.live:
            menu.addSeparator()
            menu.addAction("Auto-capture settings…", self._capture_settings)
            menu.addAction("Alert settings…", self._alert_settings)
        tools.setMenu(menu)
        tb.addWidget(tools)
        self.compare_box = QtWidgets.QWidget()
        cl = QtWidgets.QHBoxLayout(self.compare_box)
        cl.setContentsMargins(6, 0, 0, 0)
        self.compare_lbl = QtWidgets.QLabel()
        self.compare_lbl.setStyleSheet("font-style:italic;")
        cl.addWidget(self.compare_lbl)
        cl.addWidget(QtWidgets.QLabel("shift"))
        self.shift_spin = QtWidgets.QDoubleSpinBox(minimum=-36000, maximum=36000, decimals=1, suffix=" s")
        self.shift_spin.setToolTip("Move the comparison recording in time to line up the same event")
        self.shift_spin.valueChanged.connect(self._compare_shift_changed)
        cl.addWidget(self.shift_spin)
        close_cmp = QtWidgets.QPushButton("✕")
        close_cmp.setToolTip("Stop comparing")
        close_cmp.clicked.connect(self._compare_close)
        cl.addWidget(close_cmp)
        self.compare_box.setVisible(False)
        tb.addWidget(self.compare_box)
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
        self.alert_banner = QtWidgets.QLabel()
        self.alert_banner.setStyleSheet("background:#c62828;color:white;font-weight:600;padding:6px 10px;")
        self.alert_banner.setVisible(False)
        rl.addWidget(self.alert_banner)
        charts_page = QtWidgets.QWidget()
        cp = QtWidgets.QVBoxLayout(charts_page)
        cp.setContentsMargins(0, 0, 0, 0)
        cp.setSpacing(2)
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(charts_page)
        self.gauge_panel = gauge_view.GaugePanel(self)
        self.gauge_panel.changed.connect(self._sync_checks)
        self.stack.addWidget(self.gauge_panel)
        rl.addWidget(self.stack)
        self.plot_scroll = QtWidgets.QScrollArea()
        self.plot_scroll.setWidgetResizable(True)
        self.glw = ScrollingPlots(self.plot_scroll, self._pan_pixels)
        self.glw.setBackground("#111418")
        self.glw.scene().sigMouseMoved.connect(self._mouse_moved)
        self.plot_scroll.setWidget(self.glw)
        cp.addWidget(self.plot_scroll)
        self.time_bar = QtWidgets.QScrollBar(QtCore.Qt.Horizontal)
        self.time_bar.valueChanged.connect(self._bar_moved)
        cp.addWidget(self.time_bar)
        self.hint = QtWidgets.QLabel(
            "  Reviewing - drag or swipe sideways to move through time, ⌘/Ctrl+scroll to zoom, "
            "←/→ to step. The list on the left shows values at the cursor line.")
        self.hint.setStyleSheet("color:#8a94a0;")
        cp.addWidget(self.hint)

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
        if self.gauge_btn.isChecked():
            if checked != (self.channels[i].name in self.gauge_panel.sel):
                self.gauge_panel.toggle(self.channels[i].name)
            return
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
            if self.compare is not None:
                cmp_curve = p.plot(pen=pg.mkPen((255, 255, 255, 150), width=1.2, style=QtCore.Qt.DashLine),
                                   connect="finite")
                cmp_curve.setClipToView(True)
                cmp_curve.setDownsampling(auto=True, method="peak")
                p._compare_curve = cmp_curve
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
            p.setMouseEnabled(x=True, y=False)     # drag = move through time, ⌘/Ctrl+scroll = zoom time
            p.enableAutoRange(axis="y")
            p.setAutoVisible(y=True)               # fit y to the visible time span
        else:
            p.setMouseEnabled(x=False, y=True)     # time follows live data; ⌘/Ctrl+scroll zooms y
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
        self._refresh_compare()

    # ---------------------------------------------------------------- compare
    def _refresh_compare(self) -> None:
        cmp = self.compare
        if cmp is None:
            return
        names = {c.name: k for k, c in enumerate(cmp.channels)}
        for i, (p, _, _) in self.plots.items():
            curve = getattr(p, "_compare_curve", None)
            if curve is None:
                continue
            k = names.get(self.channels[i].name)
            if k is None:
                curve.setData([], [])
            else:
                curve.setData(cmp.t + self.compare_shift, cmp.y[k])

    def _compare_open(self) -> None:
        start = str(LOG_DIR) if LOG_DIR.is_dir() else ""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Compare with recording", start, "CSV files (*.csv)")
        if not path:
            return
        try:
            cmp = recording.load_csv(path)
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.warning(self, "Could not open recording", f"{path}\n\n{e}")
            return
        # line the comparison's start up with this recording's start
        self.compare = cmp
        base = float(self.buf.times[0]) if self.buf.n else 0.0
        self.shift_spin.blockSignals(True)
        self.shift_spin.setValue(round(base - float(cmp.t[0]), 1))
        self.shift_spin.blockSignals(False)
        self.compare_shift = self.shift_spin.value()
        self.compare_lbl.setText(f"Comparing: {cmp.path.name} (dashed)")
        self.compare_box.setVisible(True)
        self._rebuild_plots()

    def _compare_shift_changed(self, v: float) -> None:
        self.compare_shift = v
        self._refresh_compare()

    def _compare_close(self) -> None:
        self.compare = None
        self.compare_box.setVisible(False)
        self._rebuild_plots()

    def _value_index(self) -> int | None:
        t = self.buf.times
        if not len(t):
            return None
        if not self.reviewing:
            return len(t) - 1
        if self.gauge_btn.isChecked():
            return self.gauge_panel.value_index()
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

    def _set_profile(self, key: str) -> None:
        save_profile(key)
        if key == profiles.AUTO:
            t, series = self.data_snapshot() if self.buf.n else ([], {})
            p, why = profiles.detect(series)
            msg = f"Engine profile: automatic ({p.name} - {why})" if self.buf.n else "Engine profile: automatic"
        else:
            msg = f"Engine profile: {profiles.BY_KEY[key].label}"
        self.gauge_panel.load(key, fresh=False)
        self.statusBar().showMessage(msg + ". Applies to Analyse, guided tests, reports and gauges.", 8000)

    def _analyse(self) -> None:
        if not self.buf.n:
            self.statusBar().showMessage("No data yet", 5000)
            return
        dlg = AnalyseDialog(self)
        dlg.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        dlg.show()                                  # non-modal: charts stay usable

    # ---------------------------------------------------------------- gauge view
    def _sync_checks(self) -> None:
        """Tick the channels shown in the current view (charts or gauges)."""
        gauges = self.gauge_btn.isChecked()
        shown = set(self.gauge_panel.sel) if gauges else None
        self.table.blockSignals(True)
        for i, c in enumerate(self.channels):
            on = c.name in shown if gauges else i in self.selected
            self.table.item(self.row_of[i], 0).setCheckState(QtCore.Qt.Checked if on else QtCore.Qt.Unchecked)
        self.table.blockSignals(False)

    def _set_gauges(self, on: bool) -> None:
        self.stack.setCurrentIndex(1 if on else 0)
        self._sync_checks()
        if on:
            if not self.live:
                i = self._value_index()
                self.gauge_panel.set_index(i or 0)
            self.gauge_panel.refresh()
        else:
            self.gauge_panel.stop_play()
            self._refresh_curves()
            if self.live and not self.reviewing and self.buf.n:
                t_end = float(self.buf.times[-1])
                if self.window_s:
                    self._set_view(t_end - self.window_s, t_end)
                else:
                    self._fit_all()

    def show_range(self, a: float, b: float) -> None:
        """Switch to review mode and frame the time range a..b."""
        self.gauge_btn.setChecked(False)
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

    # ---------------------------------------------------------------- auto-capture
    def _arm_capture(self, on: bool) -> None:
        self.capture_armed = on
        self.cap_btn.setStyleSheet("background:#2b7de9;color:white;" if on else "")
        if on:
            self.capturer.known_codes = None        # codes present now are not "new"
            self._codes_next = 0.0
            missing = self.capturer.missing_channels()
            if missing:
                self.statusBar().showMessage("Auto-capture: this ECU doesn't log " + ", ".join(missing)
                                             + " - those rules are ignored", 15000)

    def _toggle_alerts(self, on: bool) -> None:
        self.alerts_on = on
        self.alert_btn.setText("⚠ Alerts on" if on else "⚠ Alerts off")
        if not on:
            self.alert_banner.setVisible(False)

    def _alert_settings(self) -> None:
        dlg = AlertDialog(self.alerter.settings, [c.name for c in self.channels], self)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            st = dlg.settings()
            save_alert_settings(st)
            self.alerter = capture.Alerter(st, [c.name for c in self.channels])
            missing = self.alerter.missing_channels()
            if missing:
                self.statusBar().showMessage("Alerts: this ECU doesn't log " + ", ".join(missing), 15000)

    def _show_dtc(self) -> None:
        if self._dtc_dialog is None:
            self._dtc_dialog = DtcDialog(self.source, self)
            self._dtc_dialog.finished.connect(lambda *_: setattr(self, "_dtc_dialog", None))
            self._dtc_dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self._dtc_dialog.show()
        self._dtc_dialog.raise_()

    def _capture_settings(self) -> None:
        dlg = CaptureDialog(self.capturer.settings, [c.name for c in self.channels], self)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            st = dlg.settings()
            save_capture_settings(st)
            pending, saved = self.capturer.pending, self.capturer.saved
            self.capturer = capture.Capturer(st, [c.name for c in self.channels])
            self.capturer.pending, self.capturer.saved = pending, saved

    def _capture_now(self) -> None:
        if not self.buf.n:
            return
        if self.capturer.trigger(float(self.buf.times[-1]), "manual capture"):
            self._mark()
        else:
            self.statusBar().showMessage("A capture is already in progress", 5000)

    def _capture_tick(self, t_now: float) -> None:
        # poll for new fault codes on the source's thread, between reads
        if self.capture_armed and self.capturer.settings.on_new_code:
            fut = self._codes_future
            if fut is not None and fut.done():
                self._codes_future = None
                if not fut.cancelled() and fut.exception() is None:
                    self.capturer.codes(t_now, fut.result())
            elif fut is None and time.monotonic() >= self._codes_next:
                self._codes_next = time.monotonic() + capture.POLL_CODES_S
                self._codes_future = self.source.submit(capture.poll_codes)
        done = self.capturer.due(t_now)
        if done:
            t_trig, reason = done
            when = time.time() - (t_now - t_trig)
            LOG_DIR.mkdir(exist_ok=True)
            path = LOG_DIR / time.strftime("ssm_%Y%m%d_%H%M%S_capture.csv", time.localtime(when))
            text = capture.capture_csv([c.label for c in self.channels], self.source.ecu_id, self.buf.times,
                                       [self.buf.series(i) for i in range(len(self.channels))],
                                       t_trig, reason, self.capturer.settings,
                                       time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when)))
            path.write_text(text)
            self.statusBar().showMessage(f"Captured {reason} -> {path.name}", 20000)

    def _tick(self) -> None:
        samples = self.source.drain()
        for t, values in samples:
            named = dict(zip(self.param_names, values))
            row = [c.compute(named) for c in self.channels]
            self.buf.append(t, row)
            if self.capture_armed:
                before = self.capturer.pending
                self.capturer.update(t, row)
                if self.capturer.pending is not before:
                    self._mark()
                    self.statusBar().showMessage(f"Auto-capture triggered: {self.capturer.pending[1]}", 10000)
            if self.alerts_on and self.alerter.update(t, row) and self.alerter.settings.sound:
                QtWidgets.QApplication.beep()
            if self.recorder:
                self.recorder.write(t, row, self.pending_mark)
            self.pending_mark = None
        if self.buf.n:
            self._capture_tick(float(self.buf.times[-1]))
        if self.alerts_on:
            active = self.alerter.active()
            self.alert_banner.setText("⚠  " + "   ·   ".join(active))
            self.alert_banner.setVisible(bool(active))

        rec = f"  |  REC {self.recorder.rows} rows" if self.recorder else ""
        if self.buf.n and (self.capture_armed or self.capturer.pending):
            rec += f"  |  Auto-capture {self.capturer.status(float(self.buf.times[-1]))}"
        paused = "  |  PAUSED" if self.reviewing else ""
        self.status_lbl.setText(f"  ECU {self.source.ecu_id}  |  {self.source.status}  |  "
                                f"{self.source.rate_hz:.0f} samples/s{rec}{paused}")
        if not samples:
            return
        if self.gauge_btn.isChecked():
            self.gauge_panel.refresh()
            self._update_values()
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
    ap.add_argument("--port", help="serial device, e.g. /dev/ttyACM0 (default: auto-detect the OpenPort)")
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
