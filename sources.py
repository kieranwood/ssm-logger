"""Data sources: the real ECU (via the OpenPort) and a simulator for testing.

Each source runs in a background thread and appends (t, values) samples to a
shared list that the GUI drains. `values` follows the order of `params`.
"""

from __future__ import annotations

import math
import queue
import random
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable

import defs
from openport import OpenPort, OpenPortError

SSM_ECU_INIT = b"\xaa"
SSM_READ_ADDRESSES = 0xA8
SSM_READ_REPLY = 0xE8
SSM_NRC = 0x7F
MAX_ADDRESSES_PER_READ = 84   # measured on a 2012 Forester (ECU 7152345007); more -> NRC 0x13

# Init reply captured from the 2012 Forester, used by the simulator so it
# offers the same parameter list as the real car.
DEMO_INIT = bytes.fromhex(
    "a2100a7152345007f3fac98c0202fea800000046ce54fdb800000f2000000000"
    "00dc00005d1f3080f024100043fb00f5c98e0000000121f18000b1")


class Source(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.params: list[defs.Parameter] = []
        self.ecu_id = ""
        self.status = "Starting"
        self.rate_hz = 0.0
        self.ready = threading.Event()
        self._lock = threading.Lock()
        self._samples: list[tuple[float, list[float]]] = []
        self._stop = threading.Event()
        self._jobs: queue.Queue[tuple[Callable, Future]] = queue.Queue()
        self.t0 = time.monotonic()

    def submit(self, job: Callable[[Callable[[bytes, float], bytes | None]], Any]) -> Future:
        """Run job(request) on the polling thread between reads.

        request(payload, timeout) sends an ISO15765 message to the ECU and returns
        its reply; an empty payload just waits for the next reply.
        """
        fut: Future = Future()
        self._jobs.put((job, fut))
        return fut

    def _run_jobs(self, request: Callable[[bytes, float], bytes | None]) -> None:
        while True:
            try:
                job, fut = self._jobs.get_nowait()
            except queue.Empty:
                return
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(job(request))
            except Exception as e:      # report any failure back to the caller
                fut.set_exception(e)

    def drain(self) -> list[tuple[float, list[float]]]:
        with self._lock:
            out, self._samples = self._samples, []
        return out

    def _push(self, values: list[float]) -> None:
        with self._lock:
            self._samples.append((time.monotonic() - self.t0, values))

    def stop(self) -> None:
        self._stop.set()


class EcuSource(Source):
    def __init__(self, port: str | None = None, defs_path=defs.DEFAULT_DEFS):
        super().__init__()
        self.port = port
        self.all_params = defs.load(defs_path)

    def _connect(self) -> OpenPort:
        self.status = "Connecting to OpenPort"
        op = OpenPort(self.port)
        try:
            op.open()
            volts = op.battery_voltage()
            self.status = f"Connecting to ECU (OBD {volts:.1f} V)"
            op.connect_can()
            reply = op.request(SSM_ECU_INIT, timeout=2.0)
            if not reply or reply[0] != 0xEA:
                raise OpenPortError("No reply from the ECU - is the ignition on?")
        except Exception:
            op.close()
            raise
        init = reply[1:]
        self.ecu_id = init[3:8].hex().upper()
        params = [p for p in self.all_params if p.supported_by(init)]
        if not self.params:                  # first connection fixes the column set
            self.params = params
        self.addresses = defs.request_addresses(self.params)
        self.requests = []
        for i in range(0, len(self.addresses), MAX_ADDRESSES_PER_READ):
            chunk = self.addresses[i:i + MAX_ADDRESSES_PER_READ]
            self.requests.append((chunk, bytes([SSM_READ_ADDRESSES, 0]) +
                                  b"".join(a.to_bytes(3, "big") for a in chunk)))
        self.ready.set()
        return op

    def _read(self, op: OpenPort) -> bytes | None:
        data = bytearray()
        for chunk, req in self.requests:
            reply = op.request(req, timeout=0.5)
            if not reply or reply[0] != SSM_READ_REPLY or len(reply) != len(chunk) + 1:
                return None
            data += reply[1:]
        return bytes(data)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                op = self._connect()
            except (OpenPortError, OSError) as e:
                self.status = f"Not connected: {e}"
                self._stop.wait(2.0)
                continue
            self.status = "Logging"
            failures, count, t_rate = 0, 0, time.monotonic()

            def request(payload: bytes, timeout: float, op=op):
                return op.request(payload, timeout) if payload else op.recv(timeout)

            try:
                while not self._stop.is_set():
                    self._run_jobs(request)
                    data = self._read(op)
                    if data is None:
                        failures += 1
                        if failures >= 5:
                            raise OpenPortError("ECU stopped answering")
                        continue
                    failures = 0
                    self._push(defs.decode(self.params, self.addresses, data))
                    count += 1
                    now = time.monotonic()
                    if now - t_rate >= 1.0:
                        self.rate_hz, count, t_rate = count / (now - t_rate), 0, now
            except (OpenPortError, OSError) as e:
                self.status = f"Connection lost: {e} - retrying"
                self.rate_hz = 0.0
            finally:
                op.close()
            self._stop.wait(1.0)


class DemoSource(Source):
    """Simulated cold start that reproduces the +35% short-term trim spikes."""

    def __init__(self, defs_path=defs.DEFAULT_DEFS):
        super().__init__()
        self.params = [p for p in defs.load(defs_path) if p.supported_by(DEMO_INIT)]
        self.ecu_id = "DEMO " + DEMO_INIT[3:8].hex().upper()

    def run(self) -> None:
        self.ready.set()
        self.status = "Simulated data"
        names = [p.name for p in self.params]
        self._demo_codes = {0x03: [(0x01, 0x71)], 0x07: [(0x01, 0x71)], 0x0A: [(0x01, 0x71)]}
        while not self._stop.wait(1 / 30):
            self._run_jobs(self._demo_request)
            t = time.monotonic() - self.t0
            v = self._values(t)
            self._push([v.get(n, 0.0) for n in names])
            self.rate_hz = 30.0

    def _demo_request(self, payload: bytes, timeout: float) -> bytes | None:
        time.sleep(0.05)
        mode = payload[0] if payload else None
        if mode == 0x04:
            self._demo_codes = {0x03: [], 0x07: [], 0x0A: [(0x01, 0x71)]}
            return b"\x44"
        if mode == 0x02 and len(payload) == 3:
            if not self._demo_codes[0x03]:
                ff = {0x02: b"\x00\x00"}
            else:     # simulated P0171 after a hot restart
                ff = {0x02: b"\x01\x71", 0x03: b"\x02\x00", 0x04: b"\x6e", 0x05: b"\x80",
                      0x06: b"\xa0", 0x07: b"\x90", 0x0B: b"\x2d", 0x0C: b"\x22\x60",
                      0x0D: b"\x5f", 0x0F: b"\x46", 0x10: b"\x05\xdc", 0x11: b"\x30",
                      0x15: b"\xaa\xff", 0x1F: b"\x00\xb4",
                      0x34: b"\x80\x00\x80\x00"}
            pid = payload[1]
            if pid in (0x00, 0x20, 0x40):
                bits = 0
                for k in list(ff) + ([0x20, 0x40] if len(ff) > 1 else []):
                    if pid < k <= pid + 32:
                        bits |= 1 << (32 - (k - pid))
                return bytes([0x42, pid, 0]) + bits.to_bytes(4, "big")
            if pid in ff:
                return bytes([0x42, pid, 0]) + ff[pid]
            return None
        if mode == 0x01 and len(payload) == 2:
            pid = payload[1]
            demo = {0x01: b"\x00\x07\x65\x21", 0x03: b"\x02\x00", 0x21: b"\x00\x00",
                    0x30: b"\x03", 0x31: b"\x00\x2a", 0x41: b"\x00\x07\x65\x65",
                    0x4E: b"\x00\x5a"}
            if pid in (0x00, 0x20, 0x40):
                bits = 0
                for k in list(demo) + [0x20, 0x40]:
                    if pid < k <= pid + 32:
                        bits |= 1 << (32 - (k - pid))
                return bytes([0x41, pid]) + bits.to_bytes(4, "big")
            if pid in demo:
                return bytes([0x41, pid]) + demo[pid]
            return None
        if mode in self._demo_codes:
            codes = self._demo_codes[mode]
            return bytes([mode + 0x40, len(codes)]) + b"".join(bytes(c) for c in codes)
        return b"\x7f" + bytes([mode or 0, 0x11])

    @staticmethod
    def _values(t: float) -> dict[str, float]:
        noise = lambda s: random.gauss(0, s)
        coolant = min(90, 26 + t * 0.4)
        lean = 1.0 if (t % 40) > 25 else 0.0                  # periodic fault
        stft = max(-10, min(35, lean * min(35, ((t % 40) - 25) * 8) + noise(1.5)))
        rpm = 1500 - min(700, t * 15) - lean * 250 + noise(15)
        maf_v = 1.3 + noise(0.01) - lean * 0.15 * (random.random() < 0.3)
        return {
            "Engine Speed": rpm,
            "Coolant Temperature": coolant,
            "Air/Fuel Correction #1": stft,
            "Air/Fuel Learning #1": 5.47,
            "Air/Fuel Sensor #1": 1.0 + noise(0.01),
            "Air/Fuel Correction #3": noise(2),
            "Rear O2 Sensor": 0.65 + 0.1 * math.sin(t) + noise(0.02),
            "Air/Fuel Sensor #1 Current": noise(0.05),
            "Air/Fuel Sensor #1 Resistance": 30 + noise(1),
            "Air Flow Sensor Voltage": maf_v,
            "Mass Air Flow": 3.2 + noise(0.1),
            "Battery Voltage": 14.3 + noise(0.05),
            "Engine Load": 35 + lean * 15 + noise(1),
            "Ignition Timing": 15 - lean * 12 + noise(1),
            "CPC Valve Duty Ratio": 20.0 if t > 60 else 0.0,
            "Throttle Opening Angle": 11.4,
            "Intake Air Temperature": 32,
            "Manifold Absolute Pressure": 4.5 + noise(0.1),
            "Atmospheric Pressure": 14.65,
            "Fuel Injector #1 Pulse Width": 4.8 + lean,
            "Ignition Switch Signal": 1.0,
        }
