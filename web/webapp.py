"""Browser glue: exposes the desktop app's Python modules to web/app.js (run with Pyodide).

recording.py, analyse.py, defs.py and dtc.py are used unchanged. Talking to the cable happens
in JavaScript (Web Serial); dtc.py's synchronous request(payload, timeout) calls are bridged to
the JavaScript driver with pyodide.ffi.run_sync, which needs JSPI (Chrome/Edge 137+).
"""

from __future__ import annotations

import json
import re
import math

import numpy as np

import analyse
import profiles
import recording

_profile = profiles.AUTO          # engine profile key chosen in the Tools menu, or "auto"


def profile_choices() -> str:
    return json.dumps(profiles.choices())


def set_profile(key: str) -> None:
    global _profile
    _profile = key if key == profiles.AUTO or key in profiles.BY_KEY else profiles.AUTO


def _profile_summary(p) -> list[str]:
    """What the profile changes in Analysis and the guided tests, in short lines."""
    lines = [f"Warm from {p.warm_c:.0f} °C, still warming up below {p.cold_c:.0f} °C",
             f"Warm idle airflow {p.idle_maf[0]:g}–{p.idle_maf[1]:g} g/s",
             "Trim-by-airflow bands: idle below {:g}, light to {:g}, cruise to {:g} g/s, load above".format(*p.maf_bands)]
    if p.turbo:
        lines.append(f"Boost checks on: in boost above {p.boost_psi:g} psi, leaner than {p.max_boost_afr:g} AFR is flagged")
    else:
        lines.append("Boost checks off")
    lines.append(f"Rear-sensor correction (A/F #3) judged against a resting value of {p.c3_rest:+.0f}%"
                 if p.c3_rest is not None else "Rear-sensor correction (A/F #3) not judged: its resting value isn't known")
    return lines


def profile_info(t_bytes=None, y_bytes=None, names_json: str = "[]") -> str:
    """The choices, what each changes, and what Automatic would pick for this log (and why)."""
    series = {}
    if t_bytes is not None and not _is_null(t_bytes):
        _, series = _arrays(t_bytes, y_bytes, json.loads(names_json))
    det, why = profiles.detect(series)
    return json.dumps({
        "choice": _profile,
        "profiles": [{"key": p.key, "name": p.name, "engines": p.engines, "summary": _profile_summary(p)}
                     for p in profiles.PROFILES],
        "detected": {"key": det.key, "name": det.name, "why": why if series else "no data yet"},
    })


def _clean(v):
    return None if isinstance(v, float) and not math.isfinite(v) else v


def _finding_dicts(findings) -> list[dict]:
    return [{"level": f.level, "title": f.title, "detail": f.detail, "short": f.short, "metric": f.metric,
             "channels": list(f.channels),
             "times": [[_clean(float(a)), _clean(float(b))] for a, b in f.times],
             "when": analyse._fmt_ranges(f.times, limit=12) if f.times else ""}
            for f in findings]


def _findings(t: np.ndarray, series: dict[str, np.ndarray]) -> list[dict]:
    return _finding_dicts(analyse.analyse(t, series, _profile))


def _arrays(t_bytes, y_bytes, names: list[str]):
    t = np.frombuffer(bytes(t_bytes.to_py()), dtype="<f8")
    y = np.frombuffer(bytes(y_bytes.to_py()), dtype="<f8").reshape(len(names), len(t))
    return t, {n: y[i] for i, n in enumerate(names)}


# ------------------------------------------------------------------ saved recordings

def load(path: str):
    rec = recording.load_csv(path)
    series = {c.name: rec.y[i] for i, c in enumerate(rec.channels)}
    meta = {
        "channels": [{"name": c.name, "units": c.units, "decimals": c.decimals, "switch": c.is_switch}
                     for c in rec.channels],
        "marks": [[float(t), int(n)] for t, n in rec.marks],
        "info": rec.info,
        "findings": _findings(rec.t, series),
    }
    return (json.dumps(meta),
            np.ascontiguousarray(rec.t, dtype="<f8").tobytes(),
            np.ascontiguousarray(rec.y, dtype="<f8").tobytes())


def analyse_arrays(t_bytes, y_bytes, names_json: str) -> str:
    """Analyse live data: t and y (channels x samples) as little-endian float64 bytes."""
    names = json.loads(names_json)
    t = np.frombuffer(bytes(t_bytes.to_py()), dtype="<f8")
    y = np.frombuffer(bytes(y_bytes.to_py()), dtype="<f8").reshape(len(names), len(t))
    return json.dumps(_findings(t, {n: y[i] for i, n in enumerate(names)}))


# ------------------------------------------------------------------ live: SSM parameters

_all_params: list = []
_params: list = []
_addresses: list[int] = []


def load_defs(path: str) -> int:
    import defs
    global _all_params
    _all_params = defs.load(path)
    return len(_all_params)


def param_table() -> str:
    """Every SSM base parameter, for the demo ECU (it encodes values back into raw bytes)."""
    return json.dumps([{"name": p.name, "address": p.address, "length": p.length, "signed": p.signed,
                        "expr": p.expr, "switch": p.switch, "switchBit": p.switch_bit}
                       for p in _all_params])


def select_params(init_js) -> str:
    """Pick the parameters this ECU supports from its init reply (bytes after 0xEA)."""
    import defs
    global _params, _addresses
    init = bytes(init_js.to_py())
    _params = [p for p in _all_params if p.supported_by(init)]
    _addresses = defs.request_addresses(_params)
    return json.dumps({
        "ecuId": init[3:8].hex().upper(),
        "params": [{"name": p.name, "units": p.units, "decimals": min(p.decimals, 3), "switch": p.switch}
                   for p in _params],
        "addresses": _addresses,
    })


def decode(data_js) -> list[float]:
    import defs
    return defs.decode(_params, _addresses, bytes(data_js.to_py()))


# ------------------------------------------------------------------ live: fault codes

def _is_null(v) -> bool:
    """JavaScript null/undefined arrive as None or (newer Pyodide) as a JsNull object."""
    return v is None or type(v).__name__ in ("JsNull", "JsUndefined")


def _bridge(js_request):
    from pyodide.ffi import run_sync, to_js

    def request(payload: bytes, timeout: float) -> bytes | None:
        reply = run_sync(js_request(to_js(payload), timeout))
        return None if _is_null(reply) else bytes(reply.to_py())
    return request


def read_codes(js_request) -> str:
    import dtc
    request = _bridge(js_request)
    return "\n\n".join([
        dtc.format_results(dtc.read_all(request)),
        dtc.format_freeze_frame(dtc.read_freeze_frame(request)),
        dtc.format_status(dtc.read_status(request)),
        dtc.format_test_results(dtc.read_test_results(request)),
        dtc.format_vehicle_info(dtc.read_vehicle_info(request)),
    ])


_FF_GROUPS = [
    ("Fuel & mixture", ("fuel system", "fuel trim", "o2 ", "a/f", "lambda", "fuel pressure", "evap")),
    ("Engine", ("engine speed", "load", "manifold", "air flow", "throttle", "ignition", "accelerator")),
    ("Conditions", ("",)),     # everything else
]


def _ff_name(name: str) -> str:
    """'O2 sensor bank 1 sensor 2 (rear) voltage' -> 'Rear O2 sensor'."""
    m = re.fullmatch(r"(O2|A/F) sensor bank 1 sensor \d \((front|rear)\)( voltage| current)?", name)
    return f"{m.group(2).capitalize()} {m.group(1)} sensor" if m else name


def _split_value(name: str, text: str) -> tuple[str, str]:
    """'95 km/h (59 mph)' -> ('95 km/h', '59 mph'); 'closed loop - using ...' -> ('Closed loop', 'using ...');
    'lambda 1.000 (AFR 14.70), current +0.00 mA' -> ('λ 1.000', 'AFR 14.70 · +0.00 mA')."""
    if text.startswith("lambda "):
        v, _, rest = text.replace("lambda ", "λ ", 1).partition(" (")
        return v, re.sub(r"[()]", "", rest).replace(", current", " ·").replace(", ", " · ")
    if " - " in text:
        v, sub = text.split(" - ", 1)
        return v[:1].upper() + v[1:], sub
    m = re.fullmatch(r"(.*?) \((.*)\)", text)
    return (m.group(1), m.group(2)) if m else (text, "")


def _num(text: str) -> float | None:
    m = re.match(r"[+-]?\d+(\.\d+)?", text.strip())
    return float(m.group(0)) if m else None


def read_codes_json(js_request) -> str:
    """Everything the fault-codes dialog shows, structured; "text" is the plain report for copying."""
    import dtc
    request = _bridge(js_request)
    results = dtc.read_all(request)
    ff = dtc.read_freeze_frame(request)
    st = dtc.read_status(request)
    tests = dtc.read_test_results(request)
    vi = dtc.read_vehicle_info(request)

    codes: dict[str, dict] = {}
    for r in results:
        for c in r.codes:
            entry = codes.setdefault(c, {"code": c, "kinds": []})
            if r.title not in entry["kinds"]:
                entry["kinds"].append(r.title)
    for c, e in codes.items():
        desc = dtc.describe(c)
        m = re.fullmatch(r"(.*) \((bank \d[^)]*)\)", desc)
        e.update(desc=m.group(1) if m else desc, bank=m.group(2).capitalize() if m else "",
                 system=dtc.SYSTEMS.get(c[0], ""), source=dtc.code_kind(c),
                 explain=dtc.EXPLANATIONS.get(c, ""))

    by_group: dict[str, list[dict]] = {title: [] for title, _ in _FF_GROUPS}
    for name, text in ff.values:
        title = next(t for t, keys in _FF_GROUPS if any(k in name.lower() for k in keys))
        v, sub = _split_value(name, text)
        by_group[title].append({"k": _ff_name(name), "v": v, "sub": sub})
    groups = [{"title": t, "rows": rows} for t, rows in by_group.items() if rows]
    ffd = dict(ff.values)
    trims = {k: _num(ffd[n]) for k, n in (("short", "Short-term fuel trim"), ("long", "Long-term fuel trim"))
             if n in ffd}

    return json.dumps({
        "codes": list(codes.values()),
        "mode_errors": [{"title": r.title, "error": r.error} for r in results if r.error],
        "status": {
            "mil": st.mil_on, "km": st.km_since_clear, "minutes": st.minutes_since_clear,
            "warmups": st.warmups_since_clear, "km_with_mil": st.km_with_mil,
            "fuel_system": _split_value("Fuel system", st.fuel_system) if st.fuel_system else None,
            "errors": st.errors or [],
        },
        "freeze": {"code": ff.code, "desc": dtc.describe(ff.code) if ff.code else "", "error": ff.error,
                   "groups": groups, "trims": trims, "since_start": ffd.get("Time since engine start", "")},
        "notes": {"permanent": dtc.PERMANENT_NOTE, "pending": dtc.PENDING_NOTE},
        "details": [
            {"title": "Readiness and status", "text": dtc.format_status(st)},
            {"title": "On-board test results", "text": dtc.format_test_results(tests)},
            {"title": "Vehicle information", "text": dtc.format_vehicle_info(vi)},
        ],
        "text": "\n\n".join([dtc.format_results(results), dtc.format_freeze_frame(ff), dtc.format_status(st),
                              dtc.format_test_results(tests), dtc.format_vehicle_info(vi)]),
    })


def clear_codes(js_request) -> str:
    """Returns "" on success, otherwise an error message."""
    import dtc
    return dtc.clear(_bridge(js_request))


def clear_warning() -> str:
    import dtc
    return dtc.CLEAR_WARNING


# ------------------------------------------------------------------ live: auto-capture

_capturer = None


def capture_setup(settings_json: str, names_json: str) -> str:
    """Create the capturer for this connection; returns the effective settings and missing channels."""
    import capture
    global _capturer
    try:
        st = capture.Settings.from_dict(json.loads(settings_json)) if settings_json else capture.Settings()
    except (ValueError, TypeError, KeyError):
        st = capture.Settings()
    pending, saved = (_capturer.pending, _capturer.saved) if _capturer else (None, 0)
    _capturer = capture.Capturer(st, json.loads(names_json))
    _capturer.pending, _capturer.saved = pending, saved
    return json.dumps({"settings": st.to_dict(), "missing": _capturer.missing_channels()})


def capture_reset_codes() -> None:
    _capturer.known_codes = None


def capture_update(t: float, row_js) -> str:
    """Feed one sample; returns the trigger reason if this sample started a capture, else ""."""
    before = _capturer.pending
    _capturer.update(t, row_js.to_py())
    return _capturer.pending[1] if _capturer.pending is not before else ""


def capture_trigger(t: float, reason: str) -> bool:
    return _capturer.trigger(t, reason)


def capture_poll_codes(js_request) -> str:
    import capture
    return json.dumps(sorted(capture.poll_codes(_bridge(js_request))))


def capture_codes(t: float, codes_json: str) -> None:
    _capturer.codes(t, set(json.loads(codes_json)))


def capture_due(t: float) -> str:
    d = _capturer.due(t) if _capturer else None
    return json.dumps(d) if d else ""


def capture_status(t: float) -> str:
    return _capturer.status(t) if _capturer else ""


def capture_csv_text(labels_json: str, ecu_id: str, t_bytes, y_bytes, t_trigger: float, reason: str,
                     when: str) -> str:
    import capture
    labels = json.loads(labels_json)
    t = np.frombuffer(bytes(t_bytes.to_py()), dtype="<f8")
    y = np.frombuffer(bytes(y_bytes.to_py()), dtype="<f8").reshape(len(labels), len(t))
    cols = [[None if v != v else float(v) for v in row] for row in y]
    return capture.capture_csv(labels, ecu_id, t.tolist(), cols, t_trigger, reason, _capturer.settings, when)



# ------------------------------------------------------------------ readiness tracker

def read_readiness(js_request) -> str:
    import dtc
    st = dtc.read_status(_bridge(js_request))
    return json.dumps({
        "summary": dtc.status_summary(st),
        "monitors": [{"name": m.name, "since": m.since_clear, "drive": m.this_drive,
                      "hint": dtc.MONITOR_HINTS.get(m.name, "") if m.since_clear == "NOT complete" else ""}
                     for m in st.monitors or []],
        "errors": st.errors or [],
    })


# ------------------------------------------------------------------ gauge view

def gauge_specs(channels_json: str, t_bytes, y_bytes, names_json: str) -> str:
    """Gauge type, range and zones for each channel ([name, units, is_switch]), for the profile."""
    import gauges
    t, series = _arrays(t_bytes, y_bytes, json.loads(names_json))
    return json.dumps(gauges.all_specs([tuple(c) for c in json.loads(channels_json)], _profile, series))


# ------------------------------------------------------------------ guided tests

def guided_list() -> str:
    import guided
    return json.dumps([{"key": g.key, "name": g.name, "purpose": g.purpose, "steps": g.steps, "needs": g.needs}
                       for g in guided.TESTS])


def guided_run(key: str, t_bytes, y_bytes, names_json: str, marks_json: str) -> str:
    import guided
    t, series = _arrays(t_bytes, y_bytes, json.loads(names_json))
    return json.dumps(_finding_dicts(guided.run(key, t, series, json.loads(marks_json), _profile)))


# ------------------------------------------------------------------ alerts

_alerter = None


def alert_setup(settings_json: str, names_json: str) -> str:
    import capture
    global _alerter
    try:
        st = capture.AlertSettings.from_dict(json.loads(settings_json)) if settings_json else capture.AlertSettings()
    except (ValueError, TypeError, KeyError):
        st = capture.AlertSettings()
    _alerter = capture.Alerter(st, json.loads(names_json))
    return json.dumps({"settings": st.to_dict(), "missing": _alerter.missing_channels()})


def alert_update(t: float, row_js) -> str:
    """Feed one sample; returns JSON [fired labels, active labels]."""
    fired = _alerter.update(t, row_js.to_py())
    return json.dumps([fired, _alerter.active()])


# ------------------------------------------------------------------ report

def report_html(js_request, opts_json: str, t_bytes, y_bytes) -> str:
    """Build the shareable report. js_request is None when not reading the ECU; t/y may be None."""
    import report
    opts = json.loads(opts_json)
    ecu = None if _is_null(js_request) else report.gather_ecu(_bridge(js_request))
    kw = {}
    if not _is_null(t_bytes):
        t, series = _arrays(t_bytes, y_bytes, opts["names"])
        kw = dict(t=t, series=series, units=opts.get("units"), findings=analyse.analyse(t, series, _profile))
    return report.build(opts.get("title") or "Vehicle report", ecu_id=opts.get("ecuId", ""),
                        source=opts.get("source", ""), ecu=ecu, include_vin=opts.get("includeVin", False),
                        generated=opts.get("generated"), **kw)
