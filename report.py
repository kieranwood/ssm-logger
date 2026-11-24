"""Shareable report: one self-contained HTML page (print it to PDF if needed) with whatever is
available - fault codes, freeze frame, readiness monitors, on-board test results and vehicle
information from the ECU, plus analysis findings and small charts from the logged data.

The VIN is left out unless include_vin is set, since reports are usually shared.

Used by logger.py (desktop) and web/webapp.py (browser).
"""

from __future__ import annotations

import html
import time

import numpy as np

import dtc

REPORT_CHANNELS = ["Total Fuel Trim", "Air/Fuel Sensor #1 AFR", "Rear O2 Sensor", "Coolant Temperature",
                   "Engine Speed", "Vehicle Speed"]
LEVEL_COLOR = {"problem": "#c62828", "warning": "#b26a00", "info": "#5f6b76", "ok": "#1b7f4b"}
LEVEL_MARK = {"problem": "✖", "warning": "▲", "info": "•", "ok": "✔"}

CSS = """
body { font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       color: #1c2024; background: #fff; max-width: 860px; margin: 24px auto; padding: 0 16px; }
h1 { font-size: 22px; margin-bottom: 2px; }
h2 { font-size: 17px; margin-top: 28px; border-bottom: 1px solid #dfe3e6; padding-bottom: 4px; }
h3 { font-size: 14px; margin: 14px 0 4px; }
.meta { color: #5f6b76; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 3px 8px 3px 0; vertical-align: top; }
th { color: #5f6b76; font-weight: 500; }
tr + tr td { border-top: 1px solid #eef1f4; }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.finding { margin: 6px 0; }
.finding .t { font-weight: 600; }
.finding .d { color: #3d4650; font-size: 13px; }
.hint { color: #5f6b76; font-size: 12px; }
.bad { color: #c62828; font-weight: 600; }
.warn { color: #b26a00; font-weight: 600; }
.chart { margin: 6px 0 14px; }
.chart .name { font-size: 13px; font-weight: 600; }
footer { margin-top: 32px; color: #5f6b76; font-size: 12px; }
@media print { body { margin: 0; } h2 { break-after: avoid; } .chart, tr { break-inside: avoid; } }
"""


def gather_ecu(request) -> dict:
    """Read everything the report can show from the ECU (all read-only requests)."""
    return {
        "codes": dtc.read_all(request),
        "freeze": dtc.read_freeze_frame(request),
        "status": dtc.read_status(request),
        "tests": dtc.read_test_results(request),
        "vehicle": dtc.read_vehicle_info(request),
    }


def _e(s) -> str:
    return html.escape(str(s))


def _codes_html(results) -> str:
    rows = []
    for r in results:
        if r.error:
            rows.append(f"<tr><td>{_e(r.title)}</td><td colspan=2 class=hint>({_e(r.error)})</td></tr>")
        elif not r.codes:
            rows.append(f"<tr><td>{_e(r.title)}</td><td colspan=2>none</td></tr>")
        for c in r.codes:
            rows.append(f"<tr><td>{_e(r.title)}</td><td><b>{_e(c)}</b></td><td>{_e(dtc.describe(c))}</td></tr>")
    return "<table><tr><th>Type</th><th>Code</th><th>Description</th></tr>" + "".join(rows) + "</table>"


def _freeze_html(ff) -> str:
    if ff.error:
        return f"<p class=hint>({_e(ff.error)})</p>"
    if not ff.code:
        return "<p>No freeze frame stored.</p>"
    rows = "".join(f"<tr><td>{_e(n)}</td><td>{_e(v)}</td></tr>" for n, v in ff.values)
    return (f"<p>Conditions when <b>{_e(ff.code)}</b> {_e(dtc.describe(ff.code))} set:</p>"
            f"<table>{rows}</table>")


def _status_html(st) -> str:
    out = []
    summary = dtc.status_summary(st)
    if summary:
        out.append("<table>" + "".join(f"<tr><td>{_e(a)}</td><td>{_e(b)}</td></tr>" for a, b in summary)
                   + "</table>")
    if st.monitors:
        rows = []
        for m in st.monitors:
            cls = " class=warn" if m.since_clear == "NOT complete" else ""
            hint = dtc.MONITOR_HINTS.get(m.name, "") if m.since_clear == "NOT complete" else ""
            rows.append(f"<tr><td>{_e(m.name)}</td><td{cls}>{_e(m.since_clear)}</td><td>{_e(m.this_drive)}</td>"
                        f"<td class=hint>{_e(hint)}</td></tr>")
        out.append("<h3>Readiness monitors</h3><table><tr><th>Monitor</th><th>Since cleared</th>"
                   "<th>This drive</th><th>How to complete</th></tr>" + "".join(rows) + "</table>")
    out += [f"<p class=hint>({_e(e)})</p>" for e in st.errors or []]
    return "".join(out)


def _tests_html(tr) -> str:
    if tr.error:
        return f"<p class=hint>({_e(tr.error)})</p>"
    if not tr.tests:
        return "<p>None reported yet - the ECU runs its tests while driving.</p>"
    failed = sum(not t.passed for t in tr.tests)
    near = sum(t.near_limit for t in tr.tests)
    rows, monitor = [], None
    for t in tr.tests:
        if t.monitor != monitor:
            monitor = t.monitor
            rows.append(f"<tr><td colspan=4><b>{_e(monitor)}</b></td></tr>")
        cls = {"FAILED": " class=bad", "near limit": " class=warn"}.get(t.status, "")
        rows.append(f"<tr><td>&nbsp;&nbsp;{_e(t.test)}</td><td class=num>{_e(t.fmt(t.value))}</td>"
                    f"<td>{_e(t.limits)}</td><td{cls}>{_e(t.status)}</td></tr>")
    return (f"<p>{len(tr.tests)} tests, <b>{failed}</b> failed, <b>{near}</b> within "
            f"{dtc.NEAR_LIMIT:.0%} of a limit.</p><table><tr><th>Test</th><th class=num>Value</th>"
            "<th>Limits</th><th>Result</th></tr>" + "".join(rows) + "</table>")


def _vehicle_html(vi, include_vin: bool) -> str:
    if vi.error:
        return f"<p class=hint>({_e(vi.error)})</p>"
    rows = []
    if vi.vin:
        rows.append(("VIN", vi.vin if include_vin else "(not included)"))
    if vi.ecu_name:
        rows.append(("ECU", vi.ecu_name))
    for i, c in enumerate(vi.calibration_ids or []):
        rows.append(("Calibration ID", c + (f" (CVN {vi.cvns[i]})" if vi.cvns and i < len(vi.cvns) else "")))
    out = "<table>" + "".join(f"<tr><td>{_e(a)}</td><td>{_e(b)}</td></tr>" for a, b in rows) + "</table>"
    if vi.iupr:
        r = "".join(f"<tr><td>{_e(n)}</td><td class=num>{c}</td><td class=num>{k}</td>"
                    f"<td class=num>{(c / k if k else 0):.2f}</td></tr>" for n, c, k in vi.iupr)
        out += ("<h3>How often each monitor gets to run</h3><p class=hint>Completed: times the monitor ran; "
                "chances: drives where its conditions were met. A low ratio means it rarely gets to run, "
                "which is why its readiness can take a while to complete.</p><table><tr><th>Monitor</th>"
                "<th class=num>Completed</th><th class=num>Chances</th><th class=num>Ratio</th></tr>" + r + "</table>")
    return out


def _findings_html(findings) -> str:
    if not findings:
        return "<p>No findings.</p>"
    out = []
    for f in findings:
        lv = f.level if isinstance(f.level, str) else str(f.level)
        when = ""
        if getattr(f, "times", None):
            import analyse
            when = f" <span class=hint>(at {_e(analyse._fmt_ranges(f.times, limit=6))})</span>"
        out.append(f"<div class=finding><span class=t style='color:{LEVEL_COLOR.get(lv, '#000')}'>"
                   f"{LEVEL_MARK.get(lv, '')} {_e(f.title)}</span>{when}<div class=d>{_e(f.detail)}</div></div>")
    return "".join(out)


def _svg_chart(t: np.ndarray, y: np.ndarray, color: str, shade: list[tuple[float, float]],
               w: int = 820, h: int = 96) -> str:
    ok = np.isfinite(y)
    if ok.sum() < 2:
        return ""
    t, y = t[ok], y[ok]
    step = max(1, len(t) // 900)
    t, y = t[::step], y[::step]
    lo, hi = float(np.min(y)), float(np.max(y))
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    pad_l, pad_b = 46, 16
    t0, t1 = float(t[0]), float(t[-1]) if t[-1] > t[0] else float(t[0]) + 1
    X = lambda v: pad_l + (v - t0) / (t1 - t0) * (w - pad_l - 4)
    Y = lambda v: 4 + (hi - v) / (hi - lo) * (h - pad_b - 8)
    pts = " ".join(f"{X(a):.1f},{Y(b):.1f}" for a, b in zip(t, y))
    shades = "".join(f"<rect x='{X(max(a, t0)):.1f}' y='4' width='{max(2.0, X(min(b, t1)) - X(max(a, t0))):.1f}' "
                     f"height='{h - pad_b - 8}' fill='#f5a524' opacity='0.18'/>"
                     for a, b in shade if b >= t0 and a <= t1)
    fmt = lambda v: f"{v:.0f}" if abs(hi - lo) >= 10 else f"{v:.2f}"
    return (f"<svg viewBox='0 0 {w} {h}' width='100%' role='img'>{shades}"
            f"<line x1='{pad_l}' y1='{h - pad_b}' x2='{w - 4}' y2='{h - pad_b}' stroke='#dfe3e6'/>"
            f"<text x='{pad_l - 4}' y='12' font-size='10' text-anchor='end' fill='#5f6b76'>{fmt(hi)}</text>"
            f"<text x='{pad_l - 4}' y='{h - pad_b}' font-size='10' text-anchor='end' fill='#5f6b76'>{fmt(lo)}</text>"
            f"<text x='{pad_l}' y='{h - 3}' font-size='10' fill='#5f6b76'>{t0:.0f} s</text>"
            f"<text x='{w - 4}' y='{h - 3}' font-size='10' text-anchor='end' fill='#5f6b76'>{t1:.0f} s</text>"
            f"<polyline points='{pts}' fill='none' stroke='{color}' stroke-width='1.3'/></svg>")


def build(title: str = "Vehicle report", *, ecu_id: str = "", source: str = "", ecu: dict | None = None,
          t=None, series: dict | None = None, units: dict | None = None, findings=None,
          include_vin: bool = False, generated: str | None = None) -> str:
    generated = generated or time.strftime("%Y-%m-%d %H:%M")
    parts = [f"<!doctype html><html lang=en><head><meta charset=utf-8>"
             f"<meta name=viewport content='width=device-width, initial-scale=1'>"
             f"<title>{_e(title)}</title><style>{CSS}</style></head><body>",
             f"<h1>{_e(title)}</h1><p class=meta>Generated {_e(generated)}"
             + (f" · ECU {_e(ecu_id)}" if ecu_id else "") + (f" · {_e(source)}" if source else "") + "</p>"]
    if ecu:
        parts.append("<h2>Fault codes</h2>" + _codes_html(ecu["codes"]))
        parts.append("<h2>Freeze frame</h2>" + _freeze_html(ecu["freeze"]))
        parts.append("<h2>Status since codes were cleared</h2>" + _status_html(ecu["status"]))
        parts.append("<h2>On-board test results</h2>" + _tests_html(ecu["tests"]))
        parts.append("<h2>Vehicle information</h2>" + _vehicle_html(ecu["vehicle"], include_vin))
    if findings is not None:
        parts.append("<h2>Analysis of the logged data</h2>" + _findings_html(findings)
                     + "<p class=hint>Rules of thumb for a naturally aspirated Subaru FB25 - hints, not a diagnosis.</p>")
    if t is not None and series:
        t = np.asarray(t, float)
        shade = [r for f in (findings or []) if f.level == "problem" for r in (f.times or [])]
        charts = []
        colors = ["#2b7de9", "#e5484d", "#8e4ec6", "#e07b00", "#1b7f4b", "#5f6b76"]
        for k, name in enumerate(n for n in REPORT_CHANNELS if n in series):
            svg = _svg_chart(t, np.asarray(series[name], float), colors[k % len(colors)], shade)
            if svg:
                u = f" ({_e(units[name])})" if units and units.get(name) else ""
                charts.append(f"<div class=chart><div class=name>{_e(name)}{u}</div>{svg}</div>")
        if charts:
            dur = (t[-1] - t[0]) / 60 if len(t) > 1 else 0
            parts.append(f"<h2>Logged data</h2><p class=hint>{dur:.1f} minutes, {len(t)} samples. "
                         "Shaded: times behind problem findings.</p>" + "".join(charts))
    parts.append("<footer>Made with ssm-logger (github.com/kieranwood/ssm-logger). Readings come from the "
                 "car's ECU; the analysis is rule-of-thumb guidance, not a professional diagnosis.</footer>"
                 "</body></html>")
    return "".join(parts)
