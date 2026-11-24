"""Gauge view for the desktop logger: the same gauges as the browser (web/gauges.js), drawn with Qt.

Which gauge suits each channel, its range and zones come from gauges.py. MainWindow (logger.py)
owns a GaugePanel and calls refresh() as data arrives or the recording is scrubbed.
"""

from __future__ import annotations

import json
import math

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

import gauges

COLORS = {"text": "#e8eaec", "muted": "#9ba1a6", "track": "#2a2e33", "accent": "#4e9fff",
          "warn": "#f5a524", "danger": "#ff6369", "cold": "#4cc4e6", "panel": "#18191b", "border": "#2b2f33"}
SIZES = {"s": (170, 150), "m": (220, 195), "l": (320, 280)}
SETTINGS = ("ssm-logger", "logger")


def _c(name: str, alpha: float = 1.0) -> QtGui.QColor:
    c = QtGui.QColor(COLORS[name])
    c.setAlphaF(alpha)
    return c


def _level_color(level: str | None) -> QtGui.QColor:
    return _c({"danger": "danger", "warn": "warn", "cold": "cold"}.get(level, "accent"))


def _zone_at(spec: dict, v) -> str | None:
    if v is None or not math.isfinite(v):
        return None
    if spec["lo"] is not None and spec["hi"] is not None:
        v = min(max(v, spec["lo"]), spec["hi"])       # off the scale: colour of the end it's past
    for a, b, level in spec["zones"]:
        if a <= v <= b:
            return level
    return None


def fmt_num(v, decimals: int) -> str:
    """Fewer decimals as numbers get bigger (as gauges.js fmtNum)."""
    if v is None or not math.isfinite(v):
        return "–"
    a = abs(v)
    return f"{v:.{min(decimals, 0 if a >= 1000 else 1 if a >= 100 else decimals)}f}"


def _tick_label(v: float, scale: float, step: float) -> str:
    scale = scale or 1
    x, s = v / scale, step / scale
    d = min(2, math.ceil(-math.log10(s))) if s < 1 else 0
    return f"{x:.{d}f}"


def _font(px: float, weight: int = 600) -> QtGui.QFont:
    f = QtGui.QFont()
    f.setPixelSize(max(1, round(px)))
    f.setWeight(QtGui.QFont.Weight(weight))
    return f


class GaugeCanvas(QtWidgets.QWidget):
    """Draws one gauge from a dict like gauges.js drawGauge's g."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.g: dict | None = None
        self.setMinimumHeight(60)

    def set_gauge(self, g: dict) -> None:
        self.g = g
        self.update()

    def paintEvent(self, _e) -> None:
        if not self.g:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        draw = {"dial": self._dial, "bar": self._bar, "centre": self._bar, "digital": self._digital,
                "lamp": self._lamp}.get(self.g["spec"]["type"], self._digital)
        draw(p, w, h, self.g)
        p.end()

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _text(p, x, y, text, color, font, align="center", valign="baseline"):
        p.setPen(color)
        p.setFont(font)
        fm = QtGui.QFontMetricsF(font)
        tw = fm.horizontalAdvance(text)
        x0 = x - tw / 2 if align == "center" else x - tw if align == "right" else x
        y0 = (y + fm.ascent() if valign == "top" else y - fm.descent() if valign == "bottom"
              else y + (fm.ascent() - fm.descent()) / 2 if valign == "middle" else y)
        p.drawText(QtCore.QPointF(x0, y0), text)

    def _minmax(self, p, w, y, g):
        if g["min"] is None:
            return
        f = _font(11, 500)
        self._text(p, 8, y, f"min {fmt_num(g['min'], g['decimals'])}", _c("muted"), f, "left")
        self._text(p, w - 8, y, f"max {fmt_num(g['max'], g['decimals'])}", _c("muted"), f, "right")

    # ------------------------------------------------------------ gauge types
    def _dial(self, p, w, h, g):
        s = g["spec"]
        lo, hi, step, scale = s["lo"], s["hi"], s["step"], s["scale"]
        r = min(w / 2 - 8, h * 0.56)
        cx, cy = w / 2, h * 0.55
        lw = max(5.0, r * 0.1)

        def ang(v):          # degrees, clockwise from +x like canvas
            f = min(1.0, max(0.0, (v - lo) / (hi - lo)))
            return 150 + f * 240

        def arc(a, b, color, width, rad):
            pen = QtGui.QPen(color, width)
            pen.setCapStyle(QtCore.Qt.FlatCap)
            p.setPen(pen)
            rect = QtCore.QRectF(cx - rad, cy - rad, 2 * rad, 2 * rad)
            # Qt angles: counter-clockwise from +x, in 1/16 degree
            p.drawArc(rect, round(-ang(a) * 16), round(-(ang(b) - ang(a)) * 16))

        def pt(a_deg, rad):
            a = math.radians(a_deg)
            return QtCore.QPointF(cx + math.cos(a) * rad, cy + math.sin(a) * rad)

        arc(lo, hi, _c("track"), lw, r - lw / 2)
        for a, b, level in s["zones"]:
            arc(a, b, _level_color(level), lw * 0.35, r - lw * 0.175)
        v = g["value"]
        has = v is not None and math.isfinite(v)
        level = _zone_at(s, v)
        col = _level_color(level)
        if has:
            base = s["centre"] if s["centre"] is not None else lo
            arc(min(base, v), max(base, v), col, lw * 0.55, r - lw * 0.65)
        if step:
            n = round((hi - lo) / step)
            every = 2 if n > 8 else 1
            f = _font(max(9, r * 0.13), 500)
            for i in range(n + 1):
                tv = lo + i * step
                p.setPen(QtGui.QPen(_c("muted"), 1.5))
                p.drawLine(pt(ang(tv), r - lw * 1.2), pt(ang(tv), r - lw * 1.9))
                if i % every == 0:
                    q = pt(ang(tv), r - lw * 3.1)
                    self._text(p, q.x(), q.y(), _tick_label(tv, scale, step), _c("muted"), f, "center", "middle")
        for m in (g["min"], g["max"]):
            if m is None:
                continue
            p.setPen(QtGui.QPen(_c("text", 0.45), 2))
            p.drawLine(pt(ang(m), r + 1), pt(ang(m), r - lw - 2))
        if has:
            pen = QtGui.QPen(_c("text") if level is None else col, max(2.0, r * 0.03))
            pen.setCapStyle(QtCore.Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(pt(ang(v) + 180, r * 0.08), pt(ang(v), r - lw * 0.8))
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(_c("text"))
        hub = max(4.0, r * 0.06)
        p.drawEllipse(QtCore.QPointF(cx, cy), hub, hub)
        self._text(p, cx, cy + r * 0.62, fmt_num(v, g["decimals"]),
                   _c("muted") if not has else _c("text") if level is None else col, _font(r * 0.3, 650))
        units = f"{g['units']} ×{scale}" if scale and scale != 1 else g["units"]
        self._text(p, cx, cy - r * 0.32, units, _c("muted"), _font(max(10, r * 0.13), 500))
        self._minmax(p, w, h - 4, g)

    def _bar(self, p, w, h, g):
        s = g["spec"]
        lo, hi = s["lo"], s["hi"]
        centre_type = s["type"] == "centre"
        centre = (s["centre"] if s["centre"] is not None else (lo + hi) / 2) if centre_type else lo
        v = g["value"]
        has = v is not None and math.isfinite(v)
        level = _zone_at(s, v)
        col = _level_color(level)
        vy = h * 0.36
        self._text(p, w / 2, vy, fmt_num(v, g["decimals"]),
                   _c("muted") if not has else col if level else _c("text"), _font(min(h * 0.27, w * 0.2), 650))
        self._text(p, w / 2, vy + 16, g["units"], _c("muted"), _font(12, 500))
        x0, x1 = 12.0, w - 12.0
        bh, by = max(10.0, h * 0.1), h * 0.56

        def X(x):
            return x0 + min(1.0, max(0.0, (x - lo) / (hi - lo))) * (x1 - x0)

        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(_c("track"))
        p.drawRoundedRect(QtCore.QRectF(x0, by, x1 - x0, bh), bh / 2, bh / 2)
        for a, b, lv in s["zones"]:
            p.setBrush(_level_color(lv))
            p.drawRect(QtCore.QRectF(X(a), by + bh + 2, X(b) - X(a), 3))
        if has:
            a, b = X(min(centre, v)), X(max(centre, v))
            p.setBrush(col)
            rad = min(bh / 2, (b - a) / 2)
            p.drawRoundedRect(QtCore.QRectF(a, by, max(b - a, 2), bh), rad, rad)
        if centre_type:
            p.setBrush(_c("text"))
            p.drawRect(QtCore.QRectF(X(centre) - 1, by - 3, 2, bh + 6))
        for m in (g["min"], g["max"]):
            if m is not None:
                p.setBrush(_c("text", 0.45))
                p.drawRect(QtCore.QRectF(X(m) - 1, by - 4, 2, bh + 8))
        f = _font(10, 500)
        step = s["step"] or (hi - lo) / 4
        ly = by + bh + 8
        self._text(p, x0, ly, _tick_label(lo, s["scale"], step), _c("muted"), f, "left", "top")
        self._text(p, x1, ly, _tick_label(hi, s["scale"], step), _c("muted"), f, "right", "top")
        if centre_type:
            self._text(p, X(centre), ly, _tick_label(centre, s["scale"], step), _c("muted"), f, "center", "top")
        if s["left"] or s["right"]:
            self._text(p, x0, by - 3, s["left"], _c("muted"), f, "left", "bottom")
            self._text(p, x1, by - 3, s["right"], _c("muted"), f, "right", "bottom")
        self._minmax(p, w, h - 4, g)

    def _digital(self, p, w, h, g):
        v = g["value"]
        has = v is not None and math.isfinite(v)
        level = _zone_at(g["spec"], v)
        vy = h * 0.4
        self._text(p, w / 2, vy, fmt_num(v, g["decimals"]),
                   _c("muted") if not has else _level_color(level) if level else _c("text"),
                   _font(min(h * 0.32, w * 0.22), 650))
        self._text(p, w / 2, vy + 16, g["units"], _c("muted"), _font(12, 500))
        hist = g.get("hist")
        top, bot = h * 0.58, h - 22
        if hist is not None and len(hist[0]) > 1:
            ts, vs = hist
            ok = np.isfinite(vs)
            if ok.any():
                mn, mx = float(np.min(vs[ok])), float(np.max(vs[ok]))
                if mx == mn:
                    mn, mx = mn - 1, mx + 1
                t1 = g["t_now"]
                t0 = t1 - 60
                path = QtGui.QPainterPath()
                pen_down = False
                for t, x in zip(ts, vs):
                    if not math.isfinite(x):
                        pen_down = False
                        continue
                    q = QtCore.QPointF(8 + (t - t0) / 60 * (w - 16), bot - (x - mn) / (mx - mn) * (bot - top))
                    if pen_down:
                        path.lineTo(q)
                    else:
                        path.moveTo(q)
                    pen_down = True
                p.setPen(QtGui.QPen(_c("accent"), 1.5))
                p.setBrush(QtCore.Qt.NoBrush)
                p.drawPath(path)
                self._text(p, 8, top - 12, "last minute", _c("muted"), _font(9, 500), "left", "top")
        self._minmax(p, w, h - 4, g)

    def _lamp(self, p, w, h, g):
        v = g["value"]
        known = v is not None and math.isfinite(v)
        on = known and v >= 0.5
        r = min(w, h) * 0.2
        cx, cy = w / 2, h * 0.42
        p.setPen(QtCore.Qt.NoPen)
        if on:
            p.setBrush(_c("accent", 0.18))
            p.drawEllipse(QtCore.QPointF(cx, cy), r * 1.45, r * 1.45)
        p.setBrush(_c("accent") if on else _c("track"))
        p.drawEllipse(QtCore.QPointF(cx, cy), r, r)
        self._text(p, cx, cy + r * 1.5, "ON" if on else "off" if known else "–",
                   _c("text") if on else _c("muted"), _font(15, 650), "center", "top")


class GaugeTile(QtWidgets.QFrame):
    def __init__(self, panel: "GaugePanel", name: str, units: str):
        super().__init__()
        self.setObjectName("tile")
        self.setStyleSheet(
            f"#tile {{ background:{COLORS['panel']}; border:1px solid {COLORS['border']}; border-radius:10px; }}"
            f"#tile QComboBox {{ color:{COLORS['muted']}; background:transparent; font-size:11px;"
            f" border:1px solid {COLORS['border']}; border-radius:4px; padding:0 4px; }}"
            f"#tile QToolButton {{ color:{COLORS['muted']}; border:0; }}")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 2)
        lay.setSpacing(0)
        head = QtWidgets.QHBoxLayout()
        label = QtWidgets.QLabel(name)
        label.setToolTip(f"{name} ({units})" if units else name)
        label.setStyleSheet(f"color:{COLORS['text']}; font-weight:600; font-size:12px;")
        label.setMinimumWidth(20)
        label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        head.addWidget(label, 1)
        self.combo = QtWidgets.QComboBox()
        self.combo.setMaximumWidth(96)
        self.combo.setToolTip("Gauge style (· marks the usual one for this channel)")
        default = panel.specs[name]["default"]
        for key, text in panel.types:
            self.combo.addItem(f"{text} ·" if key == default else text, key)
        self.combo.setCurrentIndex(self.combo.findData(panel.type_of.get(name, default)))
        self.combo.currentIndexChanged.connect(lambda _i: panel.set_type(name, self.combo.currentData()))
        head.addWidget(self.combo)
        close = QtWidgets.QToolButton(text="✕")
        close.setAutoRaise(True)
        close.setToolTip("Remove this gauge")
        close.clicked.connect(lambda: panel.toggle(name))
        head.addWidget(close)
        lay.addLayout(head)
        self.canvas = GaugeCanvas()
        lay.addWidget(self.canvas, 1)


class GaugePanel(QtWidgets.QWidget):
    """Toolbar (time slider and playback for recordings, min/max reset, size) and the gauge grid."""

    changed = QtCore.Signal()          # the selection changed (MainWindow updates its checkboxes)

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.specs: dict = {}
        self.types: list = []
        self.sel: list[str] = []
        self.type_of: dict[str, str] = {}
        self.tiles: dict[str, GaugeTile] = {}
        self.idx = 0
        self.reset_t = -math.inf
        self.play_t = 0.0
        self.setStyleSheet(f"GaugePanel {{ background:#111418; }} QLabel {{ color:{COLORS['muted']}; }}")
        self.setAttribute(QtCore.Qt.WA_StyledBackground)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        bar = QtWidgets.QHBoxLayout()
        self.play_btn = QtWidgets.QPushButton("▶ Play")
        self.play_btn.setToolTip("Play the recording")
        self.play_btn.clicked.connect(self.toggle_play)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.valueChanged.connect(self._slider_moved)
        self.time_lbl = QtWidgets.QLabel()
        self.time_lbl.setMinimumWidth(110)
        self.speed = QtWidgets.QComboBox()
        for s in (1, 2, 5, 10, 30):
            self.speed.addItem(f"{s}×", s)
        self.live_lbl = QtWidgets.QLabel("Newest values")
        for wdg in (self.play_btn, self.slider, self.time_lbl, self.speed, self.live_lbl):
            bar.addWidget(wdg, 1 if wdg is self.slider else 0)
        if win.live:
            bar.addStretch(1)
        reset = QtWidgets.QPushButton("Reset min/max")
        reset.setToolTip("Start the min/max readings again from now")
        reset.clicked.connect(self.reset_minmax)
        bar.addWidget(reset)
        bar.addWidget(QtWidgets.QLabel("Size"))
        self.size = QtWidgets.QComboBox()
        for key, text in (("s", "Small"), ("m", "Medium"), ("l", "Large")):
            self.size.addItem(text, key)
        self.size.currentIndexChanged.connect(self._size_changed)
        bar.addWidget(self.size)
        lay.addLayout(bar)
        for wdg in (self.play_btn, self.slider, self.time_lbl, self.speed):
            wdg.setVisible(not win.live)
        self.live_lbl.setVisible(win.live)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setStyleSheet("background:#111418;")
        self.grid_host = QtWidgets.QWidget()
        self.grid = QtWidgets.QGridLayout(self.grid_host)
        self.grid.setSpacing(10)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.scroll.setWidget(self.grid_host)
        self.scroll.viewport().installEventFilter(self)
        lay.addWidget(self.scroll, 1)
        hint = QtWidgets.QLabel("Tick channels on the left to add or remove gauges; pick a gauge style on each. "
                                "Pale marks show the lowest and highest values since the start (or the last reset).")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.play_timer = QtCore.QTimer(self)
        self.play_timer.timeout.connect(self._play_step)
        self._clock = QtCore.QElapsedTimer()

    # ------------------------------------------------------------ setup and prefs
    @staticmethod
    def _prefs() -> dict:
        raw = QtCore.QSettings(*SETTINGS).value("gauges", "")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _save(self) -> None:
        p = self._prefs()
        types = {k: v for k, v in p.get("types", {}).items() if k not in self.specs}
        types.update(self.type_of)
        QtCore.QSettings(*SETTINGS).setValue("gauges", json.dumps(
            {"selected": self.sel, "types": types, "size": self.size.currentData()}))

    def load(self, profile, fresh: bool = True) -> None:
        """(Re)load specs for the window's channels; fresh also restores the saved selection."""
        w = self.win
        t, series = w.data_snapshot()
        info = gauges.all_specs([(c.name, c.units, c.is_switch) for c in w.channels], profile, series)
        self.specs, self.types = info["specs"], info["types"]
        if fresh:
            prefs = self._prefs()
            saved = [n for n in prefs.get("selected", []) if n in self.specs]
            self.sel = saved or info["selection"]
            self.type_of = {k: v for k, v in prefs.get("types", {}).items() if k in self.specs}
            self.size.blockSignals(True)
            self.size.setCurrentIndex(max(0, self.size.findData(prefs.get("size", "m"))))
            self.size.blockSignals(False)
            self.idx, self.reset_t = 0, -math.inf
        self._build()

    def _build(self) -> None:
        for tile in self.tiles.values():
            tile.setParent(None)
            tile.deleteLater()
        self.tiles = {}
        units = {c.name: c.units for c in self.win.channels}
        for name in self.sel:
            self.tiles[name] = GaugeTile(self, name, units.get(name, ""))
        self._layout()
        self.refresh()

    def _layout(self) -> None:
        tw, th = SIZES[self.size.currentData() or "m"]
        width = self.scroll.viewport().width()
        cols = max(1, (width + 10) // (tw + 10))
        for i, tile in enumerate(self.tiles.values()):
            self.grid.addWidget(tile, i // cols, i % cols)
            tile.setFixedHeight(th)
            tile.setMinimumWidth(tw - 40)
        for c in range(self.grid.columnCount()):
            self.grid.setColumnStretch(c, 1 if c < cols else 0)
        self.grid.setRowStretch(self.grid.rowCount(), 1)

    def eventFilter(self, obj, ev) -> bool:
        if obj is self.scroll.viewport() and ev.type() == QtCore.QEvent.Resize:
            QtCore.QTimer.singleShot(0, self._relayout)
        return False

    def _relayout(self) -> None:
        for tile in self.tiles.values():
            self.grid.removeWidget(tile)
        self._layout()

    def _size_changed(self, _i) -> None:
        self._save()
        self._relayout()

    # ------------------------------------------------------------ selection
    def toggle(self, name: str) -> None:
        if name in self.sel:
            self.sel.remove(name)
        else:
            self.sel.append(name)
        self._save()
        self._build()
        self.changed.emit()

    def set_type(self, name: str, key: str) -> None:
        if key == self.specs[name]["default"]:
            self.type_of.pop(name, None)
        else:
            self.type_of[name] = key
        self._save()
        self.refresh()

    # ------------------------------------------------------------ time
    def value_index(self) -> int | None:
        n = self.win.buf.n
        if not n:
            return None
        return n - 1 if self.win.live else min(self.idx, n - 1)

    def set_index(self, i: int) -> None:
        self.idx = i
        self.slider.blockSignals(True)
        self.slider.setValue(i)
        self.slider.blockSignals(False)
        self.refresh()
        self.win._update_values()          # the channel list shows the same moment

    def _slider_moved(self, v: int) -> None:
        self.idx = v
        if self.play_timer.isActive():
            self.play_t = float(self.win.buf.times[v])
        self.refresh()
        self.win._update_values()

    def reset_minmax(self) -> None:
        i = self.value_index()
        self.reset_t = -math.inf if i is None else float(self.win.buf.times[i])
        self.refresh()

    def toggle_play(self) -> None:
        if self.play_timer.isActive():
            self.stop_play()
            return
        t = self.win.buf.times
        if self.win.live or len(t) < 2:
            return
        if self.idx >= len(t) - 1:
            self.idx = 0
        self.play_t = float(t[self.idx])
        self._clock.start()
        self.play_timer.start(33)
        self.play_btn.setText("❚❚ Pause")

    def stop_play(self) -> None:
        self.play_timer.stop()
        self.play_btn.setText("▶ Play")

    def _play_step(self) -> None:
        t = self.win.buf.times
        self.play_t += self._clock.restart() / 1000 * self.speed.currentData()
        i = int(min(np.searchsorted(t, self.play_t), len(t) - 1))
        if i >= len(t) - 1:
            self.stop_play()
        self.set_index(i)

    # ------------------------------------------------------------ drawing
    def refresh(self) -> None:
        if not self.isVisible() and self.tiles:
            return
        buf = self.win.buf
        t = buf.times
        n = len(t)
        idx = self.value_index()
        if not self.win.live:
            self.slider.blockSignals(True)
            self.slider.setMaximum(max(0, n - 1))
            self.slider.blockSignals(False)
            if idx is not None:
                self.time_lbl.setText(f"{t[idx] - t[0]:.1f} s of {t[-1] - t[0]:.0f} s")
        start = 0 if idx is None else int(np.searchsorted(t, self.reset_t))
        if idx is not None and start > idx:
            start = 0
        index = {c.name: i for i, c in enumerate(self.win.channels)}
        for name, tile in self.tiles.items():
            ci = index[name]
            ch = self.win.channels[ci]
            col = buf.series(ci)
            key = self.type_of.get(name, self.specs[name]["default"])
            spec = self.specs[name]["by_type"][key]
            value = mn = mx = None
            if idx is not None:
                value = float(col[idx])
                seg = col[start:idx + 1]
                ok = seg[np.isfinite(seg)]
                if len(ok):
                    mn, mx = float(ok.min()), float(ok.max())
            if key not in ("digital", "lamp") and (spec["lo"] is None or spec["hi"] is None):
                ok = col[np.isfinite(col)]
                lo, hi, step = gauges.nice_range(float(ok.min()), float(ok.max())) if len(ok) else (0, 1, 0.25)
                spec = dict(spec, lo=lo, hi=hi, step=step)
            hist = None
            if key == "digital" and idx is not None:
                k0 = int(np.searchsorted(t, t[idx] - 60))
                every = max(1, math.ceil((idx - k0 + 1) / 240))
                hist = (t[k0:idx + 1:every], col[k0:idx + 1:every])
            decimals = spec["decimals"] if spec["decimals"] is not None else min(ch.decimals, 2)
            tile.canvas.set_gauge({"spec": spec, "value": value, "min": mn, "max": mx, "decimals": decimals,
                                   "units": ch.units, "hist": hist, "t_now": float(t[idx]) if idx is not None else 0})
