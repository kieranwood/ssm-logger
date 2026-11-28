"""Minimal driver for the Tactrix OpenPort 2.0 over its USB serial (CDC) port.

The command set mirrors the open-source J2534 library for the OpenPort 2.0
(github.com/dschultzca/j2534), which RomRaider uses on Linux:

    ati              -> "ari <firmware info>"     identify
    ata              -> "aro"                     open interface
    atr 16           -> "arr 16 <millivolts>"     read voltage on OBD pin 16
    ato<p> <flags> <baud> 0   -> "aro"            connect protocol channel
    ats<p> <param> <value>    -> "aro"            set channel config
    atf<p> <type> <txflags> <len><mask><pattern><flowctl> -> "arf..."  filter
    att<p> <len> <txflags>\\r\\n<data>             transmit (no reply)
    atc<p>           -> "aro"                     disconnect channel
    atz                                           close interface

Received bus traffic arrives as binary packets:
    'a' 'r' <channel> <len> <type> <timestamp:4 BE> <data: len-5 bytes>
"""

from __future__ import annotations

import glob
import time

import serial
from serial.tools import list_ports

ISO15765 = 6
CH_ISO15765 = 0x30 + ISO15765          # channel byte in packets: ASCII '6'
CAN = 5                                # raw CAN frames (listen-only use in probe.py)
CH_CAN = 0x30 + CAN                    # ASCII '5'
CAN_ID_BOTH = 0x800                    # J2534 connect flag: receive 11- and 29-bit identifiers
PASS_FILTER = 1

TX_ISO15765_FRAME_PAD = 0x40
FLOW_CONTROL_FILTER = 3

# J2534 SET_CONFIG parameter ids (same values RomRaider's J2534Impl uses)
CFG_LOOPBACK = 0x03
CFG_ISO15765_BS = 0x1E
CFG_ISO15765_STMIN = 0x1F
CFG_BS_TX = 0x22
CFG_STMIN_TX = 0x23
CFG_ISO15765_WFT_MAX = 0x25

# Packet types
NORM_MSG = 0x00
TX_DONE = 0x10
TX_LB_MSG = 0x20
RX_MSG_END_IND = 0x40
EXT_ADDR_MSG_END_IND = 0x44
LB_MSG_END_IND = 0x60
NORM_MSG_START_IND = 0x80
TX_LB_START_IND = 0xA0


J2534_ERRORS = {
    1: "not supported", 2: "invalid channel id", 3: "invalid protocol id",
    4: "null parameter", 5: "invalid ioctl value", 6: "invalid flags", 7: "failed",
    8: "device not connected",
    9: "timeout (nothing on the CAN bus acknowledged the message - is the car connected with ignition on?)",
    10: "invalid message", 11: "invalid time interval", 12: "exceeded limit",
    13: "invalid message id", 14: "device in use", 15: "invalid ioctl id",
    16: "buffer empty", 17: "buffer full", 18: "buffer overflow", 19: "pin invalid",
    20: "channel in use", 21: "message protocol id", 22: "invalid filter id",
    23: "no flow control", 24: "not unique", 25: "invalid baud rate", 26: "invalid device id",
}


class OpenPortError(Exception):
    pass


def _describe_error(line: str) -> str:
    try:
        code = int(line.split()[1])
    except (IndexError, ValueError):
        return line
    return f"J2534 error {code}: {J2534_ERRORS.get(code, 'unknown')}"


# Tactrix OpenPort 2.0 USB ID (an FTDI-assigned vendor/product pair).
OPENPORT_USB_IDS = {(0x0403, 0xCC4C), (0x0403, 0xCC4D)}   # cc4d: seen on firmware 1.17.4877
# Fallback device names when the USB ID isn't reported: macOS, Linux.
FALLBACK_PATTERNS = ("/dev/cu.usbmodem*", "/dev/ttyACM*")


def find_port() -> str:
    """Find the OpenPort's serial device on macOS, Linux or Windows."""
    ports = list(list_ports.comports())
    for p in ports:
        if (p.vid, p.pid) in OPENPORT_USB_IDS or "openport" in f"{p.product} {p.description}".lower():
            return p.device
    for pattern in FALLBACK_PATTERNS:
        if found := sorted(glob.glob(pattern)):
            return found[0]
    seen = ", ".join(p.device for p in ports) or "none"
    raise OpenPortError(f"No OpenPort found (serial ports seen: {seen}). Is it plugged in? "
                        "Use --port to name the device.")


class OpenPort:
    def __init__(self, port: str | None = None, debug: bool = False):
        self.port_name = port or find_port()
        self.debug = debug
        self.ser = serial.Serial(self.port_name, 115200, timeout=0.02)
        self.buf = bytearray()
        self.firmware = ""
        self._channel_open = False
        self._rx_parts = bytearray()
        self._rx_queue: list[tuple[int, bytes]] = []
        self._raw_open = False
        self._frames: list[tuple[float, int, int, bytes]] = []   # raw CAN: (host time, device us, id, data)

    # ------------------------------------------------------------------ low level
    def _log(self, direction: str, data: bytes) -> None:
        if self.debug:
            print(f"  {direction} {bytes(data)!r}")

    def _write(self, data: bytes) -> None:
        self._log(">>", data)
        self.ser.write(data)

    def _fill(self) -> bool:
        chunk = self.ser.read(self.ser.in_waiting or 1)
        if chunk:
            self._log("<<", chunk)
            self.buf += chunk
            return True
        return False

    def _next_item(self):
        """Pop one complete item from the buffer.

        Returns ("pkt", type, data, channel, device timestamp) for binary bus packets, ("line", text) for
        text replies, or None if no complete item is buffered yet.
        """
        buf = self.buf
        # discard anything before the next "ar"
        i = buf.find(b"ar")
        if i < 0:
            if len(buf) > 1:
                del buf[:-1]
            return None
        if i:
            del buf[:i]
        if len(buf) < 3:
            return None
        ch = buf[2]
        if ch in (CH_ISO15765, 0x35, 0x34, 0x33):   # binary packet on a channel
            if len(buf) < 4:
                return None
            ln = buf[3]
            total = ln + 4
            if len(buf) < total:
                return None
            # another packet header inside this one means bytes were lost and this length is wrong:
            # drop the broken packet and pick up at that header (otherwise headers get read as frames)
            for k in range(4, min(total, len(buf) - 3)):
                if buf[k] == 0x61 and buf[k + 1] == 0x72 and buf[k + 2] in (CH_CAN, CH_ISO15765) and buf[k + 3] <= 0x20:
                    del buf[:k]
                    self.resyncs = getattr(self, "resyncs", 0) + 1
                    return self._next_item()
            ptype = buf[4]
            stamp = int.from_bytes(buf[5:9], "big") if total >= 9 else 0
            data = bytes(buf[9:total])
            del buf[:total]
            return ("pkt", ptype, data, ch, stamp)
        end = buf.find(b"\r\n")
        if end < 0:
            return None
        line = bytes(buf[:end]).decode("latin-1")
        del buf[:end + 2]
        return ("line", line)

    def _handle_packet(self, ptype: int, data: bytes, ch: int = CH_ISO15765, stamp: int = 0) -> None:
        if ch == CH_CAN:
            # raw CAN: one complete frame per packet, 4-byte identifier then up to 8 data bytes
            if ptype == NORM_MSG and len(data) >= 4:
                self._frames.append((time.monotonic(), stamp, int.from_bytes(data[:4], "big"), data[4:]))
            return
        if ptype in (NORM_MSG, RX_MSG_END_IND, EXT_ADDR_MSG_END_IND):
            # Long ISO-TP messages arrive in several packets; each one repeats
            # the 4-byte CAN id, which must only appear once in the message.
            if self._rx_parts and data[:4] == self._rx_parts[:4]:
                data = data[4:]
            self._rx_parts += data
            if ptype != NORM_MSG:
                if self._rx_parts:
                    self._rx_queue.append((time.monotonic(), bytes(self._rx_parts)))
                self._rx_parts = bytearray()
        # TX done, loopback and start-of-message indications are ignored

    def command(self, text: str | bytes, expect: str = "aro", timeout: float = 2.0,
                payload: bytes = b"") -> str:
        """Send a text command and wait for a reply line starting with `expect`."""
        data = text.encode() if isinstance(text, str) else text
        self._write(data + payload)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            item = self._next_item()
            if item is None:
                self._fill()
                continue
            if item[0] == "pkt":
                self._handle_packet(*item[1:])
                continue
            line = item[1]
            if line.startswith("are"):
                first = data.split(b"\r\n")[0]              # (no backslash inside the f-string: Python < 3.12)
                raise OpenPortError(f"{_describe_error(line)} (command {first!r})")
            if line.startswith(expect):
                return line
        raise OpenPortError(f"Timeout waiting for {expect!r} after {data.strip()!r}")

    # ------------------------------------------------------------------ device
    def open(self) -> str:
        self.ser.reset_input_buffer()
        self.buf.clear()
        self.firmware = self.command("\r\n\r\nati\r\n", expect="ari")
        self.command("ata\r\n")
        return self.firmware

    def battery_voltage(self) -> float:
        line = self.command("atr 16\r\n", expect="arr")
        parts = line.split()
        return int(parts[2]) / 1000.0

    def connect_can(self, baud: int = 500000, tester_id: int = 0x7E0, ecu_id: int = 0x7E8) -> None:
        """Open an ISO15765 channel with a flow-control filter, as RomRaider does."""
        self.command(f"ato{ISO15765} 0 {baud} 0\r\n")
        self._channel_open = True
        for param, value in ((CFG_LOOPBACK, 0), (CFG_ISO15765_BS, 0), (CFG_ISO15765_STMIN, 0),
                             (CFG_BS_TX, 0xFFFF), (CFG_STMIN_TX, 0xFFFF), (CFG_ISO15765_WFT_MAX, 0)):
            self.command(f"ats{ISO15765} {param} {value}\r\n")
        mask = b"\xff\xff\xff\xff"
        pattern = ecu_id.to_bytes(4, "big")
        flow = tester_id.to_bytes(4, "big")
        self.command(f"atf{ISO15765} {FLOW_CONTROL_FILTER} {TX_ISO15765_FRAME_PAD} 4\r\n",
                     expect="arf", payload=mask + pattern + flow)
        self.tester_id = tester_id
        self.ecu_id = ecu_id

    def connect_raw_can(self, baud: int = 500000) -> None:
        """Open a raw CAN channel with a pass-all filter, for listening only: nothing is ever
        transmitted on it (the adapter still acknowledges frames, as every CAN node does)."""
        try:
            self.command(f"ato{CAN} {CAN_ID_BOTH} {baud} 0\r\n")
        except OpenPortError:
            self.command(f"ato{CAN} 0 {baud} 0\r\n")            # 11-bit identifiers only
        self._raw_open = True
        self.command(f"ats{CAN} {CFG_LOOPBACK} 0\r\n")
        zero = b"\x00\x00\x00\x00"                               # mask 0: every identifier passes
        self.command(f"atf{CAN} {PASS_FILTER} 0 4\r\n", expect="arf", payload=zero + zero)

    def listen(self, seconds: float) -> list[tuple[float, int, int, bytes]]:
        """Collect raw CAN frames for `seconds`: (host time, device timestamp us, id, data)."""
        self._frames.clear()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            item = self._next_item()
            if item is None:
                self._fill()
            elif item[0] == "pkt":
                self._handle_packet(*item[1:])
            elif item[1].startswith("are"):
                raise OpenPortError(_describe_error(item[1]))
        frames, self._frames = self._frames, []
        return frames

    def take_frames(self) -> list[tuple[float, int, int, bytes]]:
        """The raw CAN frames received since the last call (while listening or making requests)."""
        frames, self._frames = self._frames, []
        return frames

    def close_iso15765(self) -> None:
        if self._channel_open:
            self.command(f"atc{ISO15765}\r\n", timeout=1.0)
            self._channel_open = False

    def raw_request(self, payload: bytes, timeout: float = 0.5, tx_id: int = 0x7E0, rx_id: int = 0x7E8) -> bytes | None:
        """A diagnostic request sent as one plain CAN frame on the raw channel, for when the adapter can't
        keep the ISO15765 channel open beside it. Only single-frame messages (up to 7 bytes each way) -
        enough for SSM reads of one address. The frames go into the capture like any others."""
        if len(payload) > 7:
            raise ValueError("raw_request handles single-frame messages only")
        frame = bytes([len(payload)]) + payload
        frame += bytes(8 - len(frame))                                # pad to 8 bytes
        start = len(self._frames)
        data = tx_id.to_bytes(4, "big") + frame
        self._write(f"att{CAN} {len(data)} 0\r\n".encode() + data)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for _, _, cid, d in self._frames[start:]:
                if cid == rx_id and d:
                    pci = d[0]
                    return bytes(d[1:1 + (pci & 0x0F)]) if pci >> 4 == 0 else None   # first frame: not handled
            start = len(self._frames)
            item = self._next_item()
            if item is None:
                self._fill()
            elif item[0] == "pkt":
                self._handle_packet(*item[1:])
            elif item[1].startswith("are"):
                raise OpenPortError(_describe_error(item[1]))
        return None

    def send(self, payload: bytes) -> None:
        data = self.tester_id.to_bytes(4, "big") + payload
        self._write(f"att{ISO15765} {len(data)} {TX_ISO15765_FRAME_PAD}\r\n".encode() + data)

    def recv(self, timeout: float = 1.0) -> bytes | None:
        """Return the next complete message from the ECU (without the CAN id)."""
        deadline = time.monotonic() + timeout
        while True:
            while self._rx_queue:
                _, msg = self._rx_queue.pop(0)
                if len(msg) >= 4 and int.from_bytes(msg[:4], "big") == self.ecu_id:
                    return msg[4:]
            item = self._next_item()
            if item is not None:
                if item[0] == "pkt":
                    self._handle_packet(*item[1:])
                elif item[1].startswith("are"):
                    raise OpenPortError(_describe_error(item[1]))
                continue
            if time.monotonic() >= deadline:
                return None
            self._fill()

    def request(self, payload: bytes, timeout: float = 1.0) -> bytes | None:
        self._rx_queue.clear()
        self.send(payload)
        return self.recv(timeout)

    def close(self) -> None:
        try:
            for open_, proto in ((self._channel_open, ISO15765), (self._raw_open, CAN)):
                if open_:
                    try:
                        self.command(f"atc{proto}\r\n", timeout=1.0)
                    except OpenPortError:
                        pass
            self._write(b"atz\r\n")
        finally:
            self.ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
