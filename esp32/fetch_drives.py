"""Copy recorded drives off the ESP32 over its COM port and convert them to logger CSVs, or set it up.

    .venv/bin/python esp32/fetch_drives.py --port /dev/cu.wchusbserial…  [--delete] [--status]
    .venv/bin/python esp32/fetch_drives.py --port … --configure   # Wi-Fi and server, for uploading
    .venv/bin/python esp32/fetch_drives.py --port … --set sleep=on [--set wake_v=13.2]

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
        time.sleep(1.5)        # opening the port resets the board: let it boot before talking to it
        self.ser.reset_input_buffer()
        self.buf = b""         # kept between reads, so a line after the one asked for isn't lost

    def lines(self, timeout: float):
        """Yield lines from the board until timeout seconds pass with nothing new."""
        last = time.monotonic()
        while True:
            while b"\n" in self.buf:
                ln, self.buf = self.buf.split(b"\n", 1)
                yield ln.rstrip(b"\r").decode("latin-1")
            if time.monotonic() - last >= timeout:
                return
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if chunk:
                last = time.monotonic()
                self.buf += chunk

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
        """Copy one drive. A damaged or out-of-place line (another message cutting in) stops the
        transfer, which then resumes from the last good byte; the whole file is checked at the end."""
        data = bytearray()
        size = crc = None
        for attempt in range(30):
            self.command(f"get {name} {len(data)}")
            damaged = False
            for ln in self.lines(5):
                if ln.startswith("ERR"):
                    raise OSError(f"{name}: {ln}")
                if ln.startswith(f"BEGIN {name} "):
                    _, _, size, crc = ln.split()
                    size, crc = int(size), int(crc, 16)
                elif ln.startswith("DATA ") and size is not None:
                    try:
                        _, off, b64 = ln.split(" ", 2)
                        chunk = base64.b64decode(b64, validate=True)
                        off = int(off)
                    except ValueError:
                        damaged = True
                    else:
                        whole = len(chunk) == 57 or off + len(chunk) == size
                        if off == len(data) and whole:
                            data += chunk
                        elif off > len(data) or not whole:
                            damaged = True
                    if damaged:
                        self.ser.write(b"\n")        # stop it, then resume from len(data)
                        for ln2 in self.lines(2):
                            if ln2.startswith(("ABORT", "END")):
                                break
                        self.ser.reset_input_buffer()
                        self.buf = b""
                        break
                elif ln == f"END {name}":
                    break
            if size is not None and len(data) == size:
                if zlib.crc32(data) != crc:
                    raise OSError(f"{name}: the copy doesn't match the board's checksum - try again")
                if attempt:
                    print(f"  ({attempt} damaged line(s) re-sent)")
                return bytes(data)
            if not damaged:
                raise TimeoutError(f"{name}: transfer stopped at {len(data)} of {size} bytes")
        raise OSError(f"{name}: too many damaged lines - try again")


def configure(board: Board) -> int:
    """Ask for the settings and store them on the board (in its NVS, not in the firmware)."""
    import getpass
    print("Current:", board.ask("config", "CONFIG ")[7:])
    print("Press Enter to keep a setting as it is.")
    values = {
        "ssid": input("Home Wi-Fi name: ").strip(),
        "pass": getpass.getpass("Wi-Fi password (not shown): "),
        "url": input("log_server.py address, e.g. http://192.168.1.20:8765: ").strip(),
        "token": getpass.getpass("log_server.py token (not shown): ").strip(),
    }
    for key, value in values.items():
        if value:
            reply = board.ask(f"set {key} {value}", "OK")
            if reply != "OK":
                print(f"{key}: {reply}")
                return 1
    print("Now:", board.ask("config", "CONFIG ")[7:])
    print("It will try uploading within 30 s; --status shows how it went.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", required=True, help="the board's COM port, e.g. /dev/cu.wchusbserial…")
    ap.add_argument("--delete", action="store_true", help="delete drives from the board once copied")
    ap.add_argument("--status", action="store_true", help="just show what the board is doing")
    ap.add_argument("--configure", action="store_true",
                    help="set the home Wi-Fi and log_server.py address and token (asks for each)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="change a setting: sleep=on|off, wake_v=13.2 (also ssid, url...)")
    args = ap.parse_args()
    board = Board(args.port)
    if args.configure:
        return configure(board)
    if args.set:
        for item in args.set:
            key, _, value = item.partition("=")
            reply = board.ask(f"set {key.strip()} {value.strip()}", "OK")
            print(f"{key}: {reply}")
            if reply != "OK":
                return 1
        board.command("status")
        for ln in board.lines(1):
            if ln.startswith("POWER "):
                print("power:", ln[6:])
        return 0
    print(board.ask("status", "STATUS ")[7:])
    for ln in board.lines(0.5):
        if ln.startswith("UPLOAD "):
            print("upload:", ln[7:])
        elif ln.startswith("POWER "):
            print("power:", ln[6:])
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
