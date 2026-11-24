"""OBD-II diagnostic trouble codes over the same ISO15765 channel used for SSM.

    Mode 03 -> 43 n [code pairs]   stored (confirmed) codes - these light the CEL
    Mode 07 -> 47 n [code pairs]   pending codes - seen once, not yet confirmed
    Mode 0A -> 4A n [code pairs]   permanent codes - cleared only by the ECU itself
    Mode 04 -> 44                  clear codes, freeze frame and readiness monitors

Command line use (when the logger is not running):
    .venv/bin/python dtc.py            read codes
    .venv/bin/python dtc.py --clear    clear codes (asks for confirmation)
"""

from __future__ import annotations

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


def read_all(request: Callable[[bytes, float], bytes | None]) -> list[DtcResult]:
    results = []
    for mode, title in MODES:
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


_SENSOR_NAMES = ["bank 1 sensor 1 (front)", "bank 1 sensor 2 (rear)", "bank 1 sensor 3", "bank 1 sensor 4",
                 "bank 2 sensor 1", "bank 2 sensor 2", "bank 2 sensor 3", "bank 2 sensor 4"]

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
    from openport import OpenPort, OpenPortError

    want_clear = "--clear" in sys.argv
    try:
        with OpenPort() as op:
            op.open()
            op.connect_can()

            def request(payload: bytes, timeout: float):
                if payload:
                    return op.request(payload, timeout)
                return op.recv(timeout)

            print(format_results(read_all(request)))
            print("\n" + format_freeze_frame(read_freeze_frame(request)))
            print("\n" + format_status(read_status(request)))
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
