"""OBD-II diagnostic trouble codes over the same ISO15765 channel used for SSM.

    Mode 03 -> 43 n [code pairs]   stored (confirmed) codes - these light the CEL
    Mode 07 -> 47 n [code pairs]   pending codes - seen once, not yet confirmed
    Mode 0A -> 4A n [code pairs]   permanent codes - cleared only by the ECU itself
    Mode 04 -> 44                  clear codes, freeze frame and readiness monitors

Command line use (when the logger is not running):
    .venv/bin/python dtc.py            read codes
    .venv/bin/python dtc.py --clear    clear codes (asks for confirmation)
    .venv/bin/python dtc.py --port /dev/ttyACM0   use a specific serial device
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from typing import Callable

NRC = 0x7F
NRC_RESPONSE_PENDING = 0x78
NRC_TEXT = {0x11: "not supported", 0x12: "not supported", 0x22: "conditions not correct",
            0x31: "out of range", 0x33: "security access denied"}

MODES = [  # (request mode, title)
    (0x03, "Stored"),
    (0x07, "Pending"),
    (0x0A, "Permanent"),
]

# Short descriptions for generic codes relevant to fuelling and sensors.
# Anything else is shown with its letter prefix; look it up in the service manual.
DESCRIPTIONS = {
    "P0030": "A/F sensor 1 heater control circuit (bank 1 sensor 1)",
    "P0031": "A/F sensor 1 heater circuit low",
    "P0032": "A/F sensor 1 heater circuit high",
    "P0037": "Rear O2 sensor heater circuit low",
    "P0038": "Rear O2 sensor heater circuit high",
    "P0101": "MAF sensor range/performance",
    "P0102": "MAF sensor circuit low",
    "P0103": "MAF sensor circuit high",
    "P0111": "Intake air temp sensor range/performance",
    "P0112": "Intake air temp sensor circuit low",
    "P0113": "Intake air temp sensor circuit high",
    "P0117": "Coolant temp sensor circuit low",
    "P0118": "Coolant temp sensor circuit high",
    "P0122": "Throttle position sensor A circuit low",
    "P0123": "Throttle position sensor A circuit high",
    "P0125": "Insufficient coolant temp for closed-loop fuel control",
    "P0128": "Coolant thermostat below regulating temperature",
    "P0130": "A/F sensor 1 circuit (bank 1 sensor 1)",
    "P0131": "A/F sensor 1 circuit low voltage",
    "P0132": "A/F sensor 1 circuit high voltage",
    "P0133": "A/F sensor 1 slow response",
    "P0134": "A/F sensor 1 no activity detected",
    "P0136": "Rear O2 sensor circuit",
    "P0137": "Rear O2 sensor circuit low voltage",
    "P0138": "Rear O2 sensor circuit high voltage",
    "P0139": "Rear O2 sensor slow response",
    "P0140": "Rear O2 sensor no activity detected",
    "P0171": "System too lean (bank 1)",
    "P0172": "System too rich (bank 1)",
    "P0300": "Random/multiple cylinder misfire",
    "P0301": "Cylinder 1 misfire", "P0302": "Cylinder 2 misfire",
    "P0303": "Cylinder 3 misfire", "P0304": "Cylinder 4 misfire",
    "P0335": "Crankshaft position sensor circuit",
    "P0340": "Camshaft position sensor circuit",
    "P0420": "Catalyst efficiency below threshold (bank 1)",
    "P0441": "EVAP purge flow incorrect",
    "P0456": "EVAP very small leak",
    "P0457": "EVAP leak (fuel cap loose/off)",
    "P0458": "EVAP purge valve circuit low",
    "P0459": "EVAP purge valve circuit high",
    "P0506": "Idle speed lower than expected",
    "P0507": "Idle speed higher than expected",
    "P0562": "System voltage low",
    "P0563": "System voltage high",
    "P2096": "Post-catalyst fuel trim too lean",
    "P2097": "Post-catalyst fuel trim too rich",
    "P2195": "A/F sensor 1 signal stuck lean",
    "P2196": "A/F sensor 1 signal stuck rich",
    "P2440": "Secondary air injection valve stuck open",
}


# A plain-English line or two for the codes most likely to come up while chasing fuelling faults.
EXPLANATIONS = {
    "P0171": "The ECU is adding more fuel than its normal limit to hold the target mixture. That usually "
             "means air getting in that the MAF didn't measure (a vacuum or intake leak), an exhaust leak "
             "ahead of the front A/F sensor, or too little fuel.",
    "P0172": "The ECU is removing more fuel than its normal limit. Common causes are a leaking injector, "
             "high fuel pressure, a MAF reading high, or a biased front A/F sensor.",
    "P0133": "The front A/F sensor responds too slowly to mixture changes - usually an ageing sensor, "
             "or an exhaust leak near it.",
    "P2195": "The front A/F sensor keeps reporting lean while the ECU corrects. A failing sensor or an "
             "exhaust leak before it can cause this.",
    "P2196": "The front A/F sensor keeps reporting rich while the ECU corrects. Often the sensor itself.",
    "P2096": "The rear O2 sensor sees a leaner mixture after the catalyst than the front sensor reports - "
             "often an exhaust leak between them, or a biased front sensor.",
    "P2097": "The rear O2 sensor sees a richer mixture after the catalyst than expected - often a biased "
             "front A/F sensor.",
    "P0420": "The catalyst isn't storing oxygen as well as it should, judged by the rear O2 sensor. Fix any "
             "fuelling or exhaust-leak problems first: they can set this code on a good catalyst.",
    "P0300": "Misfires on more than one cylinder. A lean condition, low fuel pressure or a vacuum leak "
             "affects all cylinders; plugs or coils usually affect one.",
    "P0128": "The coolant took too long to reach operating temperature - usually a thermostat stuck open.",
    "P0457": "The EVAP system found a large leak, most often a loose or missing fuel cap.",
}
PERMANENT_NOTE = ("A permanent code can't be cleared from here: the ECU removes it by itself once its own "
                  "check passes on later drives.")
PENDING_NOTE = "Pending means the ECU has seen the fault once but hasn't confirmed it on a second drive yet."
SYSTEMS = {"P": "Powertrain", "C": "Chassis", "B": "Body", "U": "Network"}


def code_kind(code: str) -> str:
    """Generic (SAE) or manufacturer-specific, from the code's second character."""
    return "Generic OBD-II code" if code[1:2] in ("0", "2") else "Manufacturer-specific code"


@dataclass
class DtcResult:
    title: str
    codes: list[str]
    error: str = ""


def decode_code(b1: int, b2: int) -> str:
    return "PCBU"[b1 >> 6] + f"{(b1 >> 4) & 3}{b1 & 0xF:X}{b2 >> 4:X}{b2 & 0xF:X}"


def describe(code: str) -> str:
    return DESCRIPTIONS.get(code, "")


def parse_codes(mode: int, reply: bytes) -> list[str]:
    """Parse a 43/47/4A reply: <mode+0x40> <count> <pairs...>."""
    if not reply or reply[0] != mode + 0x40:
        raise ValueError(f"unexpected reply {reply.hex(' ') if reply else None}")
    body = reply[2:]           # skip mode echo and count byte (ISO 15765-4 format)
    codes = []
    for i in range(0, len(body) - 1, 2):
        if body[i] or body[i + 1]:
            codes.append(decode_code(body[i], body[i + 1]))
    return codes


def _nrc(reply: bytes) -> str:
    return NRC_TEXT.get(reply[2], f"negative response 0x{reply[2]:02X}") if len(reply) > 2 else "negative response"


def exchange(request: Callable[[bytes, float], bytes | None], payload: bytes,
             timeout: float = 2.0) -> bytes | None:
    """Send an OBD request, waiting through 'response pending' replies."""
    reply = request(payload, timeout)
    tries = 0
    while reply and reply[0] == NRC and len(reply) > 2 and reply[2] == NRC_RESPONSE_PENDING and tries < 10:
        reply = request(b"", timeout)   # empty payload = just wait for the next reply
        tries += 1
    return reply


def read_all(request: Callable[[bytes, float], bytes | None],
             modes: tuple[int, ...] | None = None) -> list[DtcResult]:
    """Read every code type, or only the given request modes (e.g. (0x03, 0x07))."""
    results = []
    for mode, title in MODES:
        if modes is not None and mode not in modes:
            continue
        reply = exchange(request, bytes([mode]), timeout=1.0)
        if reply is None:
            # Some ECUs (e.g. 2012 Forester 7152345007 for mode 0A) just stay silent.
            results.append(DtcResult(title, [], "not supported by this ECU (no reply)"))
        elif reply[0] == NRC:
            results.append(DtcResult(title, [], _nrc(reply)))
        else:
            try:
                results.append(DtcResult(title, parse_codes(mode, reply)))
            except ValueError as e:
                results.append(DtcResult(title, [], str(e)))
    return results


def clear(request: Callable[[bytes, float], bytes | None]) -> str:
    """Returns "" on success, otherwise an error message."""
    reply = exchange(request, b"\x04", timeout=5.0)
    if reply is None:
        return "no reply from the ECU"
    if reply[0] == NRC:
        return _nrc(reply)
    if reply[0] != 0x44:
        return f"unexpected reply {reply.hex(' ')}"
    return ""


def format_results(results: list[DtcResult]) -> str:
    lines = []
    for r in results:
        if r.error:
            lines.append(f"{r.title}: ({r.error})")
        elif not r.codes:
            lines.append(f"{r.title}: none")
        else:
            lines.append(f"{r.title}:")
            lines += [f"  {c}  {describe(c)}".rstrip() for c in r.codes]
    return "\n".join(lines)


# ---------------------------------------------------------------- mode 01 status
# Spark-ignition readiness monitors in PID 01/41 bytes C (supported) and D (incomplete).
SPARK_MONITORS = ["Catalyst", "Heated catalyst", "EVAP system", "Secondary air",
                  "A/C refrigerant", "O2 / A/F sensors", "O2 / A/F sensor heaters", "EGR / VVT"]
# Continuous monitors in byte B: bits 0-2 supported, bits 4-6 incomplete.
CONTINUOUS_MONITORS = ["Misfire", "Fuel system", "Components"]
FUEL_SYSTEM_STATUS = {
    0x00: "not reported",
    0x01: "open loop - engine not warm enough yet",
    0x02: "closed loop - using the front A/F sensor",
    0x04: "open loop - high load or deceleration",
    0x08: "open loop - SYSTEM FAULT",
    0x10: "closed loop - but a FAULT in at least one O2 sensor",
}


@dataclass
class Monitor:
    name: str
    since_clear: str       # "complete", "NOT complete", or "not supported"
    this_drive: str = ""


@dataclass
class ObdStatus:
    mil_on: bool | None = None
    stored_count: int | None = None
    monitors: list[Monitor] | None = None
    fuel_system: str | None = None
    km_since_clear: int | None = None
    minutes_since_clear: int | None = None
    warmups_since_clear: int | None = None
    km_with_mil: int | None = None
    errors: list[str] | None = None


def _supported_pids(request) -> set[int]:
    pids: set[int] = set()
    for base in (0x00, 0x20, 0x40):
        if base and base not in pids:
            break
        reply = exchange(request, bytes([0x01, base]), timeout=1.0)
        if not reply or reply[0] != 0x41 or len(reply) < 6 or reply[1] != base:
            break
        bits = int.from_bytes(reply[2:6], "big")
        for i in range(32):
            if bits & (1 << (31 - i)):
                pids.add(base + 1 + i)
    return pids


def _pid(request, pid: int) -> bytes | None:
    reply = exchange(request, bytes([0x01, pid]), timeout=1.0)
    if reply and len(reply) >= 2 and reply[0] == 0x41 and reply[1] == pid:
        return reply[2:]
    return None


def _monitor_table(b: int, c: int, d: int) -> dict[str, str]:
    out = {}
    for i, name in enumerate(CONTINUOUS_MONITORS):
        if b & (1 << i):
            out[name] = "NOT complete" if b & (1 << (i + 4)) else "complete"
        else:
            out[name] = "not supported"
    for i, name in enumerate(SPARK_MONITORS):
        if c & (1 << i):
            out[name] = "NOT complete" if d & (1 << i) else "complete"
        else:
            out[name] = "not supported"
    return out


def read_status(request: Callable[[bytes, float], bytes | None]) -> ObdStatus:
    st = ObdStatus(errors=[])
    pids = _supported_pids(request)
    if not pids:
        st.errors.append("ECU did not report its supported OBD data")
        return st

    since = this = None
    if 0x01 in pids and (d := _pid(request, 0x01)) and len(d) >= 4:
        st.mil_on = bool(d[0] & 0x80)
        st.stored_count = d[0] & 0x7F
        since = _monitor_table(d[1], d[2], d[3])
    if 0x41 in pids and (d := _pid(request, 0x41)) and len(d) >= 4:
        this = _monitor_table(d[1], d[2], d[3])
    if since:
        st.monitors = [Monitor(n, s, (this or {}).get(n, ""))
                       for n, s in since.items() if s != "not supported"]

    if 0x03 in pids and (d := _pid(request, 0x03)):
        st.fuel_system = FUEL_SYSTEM_STATUS.get(d[0], f"unknown (0x{d[0]:02X})")
    if 0x31 in pids and (d := _pid(request, 0x31)) and len(d) >= 2:
        st.km_since_clear = int.from_bytes(d[:2], "big")
    if 0x4E in pids and (d := _pid(request, 0x4E)) and len(d) >= 2:
        st.minutes_since_clear = int.from_bytes(d[:2], "big")
    if 0x30 in pids and (d := _pid(request, 0x30)):
        st.warmups_since_clear = d[0]
    if 0x21 in pids and (d := _pid(request, 0x21)) and len(d) >= 2:
        st.km_with_mil = int.from_bytes(d[:2], "big")
    return st


def status_summary(st: ObdStatus) -> list[tuple[str, str]]:
    """(label, value) rows for display."""
    rows = []
    if st.mil_on is not None:
        rows.append(("Check-engine light", "ON" if st.mil_on else "off"))
    if st.km_since_clear is not None:
        rows.append(("Distance since codes cleared",
                     f"{st.km_since_clear} km ({st.km_since_clear * 0.621371:.0f} miles)"))
    if st.minutes_since_clear is not None:
        h, m = divmod(st.minutes_since_clear, 60)
        rows.append(("Engine run time since cleared", f"{h} h {m} min"))
    if st.warmups_since_clear is not None:
        rows.append(("Warm-ups since cleared", str(st.warmups_since_clear)))
    if st.km_with_mil:
        rows.append(("Distance with light on", f"{st.km_with_mil} km"))
    if st.fuel_system is not None:
        rows.append(("Fuel system (right now)", st.fuel_system))
    return rows


def format_status(st: ObdStatus) -> str:
    lines = [f"{label}: {value}" for label, value in status_summary(st)]
    if st.monitors:
        lines.append("\nReadiness monitors   since cleared   this drive")
        lines += [f"  {m.name:22s} {m.since_clear:15s} {m.this_drive}" for m in st.monitors]
    lines += [f"({e})" for e in st.errors or []]
    return "\n".join(lines)


# ---------------------------------------------------------------- readiness: how to complete monitors
# General guidance, not Subaru's exact drive cycles (those are in the service manual).
MONITOR_HINTS = {
    "Misfire": "Runs continuously whenever the engine runs.",
    "Fuel system": "Runs continuously whenever the engine runs.",
    "Components": "Runs continuously whenever the engine runs.",
    "Catalyst": "Once fully warm, cruise steadily at 70-90 km/h for 5-10 minutes, then decelerate "
                "gently without braking hard. May need a few drives.",
    "Heated catalyst": "Runs during warm-up after a cold start.",
    "EVAP system": "Usually the slowest. Needs a cold start after the car has sat for 8+ hours "
                   "(overnight), fuel between 1/4 and 3/4, then idle and gentle driving. Can take "
                   "several days of normal use.",
    "Secondary air": "Runs during the first minute or two after a cold start.",
    "A/C refrigerant": "Not used on most cars.",
    "O2 / A/F sensors": "Several minutes of steady cruise once warm, plus a few long decelerations "
                        "with your foot fully off the pedal (fuel cut).",
    "O2 / A/F sensor heaters": "Usually completes within a few minutes of a cold start.",
    "EGR / VVT": "Steady cruise with some gentle accelerations and decelerations once warm.",
}


# ---------------------------------------------------------------- mode 09 vehicle information
IUPR_NAMES = ["Catalyst bank 1", "Catalyst bank 2", "O2 sensor bank 1", "O2 sensor bank 2",
              "EGR / VVT", "Secondary air", "EVAP", "Rear O2 sensor bank 1", "Rear O2 sensor bank 2"]


@dataclass
class VehicleInfo:
    vin: str = ""
    calibration_ids: list[str] | None = None
    cvns: list[str] | None = None
    ecu_name: str = ""
    # In-use performance: (monitor, times completed, times conditions were met)
    iupr: list[tuple[str, int, int]] | None = None
    obd_conditions: int | None = None       # drives that met the general OBD conditions
    ignition_cycles: int | None = None
    error: str = ""


def _info(request, infotype: int) -> bytes | None:
    """Data of a mode 09 reply, after 49 <type> <number of data items>."""
    reply = exchange(request, bytes([0x09, infotype]), timeout=1.0)
    if reply and len(reply) >= 3 and reply[0] == 0x49 and reply[1] == infotype:
        return reply[3:]
    return None


def _ascii(b: bytes) -> str:
    return b.replace(b"\x00", b"").decode("ascii", "replace").strip()


def read_vehicle_info(request: Callable[[bytes, float], bytes | None]) -> VehicleInfo:
    vi = VehicleInfo()
    reply = exchange(request, b"\x09\x00", timeout=1.0)
    if not reply or reply[0] != 0x49 or len(reply) < 6:
        reason = _nrc(reply) if reply and reply[0] == NRC else "no reply"
        vi.error = f"not supported by this ECU ({reason})"
        return vi
    bits = int.from_bytes(reply[2:6], "big")
    supported = {1 + i for i in range(32) if bits & (1 << (31 - i))}
    if 0x02 in supported and (d := _info(request, 0x02)):
        vi.vin = _ascii(d)
    if 0x04 in supported and (d := _info(request, 0x04)):
        vi.calibration_ids = [_ascii(d[i:i + 16]) for i in range(0, len(d) - 15, 16)]
    if 0x06 in supported and (d := _info(request, 0x06)):
        vi.cvns = [d[i:i + 4].hex().upper() for i in range(0, len(d) - 3, 4)]
    if 0x0A in supported and (d := _info(request, 0x0A)):
        vi.ecu_name = _ascii(d).replace("\x00", "")
    if 0x08 in supported and (d := _info(request, 0x08)) and len(d) >= 4:
        counts = [int.from_bytes(d[i:i + 2], "big") for i in range(0, len(d) - 1, 2)]
        vi.obd_conditions, vi.ignition_cycles = counts[0], counts[1]
        pairs = counts[2:]
        vi.iupr = [(IUPR_NAMES[k], pairs[2 * k], pairs[2 * k + 1])
                   for k in range(min(len(IUPR_NAMES), len(pairs) // 2))
                   if pairs[2 * k] or pairs[2 * k + 1]]
    return vi


def format_vehicle_info(vi: VehicleInfo, include_vin: bool = True) -> str:
    if vi.error:
        return f"Vehicle information: ({vi.error})"
    lines = ["Vehicle information (mode 09):"]
    if vi.vin:
        lines.append(f"  VIN: {vi.vin if include_vin else '(hidden)'}")
    if vi.ecu_name:
        lines.append(f"  ECU: {vi.ecu_name}")
    for i, c in enumerate(vi.calibration_ids or []):
        cvn = f"  (CVN {vi.cvns[i]})" if vi.cvns and i < len(vi.cvns) else ""
        lines.append(f"  Calibration ID: {c}{cvn}")
    if vi.iupr:
        lines.append(f"\n  How often each monitor gets to run ({vi.obd_conditions} qualifying drives, "
                     f"{vi.ignition_cycles} ignition cycles):")
        lines.append(f"    {'Monitor':24s} {'completed':>9s} {'chances':>8s}  ratio")
        for name, comp, cond in vi.iupr:
            ratio = f"{comp / cond:.2f}" if cond else "-"
            lines.append(f"    {name:24s} {comp:9d} {cond:8d}  {ratio}")
    return "\n".join(lines)


# ---------------------------------------------------------------- mode 06 on-board test results
# The measured value and pass/fail limits of each self-test the ECU has run (SAE J1979, CAN
# format). A value near a limit is an early warning, before the test fails and sets a code.

def _sensor_mids(base: int, kind: str) -> dict[int, str]:
    return {base + i: f"{kind} {name}" for i, name in enumerate(_SENSOR_NAMES)}


_SENSOR_NAMES = ["bank 1 sensor 1 (front)", "bank 1 sensor 2 (rear)", "bank 1 sensor 3", "bank 1 sensor 4",
                 "bank 2 sensor 1", "bank 2 sensor 2", "bank 2 sensor 3", "bank 2 sensor 4"]

OBDMID_NAMES: dict[int, str] = {
    **_sensor_mids(0x01, "O2 / A/F sensor"),
    0x21: "Catalyst bank 1", 0x22: "Catalyst bank 2",
    0x31: "EGR bank 1", 0x32: "EGR bank 2", 0x35: "VVT bank 1", 0x36: "VVT bank 2",
    0x39: "EVAP (cap off / 0.150\")", 0x3A: "EVAP (0.090\" leak)", 0x3B: "EVAP (0.040\" leak)",
    0x3C: "EVAP (0.020\" leak)", 0x3D: "EVAP purge flow",
    **_sensor_mids(0x41, "O2 / A/F sensor heater"),
    0x61: "Heated catalyst bank 1", 0x62: "Heated catalyst bank 2",
    0x71: "Secondary air 1", 0x72: "Secondary air 2",
    0x81: "Fuel system bank 1", 0x82: "Fuel system bank 2",
    0x85: "Boost pressure bank 1", 0x86: "Boost pressure bank 2",
    0xA1: "Misfire (all cylinders)",
    **{0xA2 + i: f"Misfire cylinder {i + 1}" for i in range(12)},
    0xB0: "Particulate filter bank 1", 0xB1: "Particulate filter bank 2",
}

# Standard test ids; 0x80 and up are manufacturer-defined (no published names).
TID_NAMES = {
    0x01: "Rich-to-lean threshold voltage", 0x02: "Lean-to-rich threshold voltage",
    0x03: "Low voltage for switch time", 0x04: "High voltage for switch time",
    0x05: "Rich-to-lean switch time", 0x06: "Lean-to-rich switch time",
    0x07: "Minimum voltage in test", 0x08: "Maximum voltage in test",
    0x09: "Time between transitions", 0x0A: "Sensor period",
    0x0B: "Misfire count, average of last 10 drives", 0x0C: "Misfire count, last/current drive",
}

# Unit and scaling ids: id -> (scale per bit, offset, unit). Ids 0x80+ are the signed versions.
UNITS: dict[int, tuple[float, float, str]] = {
    0x01: (1, 0, ""), 0x02: (0.1, 0, ""), 0x03: (0.01, 0, ""), 0x04: (0.001, 0, ""),
    0x05: (0.0000305, 0, ""), 0x06: (0.000305, 0, ""), 0x07: (0.25, 0, "rpm"),
    0x08: (0.01, 0, "km/h"), 0x09: (1, 0, "km/h"), 0x0A: (0.000122, 0, "V"), 0x0B: (0.001, 0, "V"),
    0x0C: (0.01, 0, "V"), 0x0D: (0.00390625, 0, "mA"), 0x0E: (0.001, 0, "A"), 0x0F: (0.01, 0, "A"),
    0x10: (0.001, 0, "s"), 0x11: (0.1, 0, "s"), 0x12: (1, 0, "s"), 0x13: (0.001, 0, "Ω"),
    0x14: (1, 0, "Ω"), 0x15: (1000, 0, "Ω"), 0x16: (0.1, -40, "°C"), 0x17: (0.01, 0, "kPa"),
    0x18: (0.0117, 0, "kPa"), 0x19: (0.079, 0, "kPa"), 0x1A: (1, 0, "kPa"), 0x1B: (10, 0, "kPa"),
    0x1C: (0.01, 0, "°"), 0x1D: (0.5, 0, "°"), 0x1E: (0.0000305, 0, "λ"), 0x1F: (0.05, 0, "AFR"),
    0x20: (0.0039062, 0, ""), 0x21: (0.001, 0, "Hz"), 0x22: (1, 0, "Hz"), 0x23: (1000, 0, "Hz"),
    0x24: (1, 0, "counts"), 0x25: (1, 0, "km"), 0x27: (0.01, 0, "g/s"), 0x28: (1, 0, "g/s"),
    0x2B: (1, 0, "switches"), 0x2F: (0.01, 0, "%"), 0x30: (0.001526, 0, "%"), 0x31: (0.001, 0, "L"),
    0x34: (1, 0, "min"), 0x35: (0.01, 0, "s"),
    0x81: (1, 0, ""), 0x82: (0.1, 0, ""), 0x83: (0.01, 0, ""), 0x84: (0.001, 0, ""),
    0x85: (0.0000305, 0, ""), 0x86: (0.000305, 0, ""), 0x8A: (0.000122, 0, "V"), 0x8B: (0.001, 0, "V"),
    0x8C: (0.01, 0, "V"), 0x8D: (0.00390625, 0, "mA"), 0x8E: (0.001, 0, "A"), 0x90: (0.001, 0, "s"),
    0x96: (0.1, 0, "°C"), 0x9C: (0.01, 0, "°"), 0x9D: (0.5, 0, "°"), 0xA8: (1, 0, "g/s"),
    0xAF: (0.01, 0, "%"), 0xB0: (0.003052, 0, "%"), 0xFC: (0.01, 0, "kPa"), 0xFD: (0.001, 0, "kPa"),
}
NEAR_LIMIT = 0.10      # flag values within 10% of a limit


@dataclass
class TestResult:
    mid: int
    tid: int
    uasid: int
    value: int        # raw; signed when uasid >= 0x80
    min: int
    max: int

    @property
    def signed(self) -> bool:
        return self.uasid >= 0x80

    @property
    def monitor(self) -> str:
        return OBDMID_NAMES.get(self.mid, f"Monitor 0x{self.mid:02X}")

    @property
    def test(self) -> str:
        if self.tid >= 0x80:
            return f"Manufacturer test 0x{self.tid:02X}"
        return TID_NAMES.get(self.tid, f"Test 0x{self.tid:02X}")

    def _bounds(self) -> tuple[int, int]:
        return (-32768, 32767) if self.signed else (0, 65535)

    @property
    def has_min(self) -> bool:
        return self.min > self._bounds()[0]

    @property
    def has_max(self) -> bool:
        return self.max < self._bounds()[1]

    @property
    def passed(self) -> bool:
        return self.min <= self.value <= self.max

    @property
    def near_limit(self) -> bool:
        """Inside the limits but within NEAR_LIMIT of one of them."""
        if not self.passed:
            return False
        lo, _ = self._bounds()
        if self.has_min and self.has_max:
            span = self.max - self.min
        elif self.has_max:
            span = self.max - lo
        elif self.has_min:
            span = abs(self.min)
        else:
            return False
        span = max(span, 1)
        return ((self.has_max and self.max - self.value <= NEAR_LIMIT * span) or
                (self.has_min and self.value - self.min <= NEAR_LIMIT * span))

    @property
    def status(self) -> str:
        return "FAILED" if not self.passed else "near limit" if self.near_limit else "ok"

    def fmt(self, raw: int) -> str:
        scale, offset, unit = UNITS.get(self.uasid, (1, 0, f"(unit 0x{self.uasid:02X})"))
        v = raw * scale + offset
        decimals = 0 if scale >= 1 else min(6, math.ceil(-math.log10(scale)))
        return f"{v:.{decimals}f} {unit}".rstrip()

    @property
    def limits(self) -> str:
        if self.has_min and self.has_max:
            return f"{self.fmt(self.min)} to {self.fmt(self.max)}"
        if self.has_max:
            return f"≤ {self.fmt(self.max)}"
        if self.has_min:
            return f"≥ {self.fmt(self.min)}"
        return "no limit"


@dataclass
class TestResults:
    tests: list[TestResult]
    error: str = ""


def _s16(hi: int, lo: int, signed: bool) -> int:
    return int.from_bytes(bytes([hi, lo]), "big", signed=signed)


def parse_test_records(mid: int, reply: bytes) -> list[TestResult]:
    """Parse a 46 reply: 46 then 9-byte records (OBDMID TID UASID value min max)."""
    out = []
    data = reply[1:]
    for i in range(0, len(data) - 8, 9):
        r = data[i:i + 9]
        if r[0] != mid:
            continue
        signed = r[2] >= 0x80
        out.append(TestResult(mid, r[1], r[2], _s16(r[3], r[4], signed), _s16(r[5], r[6], signed),
                              _s16(r[7], r[8], signed)))
    return out


def read_test_results(request: Callable[[bytes, float], bytes | None]) -> TestResults:
    mids: set[int] = set()
    for base in range(0x00, 0x100, 0x20):
        if base and base not in mids:
            break
        reply = exchange(request, bytes([0x06, base]), timeout=1.0)
        if not reply or reply[0] != 0x46 or len(reply) < 6 or reply[1] != base:
            if base == 0:
                reason = _nrc(reply) if reply and reply[0] == NRC else "no reply"
                return TestResults([], f"not supported by this ECU ({reason})")
            break
        bits = int.from_bytes(reply[2:6], "big")
        mids |= {base + 1 + i for i in range(32) if bits & (1 << (31 - i))}
    tests: list[TestResult] = []
    for mid in sorted(m for m in mids if m % 0x20):          # 0x20, 0x40... are support ranges
        reply = exchange(request, bytes([0x06, mid]), timeout=1.0)
        if reply and reply[0] == 0x46:
            tests += parse_test_records(mid, reply)
    return TestResults(tests)


def format_test_results(r: TestResults) -> str:
    if r.error:
        return f"On-board test results: ({r.error})"
    if not r.tests:
        return "On-board test results: none reported yet (tests run during driving)"
    failed = sum(not t.passed for t in r.tests)
    near = sum(t.near_limit for t in r.tests)
    lines = [f"On-board test results (mode 06): {len(r.tests)} tests, {failed} failed, {near} near a limit"]
    monitor = None
    for t in r.tests:
        if t.monitor != monitor:
            monitor = t.monitor
            lines.append(f"  {monitor}")
        mark = "" if t.status == "ok" else f"   <- {t.status.upper()}"
        lines.append(f"    {t.test:42s} {t.fmt(t.value):>14s}   limits {t.limits}{mark}")
    return "\n".join(lines)


# ---------------------------------------------------------------- mode 02 freeze frame
def _u16(d: bytes) -> int:
    return (d[0] << 8) | d[1]


def _o2_narrow(sensor: str) -> tuple:
    def f(d):
        text = f"{d[0] / 200:.3f} V"
        if len(d) > 1 and d[1] != 0xFF:
            text += f", trim {(d[1] - 128) * 100 / 128:+.1f}%"
        return text
    return (f"O2 sensor {sensor} voltage", f)


def _wideband_current(sensor: str) -> tuple:
    return (f"A/F sensor {sensor}",
            lambda d: f"lambda {_u16(d) * 2 / 65536:.3f} (AFR {_u16(d) * 2 / 65536 * 14.7:.2f}), "
                      f"current {_u16(d[2:]) / 256 - 128:+.2f} mA")


def _wideband_voltage(sensor: str) -> tuple:
    return (f"A/F sensor {sensor}",
            lambda d: f"lambda {_u16(d) * 2 / 65536:.3f} (AFR {_u16(d) * 2 / 65536 * 14.7:.2f}), "
                      f"{_u16(d[2:]) * 8 / 65536:.3f} V")



def _o2_present(d: bytes) -> str:
    """PID 13: bit n set = O2 sensor present, bits 0-3 bank 1 sensors 1-4, bits 4-7 bank 2."""
    found = [f"B{bank + 1}S{s + 1}" for bank in range(2) for s in range(4) if d[0] & (1 << (bank * 4 + s))]
    return ", ".join(found) or "none"


# PID 1C: which OBD standard the vehicle was built to (common values only).
OBD_STANDARDS = {
    0x01: "OBD-II (CARB)", 0x02: "OBD (EPA)", 0x03: "OBD and OBD-II", 0x04: "OBD-I",
    0x05: "not OBD compliant", 0x06: "EOBD (Europe)", 0x07: "EOBD and OBD-II", 0x08: "EOBD and OBD",
    0x09: "EOBD, OBD and OBD-II", 0x0A: "JOBD (Japan)", 0x0B: "JOBD and OBD-II", 0x0C: "JOBD and EOBD",
    0x0D: "JOBD, EOBD and OBD-II",
}


def _pct(d: bytes) -> str:
    return f"{d[0] * 100 / 255:.1f}%"


# Standard SAE J1979 PIDs: pid -> (name, decoder(data) -> text)
PIDS: dict[int, tuple[str, Callable[[bytes], str]]] = {
    0x03: ("Fuel system", lambda d: FUEL_SYSTEM_STATUS.get(d[0], f"unknown (0x{d[0]:02X})")),
    0x04: ("Engine load", lambda d: f"{d[0] * 100 / 255:.1f}%"),
    0x05: ("Coolant temperature", lambda d: f"{d[0] - 40} °C"),
    0x06: ("Short-term fuel trim", lambda d: f"{(d[0] - 128) * 100 / 128:+.1f}%"),
    0x07: ("Long-term fuel trim", lambda d: f"{(d[0] - 128) * 100 / 128:+.1f}%"),
    0x0A: ("Fuel pressure", lambda d: f"{d[0] * 3} kPa"),
    0x0B: ("Manifold pressure", lambda d: f"{d[0]} kPa"),
    0x0C: ("Engine speed", lambda d: f"{_u16(d) / 4:.0f} rpm"),
    0x0D: ("Vehicle speed", lambda d: f"{d[0]} km/h ({d[0] * 0.621371:.0f} mph)"),
    0x0E: ("Ignition timing", lambda d: f"{d[0] / 2 - 64:.1f}° BTDC"),
    0x0F: ("Intake air temperature", lambda d: f"{d[0] - 40} °C"),
    0x10: ("Mass air flow", lambda d: f"{_u16(d) / 100:.2f} g/s"),
    0x11: ("Throttle position", lambda d: f"{d[0] * 100 / 255:.1f}%"),
    0x13: ("O2 sensors present", _o2_present),
    0x1C: ("OBD standard", lambda d: OBD_STANDARDS.get(d[0], f"unknown (0x{d[0]:02X})")),
    0x1F: ("Time since engine start", lambda d: f"{_u16(d)} s"),
    0x21: ("Distance with check-engine light on", lambda d: f"{_u16(d)} km"),
    0x2E: ("EVAP purge commanded", lambda d: f"{d[0] * 100 / 255:.1f}%"),
    0x2F: ("Fuel level", lambda d: f"{d[0] * 100 / 255:.0f}%"),
    0x30: ("Warm-ups since codes cleared", lambda d: str(d[0])),
    0x31: ("Distance since codes cleared", lambda d: f"{_u16(d)} km"),
    0x33: ("Barometric pressure", lambda d: f"{d[0]} kPa"),
    0x3C: ("Catalyst temperature (bank 1 sensor 1)", lambda d: f"{_u16(d) / 10 - 40:.0f} °C"),
    0x3E: ("Catalyst temperature (bank 1 sensor 2)", lambda d: f"{_u16(d) / 10 - 40:.0f} °C"),
    0x42: ("Control module voltage", lambda d: f"{_u16(d) / 1000:.2f} V"),
    0x43: ("Absolute load", lambda d: f"{_u16(d) * 100 / 255:.1f}%"),
    0x44: ("Commanded lambda", lambda d: f"{_u16(d) * 2 / 65536:.3f}"),
    0x45: ("Relative throttle position", lambda d: f"{d[0] * 100 / 255:.1f}%"),
    0x46: ("Ambient air temperature", lambda d: f"{d[0] - 40} °C"),
    0x47: ("Throttle position B", _pct),
    0x48: ("Throttle position C", _pct),
    0x49: ("Accelerator pedal position D", _pct),
    0x4A: ("Accelerator pedal position E", _pct),
    0x4B: ("Accelerator pedal position F", _pct),
    0x4C: ("Commanded throttle", _pct),
}
for _i, _s in enumerate(_SENSOR_NAMES):
    PIDS[0x14 + _i] = _o2_narrow(_s)
    PIDS[0x24 + _i] = _wideband_voltage(_s)
    PIDS[0x34 + _i] = _wideband_current(_s)


@dataclass
class FreezeFrame:
    code: str                          # code that stored the frame, "" if none
    values: list[tuple[str, str]]      # (name, value text)
    error: str = ""


def read_freeze_frame(request: Callable[[bytes, float], bytes | None]) -> FreezeFrame:
    """Read freeze frame 0 (mode 02): the snapshot stored when a code set."""
    def get(pid: int) -> bytes | None:
        reply = exchange(request, bytes([0x02, pid, 0x00]), timeout=1.0)
        if reply and len(reply) >= 3 and reply[0] == 0x42 and reply[1] == pid:
            return reply[3:]
        return None

    trigger = get(0x02)
    if trigger is None:
        return FreezeFrame("", [], "freeze frame not available from this ECU")
    if len(trigger) < 2 or (trigger[0] == 0 and trigger[1] == 0):
        return FreezeFrame("", [])
    code = decode_code(trigger[0], trigger[1])

    supported: list[int] = []
    for base in (0x00, 0x20, 0x40):
        if base and base not in supported:
            break
        d = get(base)
        if not d or len(d) < 4:
            break
        bits = int.from_bytes(d[:4], "big")
        supported += [base + 1 + i for i in range(32) if bits & (1 << (31 - i))]

    values = []
    for pid in supported:
        if pid in (0x02, 0x20, 0x40, 0x60):
            continue
        d = get(pid)
        if d is None:
            continue
        name, fn = PIDS.get(pid, (f"PID 0x{pid:02X}", lambda d: d.hex(" ")))
        try:
            values.append((name, fn(d)))
        except IndexError:
            values.append((name, d.hex(" ")))
    return FreezeFrame(code, values)


def format_freeze_frame(ff: FreezeFrame) -> str:
    if ff.error:
        return f"Freeze frame: ({ff.error})"
    if not ff.code:
        return "Freeze frame: none stored"
    lines = [f"Freeze frame (stored when {ff.code} {describe(ff.code)} set):".replace("  ", " ")]
    lines += [f"  {n:40s} {v}" for n, v in ff.values]
    return "\n".join(lines)


CLEAR_WARNING = (
    "Read and save the freeze frame first - it records the conditions when the "
    "code set and cannot be recovered after clearing.\n\n"
    "Clearing codes also erases the freeze-frame data and resets every readiness "
    "monitor, and the ECU may reset its learned fuel trims. The car will not pass "
    "an emissions inspection until the monitors complete again. Codes for a fault "
    "that is still present will come back.\n\n"
    "Do this with the ignition ON and the engine OFF.")


def main() -> int:
    import argparse

    from openport import OpenPort, OpenPortError

    ap = argparse.ArgumentParser(description="Read (or clear) OBD-II fault codes.")
    ap.add_argument("--clear", action="store_true", help="clear codes (asks for confirmation)")
    ap.add_argument("--port", help="serial device (default: auto-detect the OpenPort)")
    args = ap.parse_args()
    want_clear = args.clear
    try:
        with OpenPort(args.port) as op:
            op.open()
            op.connect_can()

            def request(payload: bytes, timeout: float):
                if payload:
                    return op.request(payload, timeout)
                return op.recv(timeout)

            print(format_results(read_all(request)))
            print("\n" + format_freeze_frame(read_freeze_frame(request)))
            print("\n" + format_status(read_status(request)))
            print("\n" + format_test_results(read_test_results(request)))
            if want_clear:
                print("\n" + CLEAR_WARNING)
                if input("\nType 'clear' to clear the codes: ").strip().lower() != "clear":
                    print("Not cleared.")
                    return 0
                err = clear(request)
                print("Codes cleared." if not err else f"Clear failed: {err}")
                if not err:
                    print("\nAfter clearing:\n" + format_results(read_all(request)))
    except (OpenPortError, OSError) as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
