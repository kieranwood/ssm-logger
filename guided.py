"""Guided tests: short scripted checks that tell you what to do, then judge the logged data.

Each test lists its steps; the app records from Start to Finish (or you run it on part of a saved
recording) and evaluate() turns that stretch of data into findings, shown like the Analyse
results. Thresholds are rules of thumb taken from the engine profile (profiles.py) - hints, not
a diagnosis.

Used by logger.py (desktop) and web/webapp.py (browser).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

import profiles
from analyse import INFO, OK, PROBLEM, WARNING, Finding, Log, _fmt_ranges, _states, check_boost
from profiles import Profile


@dataclass
class GuidedTest:
    key: str
    name: str
    purpose: str
    steps: list[str]
    needs: list[str]                     # channels the test can't run without
    evaluate: Callable[[Log, Profile], list[Finding]]


def _median(v: np.ndarray, mask: np.ndarray) -> float:
    v = v[mask]
    v = v[np.isfinite(v)]
    return float(np.median(v)) if len(v) else float("nan")


# ---------------------------------------------------------------- warm-up and thermostat
def _thermostat(L: Log, P: Profile) -> list[Finding]:
    S = _states(L, P)
    warm = P.warm_c
    run = S["running"]
    cool = L["Coolant Temperature"]
    if L.duration(run) < 60:
        return [Finding(INFO, "Not enough running time", "Log at least 10-15 minutes from a cold start.")]
    t_run = L.t[run]
    c_run = cool[run]
    start = float(c_run[0])
    out = []
    if start > 60:
        out.append(Finding(INFO, f"Engine wasn't cold at the start ({start:.0f} °C)",
                           "For a meaningful warm-up time, start after the car has sat for a few hours."))
    peak = float(np.nanmax(c_run))
    minutes = (t_run[-1] - t_run[0]) / 60
    reached = np.flatnonzero(c_run >= warm)
    if peak > P.hot_c:
        out.append(Finding(PROBLEM, f"Coolant reached {peak:.0f} °C",
                           f"Above about {P.hot_c:.0f} °C is overheating: check coolant level, the radiator "
                           "fan, and for a thermostat stuck closed.", L.runs(run & (cool > P.hot_c))))
    if not len(reached):
        level = PROBLEM if minutes >= 15 else WARNING
        out.append(Finding(level, f"Coolant never reached {warm:.0f} °C (peak {peak:.0f} °C after "
                           f"{minutes:.0f} min)",
                           f"These engines normally run at about {P.running_c[0]:.0f}-{P.running_c[1]:.0f} °C "
                           "once warm. Staying cooler after "
                           "15+ minutes of driving points to a thermostat stuck open (code P0128). "
                           "On a cold day at idle it can take longer - drive rather than idle."))
        return out
    t80 = (t_run[reached[0]] - t_run[0]) / 60
    if start < 40 and t80 > 15:
        out.append(Finding(WARNING, f"Slow warm-up: {t80:.0f} min to reach {warm:.0f} °C",
                           "Usually under about 10-15 minutes of driving from cold. A thermostat "
                           "opening too early is the common cause."))
    else:
        out.append(Finding(OK, f"Reached {warm:.0f} °C after {t80:.1f} min (from {start:.0f} °C)",
                           "A normal warm-up."))
    after = run & (L.t >= t_run[reached[0]] + 60)
    if L.duration(after) >= 60:
        low = float(np.nanmin(cool[after]))
        typ = _median(cool, after)
        if low < warm - 5:
            out.append(Finding(WARNING, f"Coolant fell back to {low:.0f} °C after warming up",
                               f"Once warm it should stay roughly steady. Dropping well below {warm:.0f} °C while "
                               "driving (especially at steady speed) suggests a thermostat that "
                               "doesn't close fully.", L.runs(after & (cool < warm - 5))))
        else:
            out.append(Finding(OK, f"Steady once warm (typically {typ:.0f} °C, lowest {low:.0f} °C)",
                               "The thermostat is holding the temperature."))
    return out


# ---------------------------------------------------------------- front A/F sensor response
def _afr_response(L: Log, P: Profile) -> list[Finding]:
    S = _states(L, P)
    afr = L["Air/Fuel Sensor #1 AFR"]
    cuts = L.runs(S["fuel_cut"], min_s=0.4, join_s=0.3)
    if not cuts:
        return [Finding(INFO, "No fuel cut found",
                        "Lift your foot fully off the pedal at speed (in gear, above about 2000 rpm) for "
                        "3 seconds or more, then try again.")]
    out = []
    peaks, delays = [], []
    for a, b in cuts:
        win = (L.t >= a) & (L.t <= b + 2.0)
        peak = float(np.nanmax(afr[win]))
        peaks.append(peak)
        over = np.flatnonzero(win & (afr > 16))
        delays.append(L.t[over[0]] - a if len(over) else np.inf)
    worst_peak, worst_delay = min(peaks), max(delays)
    detail = (f"{len(cuts)} fuel cut(s) at {_fmt_ranges(cuts)}. A healthy wideband sensor swings well "
              f"lean (past 18 AFR) within about a second of the injectors switching off.")
    if worst_peak < 16:
        out.append(Finding(PROBLEM, f"Front A/F sensor barely reacted (peak {worst_peak:.1f} AFR)", detail, cuts))
    elif worst_peak < 18:
        out.append(Finding(WARNING, f"Front A/F sensor reacted weakly (peak {worst_peak:.1f} AFR)", detail, cuts))
    else:
        out.append(Finding(OK, f"Front A/F sensor swung to {worst_peak:.1f} AFR or more", detail, cuts))
    if np.isfinite(worst_delay):
        if worst_delay > 2.0:
            out.append(Finding(PROBLEM, f"Slow response: {worst_delay:.1f} s to pass 16 AFR", detail, cuts))
        elif worst_delay > 1.0:
            out.append(Finding(WARNING, f"Sluggish response: {worst_delay:.1f} s to pass 16 AFR", detail, cuts))
        else:
            out.append(Finding(OK, f"Quick response: {worst_delay:.2f} s to pass 16 AFR", detail, cuts))
    if L.has("Rear O2 Sensor"):
        lows = [float(np.nanmin(L["Rear O2 Sensor"][(L.t >= a) & (L.t <= b + 2.0)])) for a, b in cuts]
        if max(lows) > 0.3:
            out.append(Finding(WARNING, f"Rear O2 sensor stayed above {max(lows):.2f} V during a fuel cut",
                               "With no fuel the exhaust is pure air, so the rear sensor should drop below "
                               "about 0.2 V within a few seconds. Try a longer lift-off; if it still stays "
                               "up, the rear sensor may be slow.", cuts))
        else:
            out.append(Finding(OK, f"Rear O2 sensor dropped to {max(lows):.2f} V or below in every cut",
                               "The rear sensor responds too."))
    return out


# ---------------------------------------------------------------- vacuum leak or sensor
def _leak_vs_sensor(L: Log, P: Profile) -> list[Finding]:
    S = _states(L, P)
    trim = L["Total Fuel Trim"]
    rpm = L["Engine Speed"]
    base = S["closed"] & S["warm"]
    idle = base & (rpm < 1000)
    high = base & (rpm >= P.leak_rpm[0]) & (rpm <= P.leak_rpm[1])
    if L.duration(idle) < 15 or L.duration(high) < 15:
        return [Finding(INFO, "Need both a warm idle and a steady 2500 rpm",
                        f"Got {L.duration(idle):.0f} s of warm closed-loop idle and {L.duration(high):.0f} s "
                        f"near 2500 rpm; at least 15 s of each is needed (engine above {P.warm_c:.0f} °C).")]
    ti, th = _median(trim, idle), _median(trim, high)
    detail = f"Total fuel trim: {ti:+.1f}% at idle, {th:+.1f}% at 2500 rpm."
    if L.has("Rear O2 Sensor"):
        detail += f" Rear O2 median {_median(L['Rear O2 Sensor'], base):.2f} V."
    if ti > 10 and ti - th >= 8:
        return [Finding(PROBLEM, "Lean mainly at idle - points to a vacuum leak",
                        detail + " Unmetered air makes the biggest difference when total airflow is "
                        "smallest, so the trim falls as rpm rises. Check intake boots, PCV hoses and the "
                        "brake booster hose.")]
    if th > 10 and th - ti >= 8:
        return [Finding(PROBLEM, "Lean mainly at higher airflow - points to the MAF or fuel delivery",
                        detail + " A MAF reading low, or a weak fuel pump or clogged filter, shows up "
                        "more as airflow rises.")]
    if ti > 10 and th > 10:
        return [Finding(PROBLEM, "Lean by about the same amount at all airflows",
                        detail + " The same correction everywhere points away from a vacuum leak, "
                        "towards fuel pressure - or a front A/F sensor reading lean (if the rear O2 "
                        "reads rich at the same time, suspect the sensor or an exhaust leak before it).")]
    if ti < -10 or th < -10:
        return [Finding(WARNING, "Running rich", detail + " Negative trims mean the ECU is removing fuel: "
                        "leaking injectors, high fuel pressure, or a purge valve stuck open.")]
    return [Finding(OK, "Fuel trims normal at idle and 2500 rpm", detail + " Within ±10% at both.")]


# ---------------------------------------------------------------- battery and charging
def _charging(L: Log, P: Profile) -> list[Finding]:
    v = L["Battery Voltage"]
    rpm = L["Engine Speed"]
    off = rpm < 100
    on = rpm > 500
    out = []
    first_on = np.flatnonzero(on)
    if not len(first_on):
        return [Finding(INFO, "The engine wasn't started during the test",
                        "Start with the ignition on and engine off, then start the engine.")]
    t_start = L.t[first_on[0]]
    rest = off & (L.t < t_start)
    if L.duration(rest) >= 3:
        r = _median(v, rest)
        level = OK if r >= 12.4 else WARNING if r >= 12.0 else PROBLEM
        text = {OK: "A healthy, charged battery.", WARNING: "Partly discharged - or the headlights "
                "were on.", PROBLEM: "Low: discharged or failing battery."}[level]
        out.append(Finding(level, f"Battery at rest: {r:.2f} V", "About 12.6 V is fully charged. " + text))
    crank = (L.t >= t_start - 5) & (L.t <= t_start + 1) & ~on
    if crank.any():
        low = float(np.nanmin(v[crank]))
        level = OK if low >= 9.6 else WARNING
        out.append(Finding(level, f"Lowest while cranking: {low:.1f} V",
                           "Should stay above about 9.6 V. The logger may miss the very lowest point, "
                           "so treat a good reading as reassurance rather than proof."))
    else:
        out.append(Finding(INFO, "No samples while cranking", "The ECU often stops answering while the "
                           "starter runs; the rest of the test still applies."))
    marks_t = getattr(L, "marks", [])
    load_t = next((t for t in marks_t if t > t_start), None)
    idle = on & (L.t >= t_start + 5) & ((L.t < load_t) if load_t else np.ones(len(L.t), bool))
    if L.duration(idle) >= 5:
        c = _median(v, idle)
        level = OK if 13.5 <= c <= 15.0 else PROBLEM
        text = ("Normal." if level == OK else
                "Too low: the alternator isn't charging properly (belt, alternator or wiring)." if c < 13.5
                else "Too high: overcharging (voltage regulator).")
        out.append(Finding(level, f"Charging at idle: {c:.2f} V", "Expect about 13.5-14.8 V. " + text))
    if load_t:
        loaded = on & (L.t >= load_t + 2)
        if L.duration(loaded) >= 5:
            low = float(np.nanmin(v[loaded]))
            level = OK if low >= 13.0 else WARNING
            out.append(Finding(level, f"Under electrical load: lowest {low:.2f} V",
                               "With headlights, blower and demister on, it should stay above about 13 V."))
    else:
        out.append(Finding(INFO, "No load step marked", "Press Mark (M) as you switch on the headlights, "
                           "blower and rear demister to check charging under load."))
    return out


# ---------------------------------------------------------------- boost pull (turbo)
def _boost_pull(L: Log, P: Profile) -> list[Finding]:
    if not P.turbo:
        return [Finding(INFO, "This test is for turbo engines",
                        f"The engine profile is {P.name}. If your engine is a turbo, choose the Turbo "
                        "engine profile and run the test again.")]
    S = _states(L, P)
    out = check_boost(L, S, P)
    if L.has("Intake Air Temperature"):
        iat = L["Intake Air Temperature"]
        hot = float(np.nanmax(iat[S["running"]])) if S["running"].any() else float("nan")
        if hot > 55:
            out.append(Finding(WARNING, f"Intake air reached {hot:.0f} °C",
                               "Hot intake air (heat soak, a dirty or blocked intercooler) makes knock more "
                               "likely and the ECU pulls timing. Let it cool and repeat after driving at speed."))
    return out or [Finding(INFO, "No boost data", "The ECU didn't report manifold pressure.")]


TESTS = [
    GuidedTest(
        "thermostat", "Warm-up and thermostat",
        "Checks how fast the engine warms up and that it then holds its temperature - a thermostat "
        "stuck open (P0128) also upsets fuel trims and economy.",
        ["Start with a cold engine (sat for a few hours).",
         "Press Start, then start the engine and drive normally for 15-20 minutes.",
         "Press Finish."],
        ["Coolant Temperature", "Engine Speed"], _thermostat),
    GuidedTest(
        "afr", "Front A/F sensor response",
        "Checks the front A/F sensor reacts quickly and fully when fuel is cut - a slow or lazy "
        "sensor causes lean/rich codes and poor trims.",
        ["Warm the engine fully (coolant above 80 °C).",
         "Press Start.",
         "Where it's safe, drive in a low gear at about 3000 rpm, then lift your foot fully off the "
         "pedal for 3-5 seconds. Repeat 2-3 times.",
         "Press Finish."],
        ["Air/Fuel Sensor #1 AFR", "Engine Speed", "Fuel Injector #1 Pulse Width"], _afr_response),
    GuidedTest(
        "leak", "Vacuum leak or sensor? (idle vs 2500 rpm)",
        "Compares fuel trims at idle and at 2500 rpm: where the engine runs lean tells a vacuum leak "
        "apart from a MAF, fuel delivery or sensor problem.",
        ["Warm the engine fully (coolant above 80 °C), parked in neutral/park.",
         "Press Start and let it idle for 60 seconds.",
         "Hold a steady 2500 rpm for 60 seconds.",
         "Let it idle for 20 seconds, then press Finish."],
        ["Total Fuel Trim", "Engine Speed", "Coolant Temperature"], _leak_vs_sensor),
    GuidedTest(
        "charging", "Battery and charging",
        "Checks the battery at rest, the voltage while cranking, and the alternator at idle and "
        "under electrical load.",
        ["Ignition on, engine off, all loads off.",
         "Press Start and wait 10 seconds.",
         "Start the engine and let it idle for 30 seconds.",
         "Switch on the headlights, blower on full and rear demister, and press Mark (M).",
         "Wait 30 seconds, switch everything off, then press Finish."],
        ["Battery Voltage", "Engine Speed"], _charging),
    GuidedTest(
        "boost", "Boost pull (turbo engines)",
        "Checks peak boost, that the engine runs rich enough under boost, and whether the ECU pulls "
        "timing for knock.",
        ["Warm the engine fully, and use the fuel you normally run.",
         "Press Start.",
         "Only where it's safe and legal: in 3rd gear from about 2500 rpm, go to full throttle and hold "
         "it to about 5500 rpm, then lift off.",
         "Press Finish."],
        ["Manifold Relative Pressure", "Engine Speed"], _boost_pull),
]
BY_KEY = {t.key: t for t in TESTS}


def run(key: str, t: np.ndarray, series: dict[str, np.ndarray],
        marks: list[float] | None = None, profile: Profile | str | None = None) -> list[Finding]:
    """Evaluate test `key` on the data between Start and Finish.

    profile: a Profile, a profile key, or None/"auto" to pick one from the channels."""
    test = BY_KEY[key]
    L = Log(np.asarray(t, float), {k: np.asarray(v, float) for k, v in series.items()})
    L.marks = sorted(marks or [])
    missing = [n for n in test.needs if not L.has(n)]
    if missing:
        return [Finding(INFO, "This test needs channels that weren't logged", ", ".join(missing))]
    if len(L.t) < 10:
        return [Finding(INFO, "Too little data", "Press Start, follow the steps, then press Finish.")]
    P = profile if isinstance(profile, Profile) else profiles.get(profile, L.s)
    return test.evaluate(L, P)
