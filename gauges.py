"""Gauge view: which kind of gauge suits each channel, its range and its coloured zones.

The desktop logger (logger.py) and the browser (web/app.js via webapp.py) draw the gauges
themselves; this module only decides what to draw, so both look and behave the same.

Gauge types:
    dial     needle gauge on a 240° arc (revs, speed, coolant, boost)
    bar      linear bar from the bottom of the range (throttle, load, voltage, airflow)
    centre   bar growing either side of a centre value (fuel trims, AFR around 14.7)
    digital  big number with min/max and a trend line of the last minute
    lamp     on/off indicator for switches

A spec is a plain dict so it passes straight to JavaScript as JSON:
    {"type", "lo", "hi", "centre", "zones": [[from, to, level]], "step", "scale", "left", "right",
     "decimals", "style", "band", "mark", "status"}
style picks the bar's shape in the browser: "split" (rich/lean halves out from the centre),
"target" (a marker on a scale with a good band), "battery", "fader" (vertical); band is the good
range [lo, hi] for "target"; mark a reference line (12 V on the battery); status the words shown
for a zone level ({"danger": "REDLINE"}).
lo/hi of None mean "fit the range to the data seen"; decimals of None mean the channel's own,
at most 2. Zone levels: "cold", "warn", "danger".
"""

from __future__ import annotations

import math

import profiles
from profiles import Profile

TYPES = [("dial", "Needle"), ("bar", "Bar"), ("centre", "Centre bar"), ("digital", "Digital"),
         ("lamp", "Indicator lamp")]
TYPE_KEYS = [k for k, _ in TYPES]

# shown first time, in this order, when the channels exist
DEFAULT_SELECTION = [
    "Engine Speed", "Vehicle Speed", "Coolant Temperature", "Manifold Relative Pressure",
    "Total Fuel Trim", "Air/Fuel Sensor #1 AFR", "Rear O2 Sensor", "Battery Voltage",
    "Air/Fuel Learning #1", "Mass Air Flow", "Throttle Opening Angle", "Intake Air Temperature",
    "Knock Correction",
]
TURBO_ONLY = {"Manifold Relative Pressure", "Knock Correction"}


def _spec(type_: str, lo=None, hi=None, centre=None, zones=(), step=None, scale=1, left="", right="",
          decimals=None, style=None, band=None, mark=None, status=None) -> dict:
    return {"type": type_, "lo": lo, "hi": hi, "centre": centre, "zones": [list(z) for z in zones],
            "step": step, "scale": scale, "left": left, "right": right, "decimals": decimals,
            "style": style, "band": list(band) if band else None, "mark": mark, "status": status or {}}


# readout precision that is easy to read at a glance
DECIMALS = {
    "Engine Speed": 0, "Vehicle Speed": 0, "Memorized Cruise Speed": 0, "Coolant Temperature": 0,
    "Intake Air Temperature": 0, "Battery Voltage": 1, "Total Fuel Trim": 1, "Air/Fuel Correction #1": 1,
    "Air/Fuel Correction #2": 1, "Air/Fuel Learning #1": 1, "Air/Fuel Learning #2": 1,
    "Air/Fuel Correction #3": 0, "Air/Fuel Sensor #1 AFR": 2, "Rear O2 Sensor": 2, "Mass Air Flow": 1,
    "Throttle Opening Angle": 0, "Accelerator Opening Angle": 0, "Engine Load": 0,
    "Manifold Relative Pressure": 1, "Manifold Absolute Pressure": 1, "Knock Correction": 1,
    "Ignition Timing": 0, "Learned Ignition Timing": 1,
}


def _trim(limit: float = 25) -> dict:
    return _spec("centre", -limit, limit, 0,
                 [(-limit, -20, "danger"), (-20, -10, "warn"), (10, 20, "warn"), (20, limit, "danger")],
                 step=10, left="rich", right="lean", style="split")


def default_spec(name: str, units: str = "", is_switch: bool = False, P: Profile | None = None) -> dict:
    """The gauge that suits this channel best."""
    s = _default_spec(name, units, is_switch, P)
    if s["decimals"] is None:
        s["decimals"] = DECIMALS.get(name)
    return s


def _default_spec(name: str, units: str, is_switch: bool, P: Profile | None) -> dict:
    P = P or profiles.DEFAULT
    u = (units or "").lower()
    if is_switch or u == "on/off":
        return _spec("lamp", 0, 1)
    if name == "Engine Speed":
        top = math.ceil(P.redline_rpm / 1000 + 1) * 1000
        return _spec("dial", 0, top, zones=[(P.redline_rpm - 1000, P.redline_rpm, "warn"), (P.redline_rpm, top, "danger")],
                     step=1000, scale=1000, status={"warn": "HIGH REVS", "danger": "REDLINE"})
    if name in ("Vehicle Speed", "Memorized Cruise Speed"):
        top = 140 if u == "mph" else 220
        return _spec("dial", 0, top, step=20)
    if name == "Coolant Temperature" and u in ("°c", "c"):
        return _spec("dial", 40, 130, zones=[(40, P.cold_c, "cold"), (100, P.hot_c, "warn"), (P.hot_c, 130, "danger")],
                     step=10, status={"cold": "WARMING UP", "warn": "HOT", "danger": "OVERHEATING"})
    if name == "Intake Air Temperature" and u in ("°c", "c"):
        return _spec("digital", -20, 80, zones=[(50, 65, "warn"), (65, 80, "danger")] if P.turbo else [])
    if name == "Manifold Relative Pressure" and u == "psi":
        top = 25 if P.turbo else 5
        return _spec("dial", -15, top, centre=0, step=5,
                     zones=[(P.boost_psi + 15, top, "warn")] if P.turbo else [], status={"warn": "HIGH BOOST"})
    if name == "Battery Voltage":
        return _spec("bar", 10, 16, zones=[(10, 12, "danger"), (12, 12.6, "warn"), (15, 16, "danger")], step=1,
                     style="battery", mark=12, status={"danger": "CHECK"})
    if name in ("Total Fuel Trim", "Air/Fuel Correction #1", "Air/Fuel Correction #2",
                "Air/Fuel Learning #1", "Air/Fuel Learning #2"):
        return _trim(35 if name == "Total Fuel Trim" else 25)
    if name.startswith("Air/Fuel Sensor") and u == "afr":
        return _spec("centre", 10, 20, 14.7, step=2, left="rich", right="lean", style="target", band=(14.2, 15.2),
                     zones=[(10, 13, "danger"), (13, 14.2, "warn"), (15.2, 16.5, "warn"), (16.5, 20, "danger")])
    if name.startswith("Air/Fuel Sensor") and u == "lambda":
        return _spec("centre", 0.7, 1.3, 1.0, step=0.1, left="rich", right="lean", style="target", band=(0.97, 1.03),
                     zones=[(0.7, 0.88, "danger"), (0.88, 0.97, "warn"), (1.03, 1.12, "warn"), (1.12, 1.3, "danger")])
    if name == "Rear O2 Sensor":
        return _spec("bar", 0, 1.1, step=0.2, left="lean", right="rich")
    if name == "Knock Correction":
        return _spec("centre", -6, 6, 0, [(-6, -3, "danger"), (-3, -1, "warn")], step=2, style="split")
    if name == "Mass Air Flow":
        return _spec("bar", 0, math.ceil(P.maf_bands[2] * 4 / 10) * 10, step=None)
    if u == "%" and not name.startswith("Air/Fuel"):
        return _spec("bar", 0, 100, step=25)
    return _spec("digital")


def spec_as(name: str, units: str, is_switch: bool, P: Profile | None, type_: str) -> dict:
    """The channel's spec drawn as another gauge type (the user's choice)."""
    s = default_spec(name, units, is_switch, P)
    if type_ == s["type"] or type_ not in TYPE_KEYS:
        return s
    s = dict(s, type=type_)
    if type_ == "centre" and s["centre"] is None:
        s["centre"] = 0 if s["lo"] is not None and s["lo"] < 0 < s["hi"] else None   # None: middle of range
    if type_ == "lamp":
        s.update(lo=0, hi=1)
    return s


def default_selection(names: list[str], P: Profile | None = None) -> list[str]:
    P = P or profiles.DEFAULT
    have = set(names)
    out = [n for n in DEFAULT_SELECTION if n in have and (P.turbo or n not in TURBO_ONLY)]
    return out or [n for n in names][:8]


def nice_range(lo: float, hi: float) -> tuple[float, float, float]:
    """Round a data range out to tidy numbers -> (lo, hi, tick step)."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return 0.0, 1.0, 0.25
    if hi - lo < 1e-9:
        pad = abs(hi) * 0.1 or 1.0
        lo, hi = lo - pad, hi + pad
    raw = (hi - lo) / 5
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return round(math.floor(lo / step) * step, 9), round(math.ceil(hi / step) * step, 9), step


def all_specs(channels: list[tuple[str, str, bool]], profile: str | Profile | None = None,
              series: dict | None = None) -> dict:
    """For the apps: {"specs": {name: {"default": type, "by_type": {type: spec}}},
    "selection": [names shown by default], "types": [[key, label]]}."""
    if not series or not any(len(v) for v in series.values()):
        # nothing logged yet: detect from which channels exist (a turbo ECU reports wastegate duty)
        series = {n: [0.0] for n, _, _ in channels}
    P = profile if isinstance(profile, Profile) else profiles.get(profile, series)
    specs = {n: {"default": default_spec(n, u, sw, P)["type"],
                 "by_type": {k: spec_as(n, u, sw, P, k) for k in TYPE_KEYS}} for n, u, sw in channels}
    return {"specs": specs, "selection": default_selection([n for n, _, _ in channels], P),
            "types": [list(t) for t in TYPES]}
