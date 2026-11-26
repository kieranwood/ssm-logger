// Tactrix OpenPort 2.0 driver over Web Serial - a port of openport.py (see there for the
// command set). Works with any object shaped like a Web Serial SerialPort, including the demo
// ECU in fake-ecu.js.
"use strict";

const OP = {
  ISO15765: 6,
  CH_ISO15765: 0x36,                 // "6"
  TX_ISO15765_FRAME_PAD: 0x40,
  FLOW_CONTROL_FILTER: 3,
  CAN: 5, CH_CAN: 0x35,              // raw CAN frames, listen-only (as openport.connect_raw_can)
  CAN_ID_BOTH: 0x800, PASS_FILTER: 1,
  CFG: { LOOPBACK: 0x03, ISO15765_BS: 0x1e, ISO15765_STMIN: 0x1f, BS_TX: 0x22, STMIN_TX: 0x23,
         ISO15765_WFT_MAX: 0x25 },
  NORM_MSG: 0x00, RX_MSG_END_IND: 0x40, EXT_ADDR_MSG_END_IND: 0x44,
  ERRORS: {
    1: "not supported", 2: "invalid channel id", 3: "invalid protocol id", 4: "null parameter",
    5: "invalid ioctl value", 6: "invalid flags", 7: "failed", 8: "device not connected",
    9: "timeout (nothing on the CAN bus acknowledged the message - is the car connected with ignition on?)",
    10: "invalid message", 11: "invalid time interval", 12: "exceeded limit", 13: "invalid message id",
    14: "device in use", 15: "invalid ioctl id", 16: "buffer empty", 17: "buffer full",
    18: "buffer overflow", 19: "pin invalid", 20: "channel in use", 21: "message protocol id",
    22: "invalid filter id", 23: "no flow control", 24: "not unique", 25: "invalid baud rate",
    26: "invalid device id",
  },
};

class OpenPortError extends Error {}

const sleepMs = (ms) => new Promise((r) => setTimeout(r, ms));

function describeOpenPortError(line) {
  const code = Number(line.split(/\s+/)[1]);
  return Number.isNaN(code) ? line : `J2534 error ${code}: ${OP.ERRORS[code] || "unknown"}`;
}

const encodeText = (s) => new TextEncoder().encode(s);
const u32be = (n) => new Uint8Array([(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255]);
const concatBytes = (...parts) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
};

class OpenPort {
  constructor(port) {
    this.port = port;
    this.buf = new Uint8Array(0);
    this.lines = [];             // text replies, in order
    this.rxQueue = [];           // complete ECU messages (with CAN id)
    this.rxParts = new Uint8Array(0);
    this.waiters = new Set();    // callbacks run whenever new data is parsed
    this.firmware = "";
    this.channelOpen = false;
    this.closed = false;
    this.portClosed = false;     // the serial port itself was closed (close() can fail)
    this.readError = null;
    this.rawOpen = false;
    this.onCanFrame = null;      // (id, data: Uint8Array, deviceMicros) for each raw CAN frame
  }

  // ---------------------------------------------------------------- low level
  async openPort() {
    await this.port.open({ baudRate: 115200 });
    this.writer = this.port.writable.getWriter();
    this.reader = this.port.readable.getReader();
    this.readLoop = this._readLoop();
  }

  async _readLoop() {
    try {
      while (!this.closed) {
        const { value, done } = await this.reader.read();
        if (done) break;
        if (value && value.length) {
          this.buf = concatBytes(this.buf, value);
          this._parse();
        }
      }
    } catch (e) {
      if (!this.closed) this.readError = e;
    }
    this._wake();
  }

  _wake() {
    for (const w of [...this.waiters]) w();
  }

  async _write(bytes) {
    await this.writer.write(bytes);
  }

  _parse() {
    for (;;) {
      const buf = this.buf;
      let i = -1;
      for (let k = 0; k + 1 < buf.length; k++) if (buf[k] === 0x61 && buf[k + 1] === 0x72) { i = k; break; }
      if (i < 0) { this.buf = buf.length > 1 ? buf.slice(-1) : buf; break; }
      if (i) { this.buf = buf.slice(i); continue; }
      if (buf.length < 3) break;
      const ch = buf[2];
      if (ch === OP.CH_ISO15765 || ch === 0x35 || ch === 0x34 || ch === 0x33) {   // binary packet
        if (buf.length < 4) break;
        const total = buf[3] + 4;
        if (buf.length < total) break;
        if (ch === OP.CH_CAN) {
          if (buf[4] === OP.NORM_MSG && total >= 13 && this.onCanFrame) {
            const stamp = ((buf[5] << 24) | (buf[6] << 16) | (buf[7] << 8) | buf[8]) >>> 0;
            const id = ((buf[9] << 24) | (buf[10] << 16) | (buf[11] << 8) | buf[12]) >>> 0;
            this.onCanFrame(id, buf.slice(13, total), stamp);
          }
        } else {
          this._handlePacket(buf[4], buf.slice(9, total));
        }
        this.buf = buf.slice(total);
        continue;
      }
      let end = -1;
      for (let k = 0; k + 1 < buf.length; k++) if (buf[k] === 13 && buf[k + 1] === 10) { end = k; break; }
      if (end < 0) break;
      this.lines.push(String.fromCharCode(...buf.slice(0, end)));
      this.buf = buf.slice(end + 2);
    }
    this._wake();
  }

  _handlePacket(type, data) {
    if (type === OP.NORM_MSG || type === OP.RX_MSG_END_IND || type === OP.EXT_ADDR_MSG_END_IND) {
      // long ISO-TP messages arrive in parts that each repeat the 4-byte CAN id
      const p = this.rxParts;
      if (p.length && data.length >= 4 && data[0] === p[0] && data[1] === p[1] && data[2] === p[2] && data[3] === p[3]) {
        data = data.slice(4);
      }
      this.rxParts = concatBytes(this.rxParts, data);
      if (type !== OP.NORM_MSG) {
        if (this.rxParts.length) this.rxQueue.push(this.rxParts);
        this.rxParts = new Uint8Array(0);
      }
    }
    // TX done, loopback and start-of-message indications are ignored
  }

  // Wait until check() returns something other than undefined, or the timeout passes.
  _waitFor(check, timeoutMs) {
    return new Promise((resolve, reject) => {
      let timer;
      const done = (fn, v) => { clearTimeout(timer); this.waiters.delete(poll); fn(v); };
      const poll = () => {
        if (this.readError) return done(reject, new OpenPortError(`Serial read failed: ${this.readError.message}`));
        let v;
        try { v = check(); } catch (e) { return done(reject, e); }
        if (v !== undefined) done(resolve, v);
      };
      timer = setTimeout(() => done(resolve, undefined), timeoutMs);
      this.waiters.add(poll);
      poll();
    });
  }

  async command(text, { expect = "aro", timeoutMs = 2000, payload = null } = {}) {
    const bytes = encodeText(text);
    await this._write(payload ? concatBytes(bytes, payload) : bytes);
    const line = await this._waitFor(() => {
      while (this.lines.length) {
        const l = this.lines.shift();
        if (l.startsWith("are")) throw new OpenPortError(`${describeOpenPortError(l)} (command ${JSON.stringify(text.trim())})`);
        if (l.startsWith(expect)) return l;
      }
      return undefined;
    }, timeoutMs);
    if (line === undefined) throw new OpenPortError(`Timeout waiting for ${expect} after ${JSON.stringify(text.trim())}`);
    return line;
  }

  // ---------------------------------------------------------------- device
  async open() {
    await this.openPort();
    this.firmware = await this.command("\r\n\r\nati\r\n", { expect: "ari" });
    await this.command("ata\r\n");
    return this.firmware;
  }

  async batteryVoltage() {
    const line = await this.command("atr 16\r\n", { expect: "arr" });
    return Number(line.split(/\s+/)[2]) / 1000;
  }

  async connectCan(baud = 500000, testerId = 0x7e0, ecuId = 0x7e8) {
    const P = OP.ISO15765;
    await this.command(`ato${P} 0 ${baud} 0\r\n`);
    this.channelOpen = true;
    const C = OP.CFG;
    for (const [param, value] of [[C.LOOPBACK, 0], [C.ISO15765_BS, 0], [C.ISO15765_STMIN, 0],
                                  [C.BS_TX, 0xffff], [C.STMIN_TX, 0xffff], [C.ISO15765_WFT_MAX, 0]]) {
      await this.command(`ats${P} ${param} ${value}\r\n`);
    }
    const payload = concatBytes(new Uint8Array([255, 255, 255, 255]), u32be(ecuId), u32be(testerId));
    await this.command(`atf${P} ${OP.FLOW_CONTROL_FILTER} ${OP.TX_ISO15765_FRAME_PAD} 4\r\n`,
                       { expect: "arf", payload });
    this.testerId = testerId;
    this.ecuId = ecuId;
  }

  /** Open a raw CAN channel with a pass-all filter, for listening: nothing is transmitted on it
   *  (the adapter still acknowledges frames, as every CAN node does). Frames go to onCanFrame. */
  async connectRawCan(baud = 500000) {
    const P = OP.CAN;
    try {
      await this.command(`ato${P} ${OP.CAN_ID_BOTH} ${baud} 0\r\n`);
    } catch (e) {
      if (/channel in use|device in use/i.test(e.message)) throw e;
      await this.command(`ato${P} 0 ${baud} 0\r\n`);          // 11-bit identifiers only
    }
    this.rawOpen = true;
    await this.command(`ats${P} ${OP.CFG.LOOPBACK} 0\r\n`);
    const zero = new Uint8Array(8);                               // mask 0, pattern 0: everything passes
    await this.command(`atf${P} ${OP.PASS_FILTER} 0 4\r\n`, { expect: "arf", payload: zero });
  }

  async closeRawCan() {
    if (!this.rawOpen) return;
    this.rawOpen = false;
    this.onCanFrame = null;
    await this.command(`atc${OP.CAN}\r\n`, { timeoutMs: 800 });
  }

  async send(payload) {
    const data = concatBytes(u32be(this.testerId), payload);
    await this._write(concatBytes(encodeText(`att${OP.ISO15765} ${data.length} ${OP.TX_ISO15765_FRAME_PAD}\r\n`), data));
  }

  /** Next complete message from the ECU (without the CAN id), or null after the timeout. */
  async recv(timeoutMs = 1000) {
    const msg = await this._waitFor(() => {
      while (this.lines.length) {
        const l = this.lines.shift();
        if (l.startsWith("are")) throw new OpenPortError(describeOpenPortError(l));
      }
      while (this.rxQueue.length) {
        const m = this.rxQueue.shift();
        if (m.length >= 4 && ((m[0] << 24) | (m[1] << 16) | (m[2] << 8) | m[3]) >>> 0 === this.ecuId) {
          return m.slice(4);
        }
      }
      return undefined;
    }, timeoutMs);
    return msg === undefined ? null : msg;
  }

  async request(payload, timeoutMs = 1000) {
    this.rxQueue = [];
    await this.send(payload);
    return this.recv(timeoutMs);
  }

  /** Say goodbye to the cable if it's still there, then release and close the serial port.
   *  Safe to call again if a previous close couldn't close the port. Returns true once closed. */
  async close() {
    if (this.portClosed) return true;
    const limit = (p, ms) => Promise.race([p, sleepMs(ms)]);   // a dead cable mustn't hang us
    if (!this.closed && !this.readError && this.writer) {
      if (this.rawOpen) {
        try { await limit(this.command(`atc${OP.CAN}\r\n`, { timeoutMs: 500 }), 800); } catch { /* ignore */ }
      }
      if (this.channelOpen) {
        try { await limit(this.command(`atc${OP.ISO15765}\r\n`, { timeoutMs: 500 }), 800); } catch { /* ignore */ }
      }
      try { await limit(this._write(encodeText("atz\r\n")), 300); } catch { /* ignore */ }
    }
    this.closed = true;
    try { await limit(this.reader?.cancel(), 500); } catch { /* ignore */ }
    if (this.readLoop) await limit(this.readLoop, 500);
    try { this.reader?.releaseLock(); } catch { /* ignore */ }
    try { await limit(this.writer?.abort(), 300); } catch { /* ignore */ }
    try { this.writer?.releaseLock(); } catch { /* ignore */ }
    try {
      await this.port.close();          // always: the port stays open even once its streams are gone
      this.portClosed = true;
    } catch (e) {
      if (/already closed|not open/i.test(e.message)) this.portClosed = true;
      else console.warn("Could not close the serial port:", e);
    }
    return this.portClosed;
  }
}
