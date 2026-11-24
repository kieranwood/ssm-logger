"""Load saved logs for review: the logger's own CSVs and OpenPort SD-card CSVs."""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np


@dataclass
class Channel:
    name: str
    units: str
    decimals: int
    is_switch: bool
    compute: Callable[[dict[str, float]], float] = field(default=lambda v: math.nan, repr=False)

    @property
    def label(self) -> str:
        return f"{self.name} ({self.units})" if self.units else self.name


# Column names used in the SD-card logcfg.txt -> (logger channel name, units)
SD_ALIASES = {
    "RPM": ("Engine Speed", "RPM"),
    "Speed_kmh": ("Vehicle Speed", "KPH"),
    "Throttle_pct": ("Throttle Opening Angle", "%"),
    "Pedal_pct": ("Accelerator Opening Angle", "%"),
    "Load_pct": ("Engine Load", "%"),
    "MAF_gs": ("Mass Air Flow", "g/s"),
    "MAP_psi": ("Manifold Absolute Pressure", "PSI"),
    "Atmo_psi": ("Atmospheric Pressure", "PSI"),
    "Coolant_C": ("Coolant Temperature", "°C"),
    "IAT_C": ("Intake Air Temperature", "°C"),
    "Timing_deg": ("Ignition Timing", "° BTDC"),
    "IPW_ms": ("Fuel Injector #1 Pulse Width", "ms"),
    "AF_Correction_1_pct": ("Air/Fuel Correction #1", "%"),
    "AF_Learning_1_pct": ("Air/Fuel Learning #1", "%"),
    "AF_Sensor_1_AFR": ("Air/Fuel Sensor #1 AFR", "AFR"),
    "Rear_O2_V": ("Rear O2 Sensor", "V"),
    "AF_Correction_3_pct": ("Air/Fuel Correction #3", "%"),
    "AF1_Current_mA": ("Air/Fuel Sensor #1 Current", "mA"),
    "AF1_Resistance": ("Air/Fuel Sensor #1 Resistance", "Ohms"),
    "MAF_Sensor_V": ("Air Flow Sensor Voltage", "V"),
    "Battery_V": ("Battery Voltage", "V"),
    "Alternator_Duty": ("Alternator Duty", "%"),
    "CPC_Purge_Duty_pct": ("CPC Valve Duty Ratio", "%"),
    "Throttle_Sensor_V": ("Throttle Sensor Voltage", "V"),
}
_SUFFIX_UNITS = {"_pct": "%", "_C": "°C", "_V": "V", "_kmh": "km/h", "_psi": "PSI", "_gs": "g/s",
                 "_ms": "ms", "_deg": "°", "_AFR": "AFR", "_mA": "mA", "_A": "A"}
_LABEL = re.compile(r"^(.*) \(([^()]*)\)$")


@dataclass
class Recording:
    path: Path
    channels: list[Channel]
    t: np.ndarray                  # seconds
    y: np.ndarray                  # shape (len(channels), len(t))
    marks: list[tuple[float, int]]
    info: str                      # e.g. "ECU 7152345007" or "OpenPort SD log"


def _parse_label(col: str) -> tuple[str, str]:
    if col in SD_ALIASES:
        return SD_ALIASES[col]
    m = _LABEL.match(col)
    if m:
        return m.group(1), m.group(2)
    for suffix, units in _SUFFIX_UNITS.items():
        if col.endswith(suffix):
            return col[: -len(suffix)].replace("_", " "), units
    return col, ""


def _float(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return math.nan


def _decimals(v: np.ndarray) -> int:
    v = v[~np.isnan(v)]
    if not len(v) or np.all(v == np.round(v)):
        return 0
    return 1 if np.median(np.abs(v)) >= 100 else 2 if np.median(np.abs(v)) >= 1 else 3


def load_csv(path: str | Path) -> Recording:
    path = Path(path)
    info = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        lines = [ln for ln in f if ln.strip()]
    body = []
    for ln in lines:
        if ln.startswith("#"):
            info.append(ln.lstrip("# ").strip())
        else:
            body.append(ln)
    rows = list(csv.reader(body))
    if len(rows) < 2:
        raise ValueError("no data rows in file")
    header = [h.strip() for h in rows[0]]

    t_col = next((i for i, h in enumerate(header) if h.lower().startswith("time")), None)
    if t_col is None:
        raise ValueError("no time column (expected 'time' or 'time_s')")
    mark_col = next((i for i, h in enumerate(header) if h.lower() == "mark"), None)
    skip = {t_col, mark_col, *(i for i, h in enumerate(header) if h.lower() == "sample")}
    cols = [i for i in range(len(header)) if i not in skip]

    t, values, marks = [], [], []
    for r in rows[1:]:
        if len(r) <= t_col:
            continue
        tv = _float(r[t_col])
        if math.isnan(tv):
            continue
        t.append(tv)
        values.append([_float(r[i]) if i < len(r) else math.nan for i in cols])
        if mark_col is not None and mark_col < len(r) and r[mark_col].strip():
            m = _float(r[mark_col])
            marks.append((tv, int(m) if not math.isnan(m) else len(marks) + 1))
    if not t:
        raise ValueError("no data rows in file")
    y = np.array(values, dtype=float).T
    t_arr = np.array(t, dtype=float)

    channels, seen = [], {}
    for i in cols:
        name, units = _parse_label(header[i])
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:                          # e.g. old SD logs with truncated names
            name = f"{name} #{seen[name]}"
        is_switch = units == "On/Off"
        channels.append(Channel(name, units, 0 if is_switch else _decimals(y[len(channels)]), is_switch))

    names = [c.name for c in channels]
    if ("Total Fuel Trim" not in names and "Air/Fuel Correction #1" in names
            and "Air/Fuel Learning #1" in names):
        total = y[names.index("Air/Fuel Correction #1")] + y[names.index("Air/Fuel Learning #1")]
        channels.insert(0, Channel("Total Fuel Trim", "%", 2, False))
        y = np.vstack([total, y])

    if not info:
        info = ["OpenPort SD log"] if any(h in SD_ALIASES for h in header) else []
    return Recording(path, channels, t_arr, y, marks, " ".join(info))
