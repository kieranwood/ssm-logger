"""Fixed rule-based checks on a recording, aimed at fuel trim / sensor faults.

    .venv/bin/python analyse.py logs/ssm_....csv     # also works on SD-card logs

The same checks run from the "Analyse" button in the logger. Each check only
runs when the channels it needs were logged, and every finding carries the
time ranges it is based on so the GUI can jump straight to them.

These are rules of thumb for a naturally aspirated Subaru FB25 with a front
wideband A/F sensor and a rear narrowband O2 sensor - not a diagnosis.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

import numpy as np

PROBLEM, WARNING, INFO, OK = "problem", "warning", "info", "ok"
LEVEL_ORDER = {PROBLEM: 0, WARNING: 1, INFO: 2, OK: 3}
LEVEL_MARK = {PROBLEM: "✖", WARNING: "▲", INFO: "•", OK: "✔"}

WARM_C = 80.0          # coolant considered fully warm
COLD_C = 70.0          # below this the engine is still warming up
C3_REST = -50.0        # A/F Correction #3 rests here on this ECU (not 0)


@dataclass
class Finding:
    level: str
    title: str
    detail: str
    times: list[tuple[float, float]] = field(default_factory=list)


class Log:
    """Channel lookup by name with NaN-filled stand-ins for missing channels."""

    def __init__(self, t: np.ndarray, series: dict[str, np.ndarray]):
        self.t = np.asarray(t, dtype=float)
        self.s = series

    def has(self, *names: str) -> bool:
        return all(n in self.s and np.isfinite(self.s[n]).any() for n in names)

    def __getitem__(self, name: str) -> np.ndarray:
        return self.s[name]

    def duration(self, mask: np.ndarray) -> float:
        """Seconds covered by the samples in mask."""
        if len(self.t) < 2:
            return 0.0
        dt = np.diff(self.t, append=self.t[-1])
        dt = np.clip(dt, 0, 1.0)              # don't count logging gaps
        return float(dt[mask].sum())

    def runs(self, mask: np.ndarray, min_s: float = 0.0, join_s: float = 0.5) -> list[tuple[float, float]]:
        """Time ranges where mask is true, merging gaps shorter than join_s."""
        out: list[list[float]] = []
        idx = np.flatnonzero(mask)
        for i in idx:
            t = self.t[i]
            if out and t - out[-1][1] <= join_s:
                out[-1][1] = t
            else:
                out.append([t, t])
        return [(a, b) for a, b in out if b - a >= min_s]


def _fmt_ranges(ranges: list[tuple[float, float]], limit: int = 5) -> str:
    parts = [f"{a:.0f}–{b:.0f} s" if b - a >= 1 else f"{a:.1f} s" for a, b in ranges[:limit]]
    more = f" (+{len(ranges) - limit} more)" if len(ranges) > limit else ""
    return ", ".join(parts) + more


def _mean(v: np.ndarray, mask: np.ndarray) -> float:
    v = v[mask]
    v = v[np.isfinite(v)]
    return float(v.mean()) if len(v) else float("nan")


# ---------------------------------------------------------------------- checks
def _states(L: Log) -> dict[str, np.ndarray]:
    n = len(L.t)
    true = np.ones(n, bool)
    running = L["Engine Speed"] > 400 if L.has("Engine Speed") else true
    ipw = L["Fuel Injector #1 Pulse Width"] if L.has("Fuel Injector #1 Pulse Width") else None
    fuel_cut = running & (ipw <= 0.01) if ipw is not None else np.zeros(n, bool)
    if L.has("Air/Fuel Correction #1"):
        # this ECU parks the short-term trim at exactly 0 when not in closed loop
        closed = running & ~fuel_cut & (L["Air/Fuel Correction #1"] != 0)
    else:
        closed = running & ~fuel_cut
    if L.has("Coolant Temperature"):
        cool = L["Coolant Temperature"]
        warm, cold = cool >= WARM_C, cool < COLD_C
    else:
        warm, cold = true, np.zeros(n, bool)
    idle = running & ((L["Vehicle Speed"] < 2) if L.has("Vehicle Speed") else (L["Engine Speed"] < 1000))
    return dict(running=running, fuel_cut=fuel_cut, closed=closed, warm=warm, cold=cold, idle=idle)


def check_trims(L: Log, S) -> list[Finding]:
    if not L.has("Air/Fuel Correction #1"):
        return []
    out = []
    stft = L["Air/Fuel Correction #1"]
    ltft = L["Air/Fuel Learning #1"] if L.has("Air/Fuel Learning #1") else np.zeros_like(stft)
    total = stft + ltft
    cl = S["closed"]
    if L.duration(cl) < 20:
        return [Finding(INFO, "Not enough closed-loop data for fuel trim checks",
                        "Log at least a minute with the engine running and warmed up.")]

    for label, mask in (("warm engine", cl & S["warm"]), ("whole log", cl)):
        if L.duration(mask) >= 20:
            break
    m = _mean(total, mask)
    lean = L.runs(cl & (total > 15), min_s=5)
    rich = L.runs(cl & (total < -15), min_s=5)
    if m > 10 or m < -10:
        side = "lean (ECU adding fuel)" if m > 0 else "rich (ECU removing fuel)"
        out.append(Finding(PROBLEM, f"Total fuel trim averages {m:+.1f}% ({label})",
                           f"Beyond ±10% means the engine runs {side}. Healthy is within about ±5%.",
                           lean or rich))
    elif m > 5 or m < -5:
        out.append(Finding(WARNING, f"Total fuel trim averages {m:+.1f}% ({label})",
                           "Within the ECU's range but higher than a healthy ±5%.", lean or rich))
    else:
        out.append(Finding(OK, f"Total fuel trim averages {m:+.1f}% ({label})", "Within ±5%."))
    if lean:
        out.append(Finding(WARNING, f"Lean trim above +15% for {len(lean)} stretch(es) of 5 s or more",
                           f"At {_fmt_ranges(lean)}.", lean))
    if rich:
        out.append(Finding(WARNING, f"Rich trim below −15% for {len(rich)} stretch(es) of 5 s or more",
                           f"At {_fmt_ranges(rich)}.", rich))

    lt = _mean(ltft, S["running"])
    if abs(lt) > 10:
        out.append(Finding(PROBLEM, f"Long-term fuel learning is {lt:+.1f}%",
                           "The ECU has learnt a large permanent correction - the fault is not just momentary."))
    elif abs(lt) > 5:
        out.append(Finding(WARNING, f"Long-term fuel learning is {lt:+.1f}%",
                           "A moderate learnt correction. Within ±5% is typical."))

    # warm-up dependence
    if L.has("Coolant Temperature"):
        cold_m, warm_m = _mean(total, cl & S["cold"]), _mean(total, cl & S["warm"])
        cold_s, warm_s = L.duration(cl & S["cold"]), L.duration(cl & S["warm"])
        if cold_s >= 15 and warm_s >= 15:
            diff = cold_m - warm_m
            text = (f"Closed-loop trim is {cold_m:+.1f}% below {COLD_C:.0f}°C coolant and "
                    f"{warm_m:+.1f}% above {WARM_C:.0f}°C.")
            if diff > 8:
                out.append(Finding(PROBLEM, "Engine runs lean only while warming up", text +
                                   " A fault that fades as the engine heats up points to something that "
                                   "seals with heat: an exhaust leak before the front A/F sensor (manifold, "
                                   "up-pipe gasket, loose sensor), or a cold-only intake leak. A coolant "
                                   "sensor reading wrong would also do this.",
                                   L.runs(cl & S["cold"] & (total > 15), min_s=2)))
            elif diff < -8:
                out.append(Finding(WARNING, "Engine runs leaner once warm", text +
                                   " A fault that grows with heat suggests fuel delivery (pump, "
                                   "regulator, injectors) or a leak that opens up when hot."))
            else:
                out.append(Finding(OK, "Fuel trim doesn't depend on engine temperature", text))

    # load dependence
    if L.has("Mass Air Flow"):
        maf = L["Mass Air Flow"]
        base = cl & S["warm"] if L.duration(cl & S["warm"]) >= 20 else cl
        bands = [("idle (<5 g/s)", maf < 5), ("light (5–15 g/s)", (maf >= 5) & (maf < 15)),
                 ("cruise (15–40 g/s)", (maf >= 15) & (maf < 40)), ("load (>40 g/s)", maf >= 40)]
        rows = [(name, _mean(total, base & b), L.duration(base & b)) for name, b in bands]
        rows = [r for r in rows if r[2] >= 5]
        if len(rows) >= 2:
            table = "; ".join(f"{n}: {v:+.1f}%" for n, v, _ in rows)
            lo, hi = rows[0][1], rows[-1][1]
            if lo - hi > 8 and lo > 8:
                out.append(Finding(WARNING, "Lean at low airflow, better at higher airflow",
                                   f"{table}. A fixed leak matters most when airflow is small - classic for "
                                   "an unmetered air leak after the MAF (vacuum or intake), or an exhaust leak "
                                   "ahead of the A/F sensor."))
            elif hi - lo > 8 and hi > 8:
                out.append(Finding(WARNING, "Lean at high airflow, fine at low airflow",
                                   f"{table}. Points to fuel supply (weak pump, clogged filter, regulator) or "
                                   "a MAF under-reading at high flow."))
            else:
                out.append(Finding(INFO, "Fuel trim by airflow", table))
    return out


def check_sensors(L: Log, S) -> list[Finding]:
    out = []
    cl, warm = S["closed"], S["warm"]
    has_front = L.has("Air/Fuel Sensor #1 AFR")
    if has_front:
        afr = L["Air/Fuel Sensor #1 AFR"]
        run = S["running"]
        spread = np.nanpercentile(afr[run], 99) - np.nanpercentile(afr[run], 1) if run.any() else 0
        cut = S["fuel_cut"]
        cuts = L.runs(cut, join_s=0.5)
        if L.duration(cut) >= 1.5:
            # the sensor lags: its lean swing peaks just after injection resumes
            after = np.zeros_like(cut)
            for a, b in cuts:
                after |= (L.t >= a) & (L.t <= b + 2.0)
            peak = float(np.nanmax(afr[after]))
            if peak < 16:
                out.append(Finding(WARNING, "Front A/F sensor barely reacts to fuel cuts",
                                   f"Across {len(cuts)} fuel cut(s) it peaked at only {peak:.1f} AFR; a "
                                   "working sensor swings well lean (>18) when the cut lasts a second or "
                                   "more. Sensor, heater or wiring - or just very short cuts.", cuts))
            else:
                out.append(Finding(OK, "Front A/F sensor responds to fuel cuts",
                                   f"Swung to {peak:.1f} AFR around the fuel cuts.", cuts))
        elif spread < 0.3:
            out.append(Finding(INFO, "Front A/F sensor reading hardly moves in this log",
                               f"It spans only {spread:.2f} AFR. In closed loop the ECU holds it on target, so "
                               "a flat line there is normal. Lifting off at speed (a fuel cut) is the "
                               "easy way to see it respond."))

    if L.has("Rear O2 Sensor"):
        o2 = L["Rear O2 Sensor"]
        mask = cl & warm if L.duration(cl & warm) >= 30 else cl
        if L.duration(mask) >= 30:
            med = float(np.nanmedian(o2[mask]))
            rich_t = L.runs(mask & (o2 > 0.8), min_s=10)
            front = (f" while the front sensor averages {_mean(L['Air/Fuel Sensor #1 AFR'], mask):.2f} AFR"
                     if has_front else "")
            if med > 0.8:
                out.append(Finding(PROBLEM, f"Rear O2 sensor says rich most of the time (median {med:.2f} V)",
                                   f"After the catalyst the exhaust reads rich{front}. If the front sensor shows "
                                   "stoich or lean at the same time, it is seeing extra oxygen the engine "
                                   "didn't burn - usually an exhaust leak before it, or a biased front sensor. "
                                   "The ECU then adds fuel the engine doesn't need.", rich_t))
            elif med < 0.2:
                out.append(Finding(PROBLEM, f"Rear O2 sensor says lean most of the time (median {med:.2f} V)",
                                   "Engine genuinely lean after the catalyst, or an exhaust leak near the "
                                   "rear sensor, or a failed rear sensor."))
            else:
                out.append(Finding(OK, f"Rear O2 sensor median {med:.2f} V in closed loop",
                                   "Switching around the 0.45–0.75 V range is normal."))
        lo = float(np.nanmin(o2[S["running"]])) if S["running"].any() else np.nan
        if L.duration(S["fuel_cut"]) >= 2 and lo > 0.3:
            out.append(Finding(WARNING, "Rear O2 sensor never went lean",
                               f"Lowest reading {lo:.2f} V despite fuel cuts; a healthy sensor drops below "
                               "0.2 V. Sensor may be slow or stuck."))

    if L.has("Air/Fuel Correction #3"):
        # On this ECU the value rests around -50%, not 0: it reads -50% at key-on before the rear
        # sensor is warm, and with a healthy front sensor it swings either side of -50% no matter
        # whether the rear O2 reads rich or lean. So only a median well away from -50% is notable.
        c3 = L["Air/Fuel Correction #3"]
        mask = S["running"]
        if L.duration(mask) >= 30:
            med = float(np.nanmedian(c3[mask]))
            off = med - C3_REST
            if abs(off) > 15:
                out.append(Finding(WARNING, f"Rear-sensor correction (A/F Correction #3) sits at {med:+.0f}%",
                                   f"That is {off:+.0f} points from its usual resting value of about "
                                   f"{C3_REST:+.0f}%. Treat as a hint only - the exact meaning and limits of "
                                   "this value aren't known; rely on the fuel trims and rear O2 voltage."))
            else:
                out.append(Finding(INFO, f"Rear-sensor correction (A/F Correction #3) median {med:+.0f}%",
                                   f"Close to its usual resting value of about {C3_REST:+.0f}% on this ECU, "
                                   "so nothing to read into it."))

    if L.has("Air/Fuel Sensor #1 Resistance") and L.has("Coolant Temperature"):
        r = L["Air/Fuel Sensor #1 Resistance"]
        late = S["running"] & (L.t > L.t[0] + 60)
        if L.duration(late) >= 30:
            med = float(np.nanmedian(r[late]))
            bad = L.runs(late & (r >= 200), min_s=1)
            if bad:
                out.append(Finding(WARNING, "Front A/F sensor element resistance jumped to its maximum",
                                   f"Reads 255 Ω (off-scale) at {_fmt_ranges(bad)} after warm-up; typical is "
                                   f"{med:.0f} Ω. Short blips can be ECU housekeeping; repeated ones suggest "
                                   "a heater or wiring problem.", bad))
            elif med > 100:
                out.append(Finding(WARNING, f"Front A/F sensor element resistance is high ({med:.0f} Ω)",
                                   "A hot sensor normally reads a few tens of ohms. Heater circuit or ageing "
                                   "sensor."))
    return out


def check_maf(L: Log, S) -> list[Finding]:
    out = []
    if L.has("Mass Air Flow", "Coolant Temperature"):
        m = S["idle"] & S["warm"] & S["closed"]
        if L.duration(m) >= 10:
            g = float(np.nanmedian(L["Mass Air Flow"][m]))
            if g < 2.0:
                out.append(Finding(WARNING, f"Warm idle airflow is low ({g:.1f} g/s)",
                                   "A 2.5 L engine usually idles around 2.5–4 g/s. A MAF reading low (dirty or "
                                   "failing) or air bypassing it would make the engine run lean."))
            else:
                out.append(Finding(OK, f"Warm idle airflow {g:.1f} g/s", "Plausible for a 2.5 L engine."))
    if L.has("Air Flow Sensor Voltage"):
        v = L["Air Flow Sensor Voltage"]
        dv = np.abs(np.diff(v, prepend=v[0]))
        steady = S["running"].copy()
        if L.has("Throttle Opening Angle"):
            thr = L["Throttle Opening Angle"]
            steady &= np.abs(np.diff(thr, prepend=thr[0])) < 1.0
        if L.has("Engine Speed"):
            rpm = L["Engine Speed"]
            steady &= np.abs(np.diff(rpm, prepend=rpm[0])) < 150
        glitch = steady & (dv > 0.5)
        # a glitch is a jump that comes straight back
        back = np.abs(np.roll(v, -1) - np.roll(v, 1)) < 0.2
        glitch &= back
        gl = L.runs(glitch, join_s=0.2)
        if gl:
            out.append(Finding(WARNING, f"MAF voltage glitched {len(gl)} time(s) with steady throttle/RPM",
                               f"Single-sample jumps of over 0.5 V at {_fmt_ranges(gl)}. Can point to a poor "
                               "connector or wiring - try wiggling the MAF plug while logging.", gl))
        elif L.duration(steady) >= 20:
            out.append(Finding(OK, "MAF voltage is smooth", "No single-sample spikes at steady throttle."))
    return out


def check_electrical(L: Log, S) -> list[Finding]:
    if not L.has("Battery Voltage"):
        return []
    out = []
    v = L["Battery Voltage"]
    run = S["running"] & (L.t > L.t[0] + 5)          # skip cranking / the first seconds
    if L.duration(run) < 10:
        return []
    low = L.runs(run & (v < 12.8), min_s=1.0)
    high = L.runs(run & (v > 15.0), min_s=1.0)
    dips = L.runs(run & (v < 13.0), join_s=0.1)
    med = float(np.nanmedian(v[run]))
    if low:
        out.append(Finding(PROBLEM, "Charging voltage dropped below 12.8 V for over a second",
                           f"At {_fmt_ranges(low)} (normal running: {med:.2f} V). Check the alternator, battery "
                           "terminals and the main ground straps.", low))
    if high:
        out.append(Finding(PROBLEM, "Charging voltage above 15 V", f"At {_fmt_ranges(high)}. Regulator fault.", high))
    if not low and not high:
        short = [d for d in dips if d[1] - d[0] < 1.0]
        extra = (f" {len(short)} brief dip(s) below 13 V at {_fmt_ranges(short)} - single-sample dips "
                 "are normal when big loads switch on.") if short else ""
        out.append(Finding(OK, f"Charging voltage steady around {med:.2f} V", "Within 13.0–15.0 V." + extra,
                           short))
    return out


def check_engine(L: Log, S) -> list[Finding]:
    out = []
    if L.has("Coolant Temperature"):
        c = L["Coolant Temperature"]
        run_s = L.duration(S["running"])
        top = float(np.nanmax(c))
        if run_s > 600 and top < 75:
            out.append(Finding(WARNING, f"Coolant only reached {top:.0f}°C after {run_s / 60:.0f} min running",
                               "Possible thermostat stuck open. The ECU keeps warm-up enrichment and fuel "
                               "trim behaviour stays in 'cold' mode."))
        elif top >= WARM_C:
            hit = np.flatnonzero(c >= WARM_C)
            start = np.flatnonzero(S["running"])
            if len(hit) and len(start) and c[start[0]] < 40:
                mins = (L.t[hit[0]] - L.t[start[0]]) / 60
                out.append(Finding(INFO, f"Warmed from {c[start[0]]:.0f}°C to {WARM_C:.0f}°C in {mins:.1f} min",
                                   "Typical is 5–15 min depending on outside temperature and driving."))
        if top > 105:
            out.append(Finding(WARNING, f"Coolant reached {top:.0f}°C", "Hotter than the usual 85–100°C."))

    rough = [n for n in (f"Roughness Monitor Cylinder #{i}" for i in range(1, 5)) if L.has(n)]
    if rough:
        hits = [(n[-2:], int(np.nansum(L[n] > 0))) for n in rough]
        bad = [h for h in hits if h[1]]
        if bad:
            times = L.runs(np.any([L[n] > 0 for n in rough], axis=0), join_s=1.0)
            out.append(Finding(WARNING, "Misfire (roughness) counts on " + ", ".join(f"cyl {c}" for c, _ in bad),
                               "Samples with roughness > 0: " + ", ".join(f"cyl {c}: {k}" for c, k in hits)
                               + ". One cylinder standing out suggests an injector, coil or plug; a lean "
                               "condition affects all four.", times))
        else:
            out.append(Finding(OK, "No misfire roughness counts", "All four cylinders read 0."))

    if L.has("Knock Signal #1"):
        k = L.runs(L["Knock Signal #1"] > 0, join_s=0.3)
        if len(k) > 5:
            out.append(Finding(INFO, f"Knock signal seen {len(k)} times", f"At {_fmt_ranges(k)}.", k))
    if L.has("Learned Ignition Timing"):
        lit = float(np.nanmin(L["Learned Ignition Timing"]))
        if lit < -2:
            out.append(Finding(WARNING, f"Learned ignition timing down to {lit:.1f}°",
                               "The ECU has pulled timing for knock - low-octane fuel, carbon or running lean "
                               "under load."))

    if L.has("Engine Speed"):
        ol = S["running"] & ~S["closed"] & ~S["fuel_cut"]
        if S["warm"].any():
            ol &= S["warm"]
        r = L.runs(ol, min_s=1.0)
        if r:
            out.append(Finding(INFO, f"Open loop (warm) {len(r)} time(s)",
                               f"At {_fmt_ranges(r)}. Normal at high load or after a fuel cut; the short-term "
                               "trim reads 0 and the ECU ignores the sensors then.", r))

    gaps = np.flatnonzero(np.diff(L.t) > 1.0)
    if len(gaps):
        rr = [(float(L.t[i]), float(L.t[i + 1])) for i in gaps]
        out.append(Finding(INFO, f"{len(rr)} gap(s) in the log over 1 s",
                           f"At {_fmt_ranges(rr)} - lost connection to the ECU.", rr))
    return out


CHECKS = [check_trims, check_sensors, check_maf, check_electrical, check_engine]


def analyse(t: np.ndarray, series: dict[str, np.ndarray]) -> list[Finding]:
    L = Log(t, {k: np.asarray(v, dtype=float) for k, v in series.items()})
    if len(L.t) < 10:
        return [Finding(INFO, "Too little data", "Log at least a minute of driving before analysing.")]
    S = _states(L)
    if L.duration(S["running"]) < 10:
        return [Finding(INFO, "Engine not running in this log",
                        "No samples with the engine above 400 RPM.")]
    findings: list[Finding] = []
    for check in CHECKS:
        try:
            findings += check(L, S)
        except Exception as e:                   # one broken check shouldn't hide the rest
            findings.append(Finding(INFO, f"{check.__name__} failed", repr(e)))
    findings.sort(key=lambda f: LEVEL_ORDER[f.level])
    return findings


def format_findings(findings: list[Finding]) -> str:
    lines = []
    for f in findings:
        lines.append(f"{LEVEL_MARK[f.level]} [{f.level.upper()}] {f.title}")
        lines.append("    " + f.detail)
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    import recording
    if len(argv) != 1:
        print(__doc__)
        return 2
    rec = recording.load_csv(argv[0])
    series = {c.name: rec.y[i] for i, c in enumerate(rec.channels)}
    dur = rec.t[-1] - rec.t[0]
    print(f"{rec.path.name}: {dur / 60:.1f} min, {len(rec.t)} rows\n")
    print(format_findings(analyse(rec.t, series)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
