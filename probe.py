"""Proof of concept: talk to the OpenPort 2.0 and, if a car is connected, the ECU.

Usage:  .venv/bin/python probe.py [--debug] [--port DEVICE]
        .venv/bin/python probe.py --listen [SECONDS]     # listen-only CAN check (default 10 s)

--listen opens a raw CAN channel at the OBD port (500 kbit/s) with a pass-all filter and records
whatever the car's modules are broadcasting. It never transmits anything: no requests, nothing
to the ECU (the adapter only acknowledges frames, as every CAN node on the bus does). It prints
each identifier with how often it appears and which bytes change, and saves every frame to
logs/can_YYYYMMDD_HHMMSS.csv for decoding later.
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


def save_frames(frames) -> Path:
    logs = Path(__file__).resolve().parent / "logs"
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
