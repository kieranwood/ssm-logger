"""Proof of concept: talk to the OpenPort 2.0 and, if a car is connected, the ECU.

Usage:  .venv/bin/python probe.py [--debug] [--port DEVICE]
        .venv/bin/python probe.py --listen [SECONDS]     # listen-only CAN check (default 10 s)
        .venv/bin/python probe.py --listen [SECONDS] --with-ssm   # ...while reading known values from the ECU

--listen opens a raw CAN channel at the OBD port (500 kbit/s) with a pass-all filter and records
whatever the car's modules are broadcasting. It never transmits anything: no requests, nothing
to the ECU (the adapter only acknowledges frames, as every CAN node on the bus does). It prints
each identifier with how often it appears and which bytes change, and saves every frame to
logs/can_YYYYMMDD_HHMMSS.csv for decoding later.

--with-ssm also reads a set of known values from the ECU over SSM at the same time (engine speed,
throttle, load, brake switch... - read requests only, as the logger makes) and saves them to
logs/can_..._ssm.csv on the same clock. It then matches each one against every CAN byte, 16-bit
pair and bit and prints the best matches with a scaling. The adapter keeps its usual ECU channel
open beside the raw one if it can; if not, the reads go out as single CAN frames on the raw channel.
"""

import csv
import sys
import time
from collections import defaultdict
from pathlib import Path

from openport import OpenPort, OpenPortError

SSM_ECU_INIT = bytes([0xAA])
SSM_READ_ADDRESSES = 0xA8

# A few standard SSM parameters: (name, address, length, conversion)
PARAMS = [
    ("Battery voltage (V)", 0x1C, 1, lambda v: v * 8 / 100),
    ("Coolant temp (C)", 0x08, 1, lambda v: v - 40),
    ("Engine speed (rpm)", 0x0E, 2, lambda v: v / 4),
    ("A/F correction #1 (%)", 0x09, 1, lambda v: (v - 128) * 100 / 128),
    ("A/F learning #1 (%)", 0x0A, 1, lambda v: (v - 128) * 100 / 128),
]


def main() -> int:
    debug = "--debug" in sys.argv
    try:
        port = sys.argv[sys.argv.index("--port") + 1] if "--port" in sys.argv else None
        op = OpenPort(port, debug=debug)
    except (OpenPortError, OSError) as e:
        print(f"Could not open the OpenPort: {e}")
        return 1

    try:
        if "--listen" in sys.argv:
            k = sys.argv.index("--listen")
            secs = float(sys.argv[k + 1]) if k + 1 < len(sys.argv) and sys.argv[k + 1][:1].isdigit() else 10.0
            if "--with-ssm" in sys.argv:
                return listen_with_ssm(op, secs)
            return listen(op, secs)
        return run(op)
    except OpenPortError as e:
        print(f"\nStopped: {e}")
        return 2


def run(op: OpenPort) -> int:
    with op:
        print(f"Port:      {op.port_name}")
        print(f"Firmware:  {op.open()}")
        volts = op.battery_voltage()
        print(f"OBD pin 16 voltage: {volts:.2f} V")
        if volts < 8:
            print("Warning: no vehicle power detected on OBD pin 16. Trying the ECU anyway.")

        op.connect_can()
        print("CAN channel open (500 kbps, ECU 0x7E0/0x7E8)")

        resp = op.request(SSM_ECU_INIT, timeout=2.0)
        if not resp:
            print("No reply to the SSM ECU init. Is the ignition on?")
            return 2
        print(f"Init reply ({len(resp)} bytes): {resp.hex(' ')}")
        if resp[0] != 0xEA:
            print(f"Unexpected reply type 0x{resp[0]:02X}")
            return 2
        init = resp[1:]
        print(f"ECU ID:    {init[3:8].hex().upper()}")

        req = bytearray([SSM_READ_ADDRESSES, 0x00])
        for _, addr, length, _ in PARAMS:
            for k in range(length):
                req += (addr + k).to_bytes(3, "big")
        resp = op.request(bytes(req), timeout=1.0)
        if not resp or resp[0] != 0xE8:
            print(f"Unexpected read reply: {resp.hex(' ') if resp else None}")
            return 2
        values = resp[1:]
        i = 0
        print()
        for name, _, length, conv in PARAMS:
            raw = int.from_bytes(values[i:i + length], "big")
            i += length
            print(f"  {name:24s} {conv(raw):8.2f}   (raw 0x{raw:0{length * 2}X})")
    return 0


def listen(op: OpenPort, seconds: float) -> int:
    """Listen-only: record the CAN traffic at the OBD port and summarise it per identifier."""
    with op:
        print(f"Port:      {op.port_name}")
        print(f"Firmware:  {op.open()}")
        volts = op.battery_voltage()
        print(f"OBD pin 16 voltage: {volts:.2f} V")
        if volts < 8:
            print("Warning: no vehicle power detected on OBD pin 16. Turn the ignition on.")
        op.connect_raw_can()
        print(f"Listening on CAN (500 kbps, all identifiers, nothing transmitted) for {seconds:g} s...")
        frames = op.listen(seconds)
    if not frames:
        print("\nNothing heard. Either the ignition is off, or this OBD port only carries "
              "diagnostic traffic (the modules' broadcast messages stay behind a gateway).")
        return 2
    save_frames(frames)
    summarise(frames, seconds)
    return 0


# Known values to read alongside the CAN capture (those the ECU supports).
MATCH_PARAMS = ["Engine Speed", "Vehicle Speed", "Throttle Opening Angle", "Accelerator Opening Angle", "Engine Load",
                "Mass Air Flow", "Manifold Absolute Pressure", "Coolant Temperature", "Ignition Timing",
                "Intake Air Temperature", "Battery Voltage", "Fuel Injector #1 Pulse Width",
                "Brake Switch", "Neutral Position Switch Signal", "Gear Position"]
DIAG_IDS = range(0x7DF, 0x7F0)


def listen_with_ssm(op: OpenPort, seconds: float) -> int:
    import defs
    with op:
        print(f"Port:      {op.port_name}")
        print(f"Firmware:  {op.open()}")
        print(f"OBD pin 16 voltage: {op.battery_voltage():.2f} V")
        op.connect_can()
        resp = op.request(SSM_ECU_INIT, timeout=2.0)
        if not resp or resp[0] != 0xEA:
            print("No reply to the SSM ECU init. Is the ignition on?")
            return 2
        init = resp[1:]
        print(f"ECU ID:    {init[3:8].hex().upper()}")
        by_name = {p.name: p for p in defs.load()}
        params = [by_name[n] for n in MATCH_PARAMS if n in by_name and by_name[n].supported_by(init)]
        addresses = defs.request_addresses(params)
        try:
            op.connect_raw_can()
            mode = "two channels"
            reqs = [(addresses[i:i + 30], bytes([SSM_READ_ADDRESSES, 0]) + b"".join(a.to_bytes(3, "big") for a in addresses[i:i + 30]))
                    for i in range(0, len(addresses), 30)]

            def read():
                out = bytearray()
                for chunk, req in reqs:
                    r = op.request(req, timeout=0.5)
                    if not r or r[0] != 0xE8 or len(r) != len(chunk) + 1:
                        return None
                    out += r[1:]
                return bytes(out)
        except OpenPortError as e:
            print(f"The adapter won't open a raw CAN channel beside the ECU channel ({e});")
            print("sending the reads as single CAN frames on the raw channel instead.")
            op.close_iso15765()
            op.connect_raw_can()
            mode = "one channel"

            def read():
                out = bytearray()
                for a in addresses:
                    r = op.raw_request(bytes([SSM_READ_ADDRESSES, 0]) + a.to_bytes(3, "big"))
                    if not r or r[0] != 0xE8 or len(r) != 2:
                        return None
                    out += r[1:]
                return bytes(out)

        print(f"Listening on CAN and reading {len(params)} values over SSM ({mode}) for {seconds:g} s...")
        op.take_frames()
        samples = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            data = read()
            if data is not None:
                samples.append((time.monotonic(), defs.decode(params, addresses, data)))
        frames = op.take_frames()
    if not frames:
        print("\nNo CAN frames heard.")
        return 2
    print(f"{len(samples)} SSM samples ({len(samples) / seconds:.1f}/s), {len(frames)} CAN frames")
    path = save_frames(frames)
    t0 = frames[0][0]
    ssm_path = path.with_name(path.stem + "_ssm.csv")
    with open(ssm_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_s"] + [p.name for p in params])
        for t, vals in samples:
            w.writerow([f"{t - t0:.4f}"] + [f"{v:g}" for v in vals])
    print(f"Saved {len(samples)} SSM samples to {ssm_path}")
    summarise(frames, seconds)
    match_signals(frames, samples, params)
    return 0


def match_signals(frames, samples, params) -> None:
    """For each SSM value, the CAN byte / 16-bit pair / bit that tracks it best, with a linear scaling."""
    import bisect
    import numpy as np
    if len(samples) < 10:
        print("\nToo few SSM samples to match.")
        return
    ts = np.array([t for t, _ in samples])
    by_id: dict[int, tuple[list[float], list[bytes]]] = defaultdict(lambda: ([], []))
    for t, _, cid, data in frames:
        if cid in DIAG_IDS or len(data) < 1:
            continue
        by_id[cid][0].append(t)
        by_id[cid][1].append(data)
    # every candidate signal, sampled at the SSM times (latest frame at or before each)
    cands = []
    for cid, (tt, dd) in by_id.items():
        idx = [max(0, bisect.bisect_right(tt, t) - 1) for t in ts]
        rows = np.array([list(dd[i].ljust(8, b"\0")[:8]) for i in idx], dtype=float)
        for i in range(8):
            cands.append((f"{cid:03X} byte {i}", rows[:, i], False))
            for b in range(8):
                cands.append((f"{cid:03X} byte {i} bit {b}", (rows[:, i].astype(int) >> b) & 1, True))
        for i in range(7):
            cands.append((f"{cid:03X} bytes {i}-{i + 1} (big-endian)", rows[:, i] * 256 + rows[:, i + 1], False))
            cands.append((f"{cid:03X} bytes {i}-{i + 1} (little-endian)", rows[:, i + 1] * 256 + rows[:, i], False))
    print("\nBest CAN matches for each SSM value (r = correlation; value ≈ a × raw + b):")
    for k, p in enumerate(params):
        y = np.array([v[k] for _, v in samples], dtype=float)
        if np.ptp(y) == 0:
            print(f"  {p.name}: didn't change during the capture, so it can't be matched")
            continue
        found = []
        for name, x, is_bit in cands:
            if is_bit != p.switch or np.ptp(x) == 0:
                continue
            r = float(np.corrcoef(x, y)[0, 1])
            if np.isfinite(r) and abs(r) >= 0.8:
                a, b = np.polyfit(x, y, 1)
                found.append((abs(r), r, name, a, b))
        found.sort(reverse=True)
        if not found:
            print(f"  {p.name}: no CAN signal tracks it (|r| < 0.8)")
            continue
        print(f"  {p.name} ({p.units}):")
        for _, r, name, a, b in found[:3]:
            scale = "" if p.switch else f"   a={a:.4g} b={b:.4g}"
            print(f"      {name:34s} r={r:+.3f}{scale}")


def summarise(frames, seconds: float) -> None:
    by_id: dict[int, list[bytes]] = defaultdict(list)
    for _, _, cid, data in frames:
        by_id[cid].append(data)
    print(f"\n{len(frames)} frames from {len(by_id)} identifiers in {seconds:g} s\n")
    print(f"  {'ID':>8}  {'count':>6}  {'rate':>8}  len  {'last data':<23}  changing bytes")
    for cid in sorted(by_id):
        msgs = by_id[cid]
        last = msgs[-1]
        width = max(len(m) for m in msgs)
        changing = [i for i in range(width) if len({m[i] if i < len(m) else None for m in msgs}) > 1]
        ident = f"{cid:08X}" if cid > 0x7FF else f"{cid:03X}"
        lens = sorted({len(m) for m in msgs})
        ln = str(lens[0]) if len(lens) == 1 else f"{lens[0]}-{lens[-1]}"
        print(f"  {ident:>8}  {len(msgs):>6}  {len(msgs) / seconds:>6.1f}/s  {ln:>3}  {last.hex(' '):<23}  "
              + (" ".join(str(i) for i in changing) if changing else "-"))
    diag = [cid for cid in by_id if 0x7DF <= cid <= 0x7EF]
    if diag and len(diag) == len(by_id):
        print("\nOnly diagnostic identifiers (0x7DF-0x7EF) were seen: the broadcast traffic isn't visible here.")


LOGS_DIR = Path(__file__).resolve().parent / "logs"


def save_frames(frames) -> Path:
    logs = LOGS_DIR
    logs.mkdir(exist_ok=True)
    path = logs / time.strftime("can_%Y%m%d_%H%M%S.csv")
    t0 = frames[0][0]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_s", "device_us", "id", "len", "data"])
        for t, stamp, cid, data in frames:
            w.writerow([f"{t - t0:.4f}", stamp, f"{cid:X}", len(data), data.hex(" ")])
    print(f"Saved {len(frames)} frames to {path}")
    return path


if __name__ == "__main__":
    sys.exit(main())
