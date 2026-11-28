"""Copy recorded drives off the ESP32 over its COM port and convert them to logger CSVs.

    .venv/bin/python esp32/fetch_drives.py --port /dev/cu.wchusbserial…  [--delete] [--status]

Each drive is saved to logs/esp32/drive_NNNN.raw (checked against the board's CRC-32), then
converted to logs/ssm_….csv with ssmraw.py. --delete removes drives from the board once they are
safely copied. Doesn't reset the board, so it can run while the logger is recording.
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
import zlib
from pathlib import Path

import serial

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ssmraw  # noqa: E402

BAUD = 921600


class Board:
    def __init__(self, port: str):
        self.ser = serial.Serial()
        self.ser.port, self.ser.baudrate, self.ser.timeout = port, BAUD, 0.5
        self.ser.dtr = self.ser.rts = False          # don't reset the board when opening
        self.ser.open()
        time.sleep(0.1)
        self.ser.reset_input_buffer()

    def lines(self, timeout: float):
        """Yield lines from the board until timeout seconds pass with nothing new."""
        last = time.monotonic()
        buf = b""
        while time.monotonic() - last < timeout:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if not chunk:
                continue
            last = time.monotonic()
            buf += chunk
            *done, buf = buf.split(b"\n")
            for ln in done:
                yield ln.rstrip(b"\r").decode("latin-1")

    def command(self, text: str) -> None:
        self.ser.write(b"\n" + text.encode() + b"\n")

    def ask(self, text: str, prefix: str) -> str:
        self.command(text)
        for ln in self.lines(3):
            if ln.startswith(prefix) or ln.startswith("ERR"):
                return ln
        raise TimeoutError(f"no reply to '{text}' - is this the board's COM port, and stage 2 flashed?")

    def ls(self) -> list[tuple[str, int]]:
        self.command("ls")
        files = []
        for ln in self.lines(3):
            if ln.startswith("FILE "):
                _, name, size = ln.split()
                files.append((name, int(size)))
            elif ln == "OK":
                return files
        raise TimeoutError("no reply to 'ls' - is this the board's COM port, and stage 2 flashed?")

    def get(self, name: str) -> bytes:
        self.command(f"get {name}")
        size = crc = None
        parts = []
        for ln in self.lines(5):
            if ln.startswith("ERR"):
                raise OSError(ln)
            if ln.startswith(f"BEGIN {name} "):
                _, _, size, crc = ln.split()
                size, crc = int(size), int(crc, 16)
            elif ln.startswith("DATA ") and size is not None:
                parts.append(base64.b64decode(ln[5:]))
            elif ln == f"END {name}":
                data = b"".join(parts)
                if size is None or len(data) != size or zlib.crc32(data) != crc:
                    raise OSError(f"{name}: damaged in transfer ({len(data)} of {size} bytes) - try again")
                return data
        raise TimeoutError(f"{name}: transfer stopped")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", required=True, help="the board's COM port, e.g. /dev/cu.wchusbserial…")
    ap.add_argument("--delete", action="store_true", help="delete drives from the board once copied")
    ap.add_argument("--status", action="store_true", help="just show what the board is doing")
    args = ap.parse_args()
    board = Board(args.port)
    print(board.ask("status", "STATUS ")[7:])
    total, used = (int(x) for x in board.ask("df", "DF ").split()[1:])
    print(f"storage: {used / 1e6:.2f} MB used of {total / 1e6:.2f} MB")
    files = board.ls()
    if args.status:
        for name, size in files:
            print(f"  {name}  {size / 1e6:.2f} MB")
        return 0
    if not files:
        print("no drives on the board")
        return 0
    raw_dir = ROOT / "logs" / "esp32"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for name, size in files:
        t = time.monotonic()
        data = board.get(name)
        (raw_dir / name).write_bytes(data)
        drive = ssmraw.parse(data)
        csv = ssmraw.to_csv(drive, ROOT / "logs", name[:-4])
        span = drive.samples[-1][0] if drive.samples else 0
        print(f"{name}: {size / 1e6:.2f} MB in {time.monotonic() - t:.0f} s -> {csv.relative_to(ROOT)} "
              f"({len(drive.samples)} samples, {span / 60:.1f} min)")
        if args.delete:
            reply = board.ask(f"rm {name}", "OK")
            if reply != "OK":
                print(f"  could not delete it from the board: {reply}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
