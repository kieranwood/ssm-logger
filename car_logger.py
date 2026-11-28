"""Headless drive logger: lives in the car on a Raspberry Pi with the OpenPort plugged in.

Records every drive to logs/ssm_YYYYMMDD_HHMMSS.csv (the same format as logger.py's recordings,
so they open in the desktop app and the website) and, once the car is home and the server can be
reached over Wi-Fi, uploads them to log_server.py on your network. It can power the Pi off when
there is nothing left to do. See docs/car-logger.md for the hardware and setup.

    python3 car_logger.py                       # uses car_logger.toml next to this file
    python3 car_logger.py --config FILE.toml --port /dev/ttyACM0
    python3 car_logger.py --demo                # simulated drives, no car (never powers off)
    python3 car_logger.py --upload-now          # upload what's waiting, then exit

A drive starts when the ECU answers (ignition on) and ends when it has been silent for
`end_after_s` (ignition off). A recording in which the engine never ran for `min_engine_s` -
the ignition switched on and off again, say - is thrown away. While a drive is being written the
file is called ….csv.part; it is renamed to .csv when the drive ends (or, after a power cut, at
the next start), and only .csv files are uploaded. Uploaded files move to logs/uploaded/.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, fields
from pathlib import Path

import defs
from openport import OpenPortError
from recording import CsvRecorder, build_channels
from sources import DEMO_INIT, DemoSource, EcuSource

HERE = Path(__file__).resolve().parent
log = logging.getLogger("car_logger")

ENGINE_RUNNING_RPM = 300
READ_FAILURES_BEFORE_LOST = 5          # failed reads in a row before the ECU counts as gone


@dataclass
class Config:
    # [server]
    url: str = ""                      # log_server.py, e.g. "http://192.168.1.20:8765"
    token: str = ""                    # the same token log_server.py was started with
    # [logging]
    dir: str = "logs"                  # relative to this file
    end_after_s: float = 15            # ECU silent this long: the drive is over
    min_engine_s: float = 10           # keep a recording only if the engine ran this long
    keep_uploaded_days: float = 30     # local copies of uploaded drives are deleted after this
    min_free_mb: float = 500           # ...or sooner, oldest first, to keep this much space free
    # [power]
    shutdown: bool = False             # power the Pi off when idle (see docs/car-logger.md)
    shutdown_cmd: list[str] = field(default_factory=lambda: ["sudo", "-n", "systemctl", "poweroff"])
    linger_s: float = 120              # after the last drive with nothing left to upload
    give_up_s: float = 900             # after the last drive with uploads still waiting (not home)
    low_battery_v: float = 11.8        # ignition off and the OBD supply below this: power off now
    # [ecu]
    port: str = ""                     # OpenPort device; empty: find it by its USB ID

    @classmethod
    def load(cls, path: Path) -> "Config":
        cfg = cls()
        if not path.exists():
            log.warning("No config at %s - using defaults (nothing will be uploaded)", path)
            return cfg
        import tomllib                  # Python 3.11+
        with open(path, "rb") as f:
            data = tomllib.load(f)
        known = {f.name for f in fields(cls)}
        for section, values in data.items():
            if not isinstance(values, dict):
                raise SystemExit(f"{path}: '{section}' should be a [section]")
            for k, v in values.items():
                if k not in known:
                    raise SystemExit(f"{path}: unknown setting [{section}] {k}")
                setattr(cfg, k, v)
        cfg.url = cfg.url.rstrip("/")
        return cfg

    @property
    def log_dir(self) -> Path:
        return (HERE / self.dir).resolve()


# ------------------------------------------------------------------ talking to the car

class EcuLink:
    """One ECU connection at a time, on the caller's thread (EcuSource's connect and read)."""

    def __init__(self, port: str | None, defs_path=None):
        self.src = EcuSource(port, defs_path)
        self.op = None
        self.volts: float | None = None       # OBD supply at the last attempt (None: no OpenPort)
        self.error = ""

    @property
    def params(self) -> list[defs.Parameter]:
        return self.src.params

    @property
    def ecu_id(self) -> str:
        return self.src.ecu_id

    def connect(self) -> bool:
        """True if the ECU answered (the ignition is on)."""
        self.close()
        try:
            self.op = self.src._connect()
            self.error = ""
            return True
        except (OpenPortError, OSError) as e:
            self.error = str(e)
            return False
        finally:
            self.volts = self.src.obd_volts

    def read(self) -> list[float] | None:
        if self.op is None:
            return None
        try:
            data = self.src._read(self.op)
        except (OpenPortError, OSError) as e:
            self.error = str(e)
            self.close()
            return None
        return None if data is None else defs.decode(self.src.params, self.src.addresses, data)

    def close(self) -> None:
        if self.op is not None:
            try:
                self.op.close()
            except (OpenPortError, OSError):
                pass
            self.op = None


class DemoLink:
    """Simulated car: the ignition is on for `on_s`, then off for `off_s`, over and over."""

    def __init__(self, defs_path=None, on_s: float = 60, off_s: float = 30):
        self.params = [p for p in defs.load(defs_path) if p.supported_by(DEMO_INIT)]
        self.ecu_id = "DEMO " + DEMO_INIT[3:8].hex().upper()
        self.on_s, self.off_s = on_s, off_s
        self.t0 = time.monotonic()
        self.connected = False
        self.volts: float | None = None
        self.error = ""

    def _on(self) -> bool:
        return (time.monotonic() - self.t0) % (self.on_s + self.off_s) < self.on_s

    def connect(self) -> bool:
        time.sleep(0.5)
        self.connected = self._on()
        self.volts = 14.2 if self.connected else 12.6
        self.error = "" if self.connected else "No reply from the ECU - is the ignition on?"
        return self.connected

    def read(self) -> list[float] | None:
        time.sleep(1 / 20)
        if not (self.connected and self._on()):
            self.connected = False
            return None
        v = DemoSource._values((time.monotonic() - self.t0) % (self.on_s + self.off_s))
        return [v.get(p.name, 0.0) for p in self.params]

    def close(self) -> None:
        self.connected = False


# ------------------------------------------------------------------ recording a drive

def new_drive_path(log_dir: Path) -> Path:
    stem = time.strftime("ssm_%Y%m%d_%H%M%S")
    n = 1
    while True:
        name = stem if n == 1 else f"{stem}_{n}"
        if not any((log_dir / f"{name}{ext}").exists() for ext in (".csv", ".csv.part")) and \
                not (log_dir / "uploaded" / f"{name}.csv").exists():
            return log_dir / f"{name}.csv.part"
        n += 1


def engine_seconds(path: Path) -> float | None:
    """How long the engine ran in a recording (None: it has no Engine Speed column)."""
    import csv
    with open(path, newline="", errors="replace") as f:
        rows = csv.reader(ln for ln in f if not ln.startswith("#"))
        header = next(rows, [])
        col = next((i for i, h in enumerate(header) if h.startswith("Engine Speed")), None)
        if col is None:
            return None
        total, last = 0.0, None
        for r in rows:
            try:
                t, rpm = float(r[0]), float(r[col])
            except (ValueError, IndexError):
                continue                         # e.g. a row cut short by a power cut
            if last is not None and rpm > ENGINE_RUNNING_RPM and t - last < 2:
                total += t - last
            last = t
        return total


def finish_drive(part: Path, cfg: Config) -> Path | None:
    """Rename a finished ….csv.part to .csv, or delete it if the engine never ran long enough."""
    try:
        ran = engine_seconds(part)
    except OSError as e:
        log.error("Could not read %s: %s", part.name, e)
        return None
    if ran is not None and ran < cfg.min_engine_s:
        log.info("Discarding %s: the engine ran for %.0f s", part.name, ran)
        part.unlink(missing_ok=True)
        return None
    final = part.with_suffix("")                 # ssm_….csv.part -> ssm_….csv
    part.rename(final)
    size = final.stat().st_size
    log.info("Saved %s (%.1f MB%s)", final.name, size / 1e6,
             "" if ran is None else f", engine running {ran / 60:.1f} min")
    return final


def record_drive(link, cfg: Config, stop: threading.Event) -> None:
    """Record from the connected ECU until it has been silent for end_after_s."""
    log_dir = cfg.log_dir
    channels = build_channels(link.params)
    names = [p.name for p in link.params]
    part = new_drive_path(log_dir)
    rec = CsvRecorder(channels, link.ecu_id, log_dir, path=part)
    log.info("Ignition on (ECU %s, %.1f V): recording %s", link.ecu_id, link.volts or math.nan, part.name)
    failures, rows_at_rate, t_rate = 0, 0, time.monotonic()
    t_flush = t_sync = time.monotonic()
    try:
        while not stop.is_set():
            values = link.read()
            now = time.monotonic()
            if values is None:
                failures += 1
                if failures < READ_FAILURES_BEFORE_LOST and link_is_open(link):
                    continue
                # The ECU stopped answering: the ignition went off, or a dropout. Give it
                # end_after_s to come back before calling the drive over.
                link.close()
                log.info("ECU stopped answering (%s) - waiting %.0f s", link.error or "no reply", cfg.end_after_s)
                lost = time.monotonic()
                while not stop.is_set() and time.monotonic() - lost < cfg.end_after_s:
                    if link.connect():
                        break
                    stop.wait(1.0)
                else:
                    return
                if stop.is_set():
                    return
                log.info("ECU back after %.0f s - carrying on with %s", time.monotonic() - lost, part.name)
                failures = 0
                continue
            failures = 0
            named = dict(zip(names, values))
            rec.write(now, [c.compute(named) for c in channels], None)
            if now - t_flush >= 1.0:              # at most a second lost if the power goes
                rec.flush()
                t_flush = now
            if now - t_sync >= 10.0:
                os.fsync(rec.f.fileno())
                t_sync = now
            if now - t_rate >= 60.0:
                log.info("Recording %s: %d rows, %.1f samples/s", part.name, rec.rows,
                         (rec.rows - rows_at_rate) / (now - t_rate))
                rows_at_rate, t_rate = rec.rows, now
    finally:
        rec.close()
        link.close()
        finish_drive(part, cfg)


def link_is_open(link) -> bool:
    return getattr(link, "op", None) is not None or getattr(link, "connected", False)


def recover_partial(cfg: Config) -> None:
    """Finish recordings left as ….csv.part by a power cut."""
    for part in sorted(cfg.log_dir.glob("ssm_*.csv.part")):
        log.info("Recovering %s from an earlier run", part.name)
        finish_drive(part, cfg)


# ------------------------------------------------------------------ uploading

class Uploader(threading.Thread):
    """Uploads finished drives whenever log_server.py can be reached."""

    def __init__(self, cfg: Config, stop: threading.Event, interval_s: float = 20):
        super().__init__(daemon=True, name="uploader")
        self.cfg, self.stop, self.interval_s = cfg, stop, interval_s
        self.done_dir = cfg.log_dir / "uploaded"
        self.busy = threading.Event()
        self.wake = threading.Event()
        self.last_error = ""

    def pending(self) -> list[Path]:
        return sorted(self.cfg.log_dir.glob("ssm_*.csv"))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.token}", "User-Agent": "ssm-car-logger"}

    def server_reachable(self) -> bool:
        req = urllib.request.Request(f"{self.cfg.url}/health", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=4) as r:
                return r.status == 200
        except urllib.error.HTTPError as e:
            self.last_error = f"server said {e.code} {e.reason}" + (" - check the token" if e.code == 401 else "")
        except (urllib.error.URLError, OSError) as e:
            self.last_error = f"server not reachable ({getattr(e, 'reason', e)})"
        return False

    def upload(self, path: Path) -> bool:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        body = gzip.compress(raw, compresslevel=6)
        req = urllib.request.Request(
            f"{self.cfg.url}/upload/{path.name}", data=body, method="PUT",
            headers={**self._headers(), "Content-Type": "text/csv", "Content-Encoding": "gzip",
                     "X-Content-SHA256": digest})
        t = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                reply = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            self.last_error = f"upload of {path.name} refused: {e.code} {e.read()[:200].decode(errors='replace')}"
            log.error("%s", self.last_error)
            return False
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.last_error = f"upload of {path.name} failed: {getattr(e, 'reason', e)}"
            log.warning("%s", self.last_error)
            return False
        if reply.get("sha256") != digest:
            self.last_error = f"upload of {path.name}: the server's copy doesn't match"
            log.error("%s", self.last_error)
            return False
        self.done_dir.mkdir(exist_ok=True)
        path.rename(self.done_dir / path.name)
        log.info("Uploaded %s as %s (%.1f MB in %.0f s)", path.name, reply.get("saved_as", path.name),
                 len(body) / 1e6, time.monotonic() - t)
        return True

    def upload_all(self) -> bool:
        """Upload every finished drive; True if none are left."""
        if not self.cfg.url:
            self.last_error = "no server url in the config"
            return not self.pending()
        files = self.pending()
        if not files:
            return True
        if not self.server_reachable():
            return False
        self.busy.set()
        try:
            for path in files:
                if self.stop.is_set() or not self.upload(path):
                    return False
            return True
        finally:
            self.busy.clear()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.upload_all()
                prune(self.cfg)
            except Exception:                      # keep uploading after anything unexpected
                log.exception("Uploader error")
            self.wake.wait(self.interval_s)
            self.wake.clear()


def prune(cfg: Config) -> None:
    """Delete old uploaded copies: after keep_uploaded_days, or sooner when space runs low."""
    done = sorted((cfg.log_dir / "uploaded").glob("*.csv"), key=lambda p: p.stat().st_mtime)
    cutoff = time.time() - cfg.keep_uploaded_days * 86400
    for p in done:
        if p.stat().st_mtime < cutoff or shutil.disk_usage(cfg.log_dir).free < cfg.min_free_mb * 1e6:
            log.info("Deleting the local copy of %s (uploaded)", p.name)
            p.unlink(missing_ok=True)


# ------------------------------------------------------------------ power

def power_off(cfg: Config, reason: str) -> None:
    log.info("Powering off: %s", reason)
    logging.shutdown()
    try:
        subprocess.run(cfg.shutdown_cmd, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        log.error("Power-off command %s failed: %s", cfg.shutdown_cmd, e)


def idle_verdict(cfg: Config, idle_s: float, pending: int, volts: float | None, uploading: bool) -> str | None:
    """Why the Pi should power off now (ignition off for idle_s), or None to stay on."""
    if volts is not None and volts < cfg.low_battery_v and idle_s >= 30:
        return f"car battery at {volts:.1f} V (below {cfg.low_battery_v} V)"
    if uploading:
        return None
    if not pending and idle_s >= cfg.linger_s:
        return "everything is uploaded"
    if idle_s >= cfg.give_up_s:
        return f"{pending} drive(s) still to upload but the server isn't reachable - next time"
    return None


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description="Headless drive logger for the car (see docs/car-logger.md)")
    ap.add_argument("--config", type=Path, default=HERE / "car_logger.toml")
    ap.add_argument("--port", help="OpenPort device (default: from the config, or found by USB ID)")
    ap.add_argument("--defs", help="path to RomRaider log_defs.xml")
    ap.add_argument("--demo", action="store_true", help="simulated drives; never powers off")
    ap.add_argument("--upload-now", action="store_true", help="upload waiting drives and exit")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    cfg = Config.load(args.config)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    uploader = Uploader(cfg, stop)

    if args.upload_now:
        recover_partial(cfg)
        ok = uploader.upload_all()
        if not ok:
            print(uploader.last_error or "Some uploads failed")
        return 0 if ok else 1

    def on_signal(signum, _frame):
        log.info("Stopping (signal %d)", signum)
        stop.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        link = DemoLink(args.defs) if args.demo else EcuLink(args.port or cfg.port or None, args.defs)
    except defs.DefinitionsMissing as e:
        print(e)
        return 1
    recover_partial(cfg)
    uploader.start()
    log.info("Waiting for the ignition (logs in %s, server %s, power-off %s)", cfg.log_dir,
             cfg.url or "not set", "on" if cfg.shutdown and not args.demo else "off")

    idle_since = time.monotonic()
    last_note = ""
    while not stop.is_set():
        if link.connect():
            record_drive(link, cfg, stop)
            idle_since = time.monotonic()
            uploader.wake.set()                   # try the server straight away
            continue
        note = link.error or "no reply"
        if note != last_note:
            log.info("Ignition off: %s%s", note, "" if link.volts is None else f" (OBD {link.volts:.1f} V)")
            last_note = note
        if cfg.shutdown and not args.demo:
            why = idle_verdict(cfg, time.monotonic() - idle_since, len(uploader.pending()),
                               link.volts, uploader.busy.is_set())
            if why:
                stop.set()
                power_off(cfg, why)
                break
        stop.wait(2.0)
    link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
