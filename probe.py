"""Proof of concept: talk to the OpenPort 2.0 and, if a car is connected, the ECU.

Usage:  .venv/bin/python probe.py [--debug]
"""

import sys

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
        op = OpenPort(debug=debug)
    except (OpenPortError, OSError) as e:
        print(f"Could not open the OpenPort: {e}")
        return 1

    try:
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


if __name__ == "__main__":
    sys.exit(main())
