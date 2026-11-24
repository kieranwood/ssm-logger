"""Browser glue: exposes the desktop app's Python modules to web/app.js (run with Pyodide).

recording.py, analyse.py, defs.py and dtc.py are used unchanged. Talking to the cable happens
in JavaScript (Web Serial); dtc.py's synchronous request(payload, timeout) calls are bridged to
the JavaScript driver with pyodide.ffi.run_sync, which needs JSPI (Chrome/Edge 137+).
"""

from __future__ import annotations

import json
import math

import numpy as np

import analyse
import recording


def _clean(v):
    return None if isinstance(v, float) and not math.isfinite(v) else v


def _findings(t: np.ndarray, series: dict[str, np.ndarray]) -> list[dict]:
    return [{"level": f.level, "title": f.title, "detail": f.detail,
             "times": [[_clean(float(a)), _clean(float(b))] for a, b in f.times],
             "when": analyse._fmt_ranges(f.times, limit=12) if f.times else ""}
            for f in analyse.analyse(t, series)]


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

def _bridge(js_request):
    from pyodide.ffi import run_sync, to_js

    def request(payload: bytes, timeout: float) -> bytes | None:
        reply = run_sync(js_request(to_js(payload), timeout))
        return None if reply is None else bytes(reply.to_py())
    return request


def read_codes(js_request) -> str:
    import dtc
    request = _bridge(js_request)
    return "\n\n".join([
        dtc.format_results(dtc.read_all(request)),
        dtc.format_freeze_frame(dtc.read_freeze_frame(request)),
        dtc.format_status(dtc.read_status(request)),
    ])


def clear_codes(js_request) -> str:
    """Returns "" on success, otherwise an error message."""
    import dtc
    return dtc.clear(_bridge(js_request))


def clear_warning() -> str:
    import dtc
    return dtc.CLEAR_WARNING
