"""Convert the ESP32 logger's raw drive files (.raw) to the logger's CSV format.

    python3 ssmraw.py drive_0001.raw [...] [--out DIR]

A .raw file holds the ECU's init reply, the addresses read, and each sample's raw reply bytes with
its time (format: esp32/main/drive_log.h). The parameters are worked out again here from the init
reply with defs.py, exactly as the desktop logger does, and decoded with RomRaider's scalings - so
the CSV is the same as one the desktop logger would have recorded.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import defs
from recording import CsvRecorder, build_channels

MAGIC = b"SSMRAW1\n"


@dataclass
class RawDrive:
    init: bytes                         # ECU init reply after the 0xEA byte
    addresses: list[int]
    start_unix: int                     # 0 if the logger's clock wasn't set
    samples: list[tuple[float, bytes]]  # (seconds since the start, one byte per address)
    truncated: bool                     # the last record was cut short (power cut)

    @property
    def ecu_id(self) -> str:
        return self.init[3:8].hex().upper()


def parse(data: bytes) -> RawDrive:
    if not data.startswith(MAGIC):
        raise ValueError("not an SSM raw drive file")
    pos = len(MAGIC)
    try:
        init_len = data[pos]
        init = data[pos + 1:pos + 1 + init_len]
        pos += 1 + init_len
        (n,) = struct.unpack_from("<H", data, pos)
        addresses = list(struct.unpack_from(f"<{n}I", data, pos + 2))
        pos += 2 + 4 * n
        (start,) = struct.unpack_from("<I", data, pos)
        pos += 4
    except (IndexError, struct.error):
        raise ValueError("raw drive file header is cut short") from None
    if len(init) != init_len or n == 0:
        raise ValueError("raw drive file header is damaged")
    rec = 4 + n
    samples = []
    while pos + rec <= len(data):
        (t_ms,) = struct.unpack_from("<I", data, pos)
        samples.append((t_ms / 1000, data[pos + 4:pos + rec]))
        pos += rec
    return RawDrive(init, addresses, start, samples, truncated=pos != len(data))


def to_csv(drive: RawDrive, out_dir: Path, stem: str, defs_path=None, unique: bool = False) -> Path:
    """Write the drive as out_dir/ssm_<start time or stem>.csv; returns the path.
    unique: never overwrite an existing CSV (add _2, _3...) - otherwise it's replaced."""
    params = [p for p in defs.load(defs_path) if p.supported_by(drive.init)]
    have = set(drive.addresses)
    usable = [p for p in params if all(a in have for a in p.addresses())]
    if len(usable) != len(params):
        missing = [p.name for p in params if p not in usable]
        print(f"warning: {len(missing)} parameter(s) not in the recording: {', '.join(missing)}", file=sys.stderr)
    if not usable:
        raise ValueError("none of the ECU's parameters are in the recording")
    channels = build_channels(usable)
    names = [p.name for p in usable]
    name = (time.strftime("ssm_%Y%m%d_%H%M%S", time.localtime(drive.start_unix)) if drive.start_unix
            else f"ssm_{stem}")
    path = out_dir / f"{name}.csv"
    n = 2
    while unique and path.exists():
        path = out_dir / f"{name}_{n}.csv"
        n += 1
    rec = CsvRecorder(channels, drive.ecu_id, out_dir, path=path)
    try:
        for t, data in drive.samples:
            named = dict(zip(names, defs.decode(usable, drive.addresses, data)))
            rec.write(t, [c.compute(named) for c in channels], None)
    finally:
        rec.close()
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert ESP32 raw drive files to logger CSVs")
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, help="output folder (default: next to each file)")
    ap.add_argument("--defs", help="path to RomRaider log_defs.xml")
    args = ap.parse_args()
    for f in args.files:
        try:
            drive = parse(f.read_bytes())
            path = to_csv(drive, args.out or f.parent, f.stem, args.defs)
        except (OSError, ValueError) as e:
            print(f"{f}: {e}")
            return 1
        span = drive.samples[-1][0] if drive.samples else 0
        print(f"{f.name} -> {path} ({len(drive.samples)} samples over {span / 60:.1f} min, ECU {drive.ecu_id}"
              f"{', last sample cut short' if drive.truncated else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
