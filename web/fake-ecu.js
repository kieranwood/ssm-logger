// Demo "cable": an object shaped like a Web Serial SerialPort that emulates a Tactrix OpenPort 2.0
// with a Subaru ECU behind it. The ECU replays a recording, encoding each value back into the raw
// bytes the real ECU would send, so the live demo runs through the real driver (openport.js),
// SSM decoding (defs.py) and fault-code reader (dtc.py).
"use strict";

// Init reply captured from the 2012 Forester (same as DEMO_INIT in sources.py).
const DEMO_ECU_INIT = "a2100a7152345007f3fac98c0202fea800000046ce54fdb800000f2000000000" +
                      "00dc00005d1f3080f024100043fb00f5c98e0000000121f18000b1";

// Simulated mode 06 test results (same as DEMO_MODE06 in sources.py): MID -> records.
const DEMO_MODE06 = {
  0x01: "01 05 10 0048 0000 0190 01 06 10 0052 0000 0190 01 81 84 0370 fc18 03e8",
  0x21: "21 80 01 01a4 0000 02bc",
  0x41: "41 81 14 0019 0000 0050",
  0xa1: "a1 0b 24 0000 0000 ffff a1 0c 24 0000 0000 ffff",
};

// Simulated mode 09 vehicle information (same as DEMO_MODE09 in sources.py; the VIN is fake).
const DEMO_MODE09 = {
  0x02: "0144454d4f3056494e303030303030303030",
  0x04: "01413257433531304e0000000000000000",
  0x06: "011a2b3c4d",
  0x08: "10007801900028006e000000000034006e00000000001e006400000000000c005f",
  0x0a: "0145434d002d456e67696e65436f6e74726f6c00",
};

const hexBytes = (hex) => new Uint8Array(hex.replace(/\s+/g, "").match(/../g).map((h) => parseInt(h, 16)));

class DemoEcu {
  /** params: param_table() rows; sample: {t: number[], columns: Map<name, number[]>} */
  constructor(params, sample) {
    this.sample = sample;
    this.t0 = performance.now();
    this.duration = sample.t[sample.t.length - 1] - sample.t[0];
    this.encoders = [];
    for (const p of params) {
      const values = sample.columns.get(p.name);
      if (!values) continue;
      const f = new Function("x", `return ${p.expr};`);   // expr is digits, operators and x only (defs.py checks)
      this.encoders.push({ p, values, f0: f(0), scale: f(1) - f(0) });
    }
    this.rowCache = { idx: -1, memory: null };
    // Stored, pending and permanent codes: P0171 until cleared.
    this.codes = { 0x03: [[0x01, 0x71]], 0x07: [[0x01, 0x71]], 0x0a: [[0x01, 0x71]] };
  }

  /** The recording's values right now (channel name -> value), for the demo CAN traffic. */
  values() {
    const t = this.sample.t;
    const now = t[0] + (((performance.now() - this.t0) / 1000) % this.duration);
    let lo = 0, hi = t.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (t[mid] <= now) lo = mid; else hi = mid - 1; }
    const out = {};
    for (const [name, col] of this.sample.columns) out[name] = col[lo];
    return out;
  }

  _memory() {
    const t = this.sample.t;
    const now = t[0] + (((performance.now() - this.t0) / 1000) % this.duration);
    let lo = 0, hi = t.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (t[mid] <= now) lo = mid; else hi = mid - 1; }
    if (this.rowCache.idx === lo) return this.rowCache.memory;
    const mem = new Map();
    for (const { p, values, f0, scale } of this.encoders) {
      const v = values[lo];
      if (!Number.isFinite(v)) continue;
      if (p.switch) {
        if (v >= 0.5) mem.set(p.address, (mem.get(p.address) || 0) | (1 << p.switchBit));
        continue;
      }
      if (!scale) continue;
      const bits = p.length * 8;
      let raw = Math.round((v - f0) / scale);
      const min = p.signed ? -(2 ** (bits - 1)) : 0;
      const max = p.signed ? 2 ** (bits - 1) - 1 : 2 ** bits - 1;
      raw = Math.min(max, Math.max(min, raw));
      if (raw < 0) raw += 2 ** bits;
      for (let i = p.length - 1; i >= 0; i--) {
        mem.set(p.address + i, raw % 256);
        raw = Math.floor(raw / 256);
      }
    }
    this.rowCache = { idx: lo, memory: mem };
    return mem;
  }

  handle(req) {
    const mode = req[0];
    if (mode === 0xaa) return new Uint8Array([0xea, ...hexBytes(DEMO_ECU_INIT)]);
    if (mode === 0xa8) {
      const mem = this._memory();
      const out = [0xe8];
      for (let i = 2; i + 2 < req.length; i += 3) out.push(mem.get((req[i] << 16) | (req[i + 1] << 8) | req[i + 2]) || 0);
      return new Uint8Array(out);
    }
    if (mode === 0x04) {
      this.codes = { 0x03: [], 0x07: [], 0x0a: [[0x01, 0x71]] };
      return new Uint8Array([0x44]);
    }
    const supported = (pid, keys) => {
      let bits = 0;
      for (const k of keys) if (pid < k && k <= pid + 32) bits += 2 ** (32 - (k - pid));
      return [(bits / 2 ** 24) & 255, (bits / 2 ** 16) & 255, (bits / 2 ** 8) & 255, bits & 255];
    };
    if (mode === 0x02 && req.length === 3) {          // freeze frame, as in sources.DemoSource
      const ff = !this.codes[0x03].length ? { 0x02: [0, 0] } : {
        0x02: [0x01, 0x71], 0x03: [0x02, 0x00], 0x04: [0x6e], 0x05: [0x80], 0x06: [0xa0], 0x07: [0x90],
        0x0b: [0x2d], 0x0c: [0x22, 0x60], 0x0d: [0x5f], 0x0f: [0x46], 0x10: [0x05, 0xdc], 0x11: [0x30],
        0x15: [0xaa, 0xff], 0x1f: [0x00, 0xb4], 0x34: [0x80, 0x00, 0x80, 0x00] };
      const pid = req[1];
      const keys = Object.keys(ff).map(Number);
      if (pid === 0x00 || pid === 0x20 || pid === 0x40) {
        return new Uint8Array([0x42, pid, 0, ...supported(pid, keys.length > 1 ? [...keys, 0x20, 0x40] : keys)]);
      }
      return ff[pid] ? new Uint8Array([0x42, pid, 0, ...ff[pid]]) : null;
    }
    if (mode === 0x01 && req.length === 2) {
      const live = { 0x01: [0x00, 0x07, 0x65, 0x21], 0x03: [0x02, 0x00], 0x21: [0x00, 0x00], 0x30: [0x03],
                     0x31: [0x00, 0x2a], 0x41: [0x00, 0x07, 0x65, 0x65], 0x4e: [0x00, 0x5a] };
      if (this.codes[0x03].length) live[0x01] = [0x81, 0x07, 0x65, 0x21];   // MIL on, 1 code
      const pid = req[1];
      if (pid === 0x00 || pid === 0x20 || pid === 0x40) {
        return new Uint8Array([0x41, pid, ...supported(pid, [...Object.keys(live).map(Number), 0x20, 0x40])]);
      }
      return live[pid] ? new Uint8Array([0x41, pid, ...live[pid]]) : null;
    }
    if (mode === 0x09 && req.length === 2) {
      const type = req[1];
      if (type === 0x00) return new Uint8Array([0x49, 0x00, ...supported(0, Object.keys(DEMO_MODE09).map(Number))]);
      return DEMO_MODE09[type] ? new Uint8Array([0x49, type, ...hexBytes(DEMO_MODE09[type])]) : null;
    }
    if (mode === 0x06 && req.length === 2) {
      const mid = req[1];
      if (mid % 0x20 === 0) {
        return new Uint8Array([0x46, mid, ...supported(mid, [...Object.keys(DEMO_MODE06).map(Number), 0x20, 0x40, 0x60, 0x80, 0xa0])]);
      }
      return DEMO_MODE06[mid] ? new Uint8Array([0x46, ...hexBytes(DEMO_MODE06[mid])]) : null;
    }
    if (this.codes[mode]) {
      const c = this.codes[mode];
      return new Uint8Array([mode + 0x40, c.length, ...c.flat()]);
    }
    return new Uint8Array([0x7f, mode, 0x11]);
  }
}

class DemoSerialPort {
  constructor(ecu) {
    this.ecu = ecu;
    this.readable = null;
    this.writable = null;
    this.connected = true;
    this.onCable = null;          // (type, port) - set by the app, like navigator.serial's events
  }

  getInfo() {
    return { demo: true };
  }

  // Same rules as a real Web Serial port, so the app's error handling can be tested on the demo.
  async open() {
    if (!this.connected) throw new DOMException("Failed to open serial port.", "NetworkError");
    if (this.isOpen) throw new DOMException("Failed to execute 'open' on 'SerialPort': The port is already open.", "InvalidStateError");
    this.isOpen = true;
    this.inbuf = [];
    this.pending = null;          // {n, kind} raw bytes expected after a command line
    this.readable = new ReadableStream({ start: (c) => { this.ctrl = c; } });
    this.writable = new WritableStream({ write: (chunk) => { if (!this.connected) throw new DOMException("The device has been lost.", "NetworkError"); this._input(chunk); } });
  }

  async close() {
    this._stopBus();
    if (!this.isOpen) throw new DOMException("The port is already closed.", "InvalidStateError");
    if (this.readable?.locked || this.writable?.locked) throw new DOMException("Cannot close a port with locked streams.", "InvalidStateError");
    try { this.ctrl.close(); } catch { /* already closed or cancelled */ }
    this.readable = this.writable = null;
    this.isOpen = false;
  }

  /** Test hooks: pull the cable out / plug it back in. */
  unplug() {
    this.connected = false;
    try { this.ctrl?.error(new DOMException("The device has been lost.", "NetworkError")); } catch { /* closed */ }
    this.onCable?.("disconnect", this);
  }

  replug() {
    this.connected = true;
    this.readable = this.writable = null;     // a real replug is a fresh, closed port
    this.isOpen = false;
    this.onCable?.("connect", this);
  }

  _out(text) {
    try { this.ctrl.enqueue(typeof text === "string" ? new TextEncoder().encode(text) : text); } catch { /* closed */ }
  }

  _input(chunk) {
    for (const b of chunk) this.inbuf.push(b);
    for (;;) {
      if (this.pending) {
        if (this.inbuf.length < this.pending.n) return;
        const data = new Uint8Array(this.inbuf.splice(0, this.pending.n));
        const kind = this.pending.kind;
        this.pending = null;
        if (kind === "filter") this._out(`arf${this.pending0 || 6} 0\r\n`);
        else if (kind === "tx") this._transmit(data);
        continue;
      }
      let end = -1;
      for (let i = 0; i + 1 < this.inbuf.length; i++) if (this.inbuf[i] === 13 && this.inbuf[i + 1] === 10) { end = i; break; }
      if (end < 0) return;
      const line = String.fromCharCode(...this.inbuf.splice(0, end + 2).slice(0, end));
      this._command(line);
    }
  }

  _command(line) {
    if (!line) return;
    const parts = line.split(/\s+/);
    if (line.startsWith("ati")) this._out("ari Demo OpenPort - replaying a recording\r\n");
    else if (line.startsWith("atr")) this._out("arr 16 12450\r\n");
    else if (line.startsWith("atf")) {
      // a flow-control filter carries mask, pattern and flow-control id; pass/block filters only two
      this.pending = { n: (Number(parts[1]) === 3 ? 3 : 2) * Number(parts[3]), kind: "filter" };
      this.pending0 = Number(line[3]);
    } else if (line.startsWith("ato5")) {
      this._out("aro\r\n");
      this._startBus();
    } else if (line.startsWith("atc5")) {
      this._stopBus();
      this._out("aro\r\n");
    }
    else if (line.startsWith("att")) this.pending = { n: Number(parts[1]), kind: "tx" };
    else if (/^at[aosc]/.test(line)) this._out("aro\r\n");
    // atz: no reply
  }

  // ---- demo CAN traffic: the IDs, rates and housekeeping bytes of a capture from the 2012 Forester's
  // OBD port, carrying the replayed recording's values. Which byte carries what is made up for the demo.
  _startBus() {
    if (this.busTimer) return;
    this.busTick = 0;
    this.busPrev = {};
    this.busTimer = setInterval(() => this._busTick(), 10);
  }

  _stopBus() {
    clearInterval(this.busTimer);
    this.busTimer = null;
  }

  _busTick() {
    if (!this.isOpen || !this.connected) return this._stopBus();
    const k = ++this.busTick, v = this.ecu.values();
    const num = (name, d = 0) => (Number.isFinite(v[name]) ? v[name] : d);
    const rpm = num("Engine Speed"), thr = num("Throttle Opening Angle"), spd = num("Vehicle Speed");
    const load = num("Engine Load"), cool = num("Coolant Temperature", 80), brake = num("Brake Switch") >= 0.5;
    const neutral = num("Neutral Position Switch Signal") >= 0.5, iat = num("Intake Air Temperature", 30);
    const pressure = brake ? Math.min(60, (this.busPrev.press || 0) + 2) : Math.max(0, (this.busPrev.press || 0) - 4);
    this.busPrev.press = pressure;
    const c = k & 255, n4 = k & 15, noise = () => Math.round((Math.random() - 0.5) * 6);
    const le16 = (x) => [x & 255, (x >> 8) & 255];
    const sum = (b) => { b[7] = b.slice(0, 7).reduce((a, x) => a + x, 0) & 255; return b; };
    const r = Math.round(rpm / 1.6), w = Math.round(spd * 17.6);
    const frames = [
      [0x002, 1, [0x0c, 0x80, (n4 << 4) | 0x04, 0, 0, 0, 0, 0x01]],
      [0x070, 2, sum([0xf0 + noise(), 0x7f, 0, 0xff, 0x80 + noise(), 0x7f, n4, 0])],
      [0x075, 2, sum([0, 0, 0, 0, 0, Math.abs(noise()), n4, 0])],
      [0x410, 1, [Math.round(20 + thr * 0.6), 0, Math.round(34 + load * 0.4), 10, 0, ...le16(Math.max(0, 1120 - Math.round(load * 6))), 0xc4]],
      [0x411, 1, [0, 0xff, 0x3f, 0, 0, 0, brake ? 0x1c : 0x0c, 0x04]],
      [0x412, 1, [0, ...le16(r), ...le16(r), 22, 8, 2]],
      [0x420, 1, [0, neutral ? 0x88 : 0x77, 0x41, 0x40, Math.max(0, 137 - Math.round(thr * 4)), 0x40, 0xfa, 0x80]],
      [0x421, 1, [0xff, 0, 0xff, c, 0, 0xb0, 0, 0]],
      [0x422, 1, [0, 0, neutral ? 7 : 6, 0xff, 0x7f, 0x58, 0, 0x7d]],
      [0x501, 2, [0, 0, 0, 0xff, 0, c, 0, 0]],
      [0x511, 2, [0xfc, 0xff, 0, 0, Math.round(pressure), 1, 0, 0]],
      [0x512, 2, [0, 0xf0, 0, 0, brake ? 0x90 : 0x80, c, 0xff, 0x7f]],
      [0x513, 2, [...le16(w), ...le16(w + 2), ...le16(w + 1), ...le16(w + 3)]],
      [0x514, 2, [brake ? 8 : 0, 0, 0x8c, 0, 0xc8, 117, (k * 2) & 15, 0x80]],
      [0x576, 2, sum([0, 0, 0, 0, 10 + Math.abs(noise()), 25 + noise(), 0x10 | n4, 0])],
      [0x600, 5, [0, 128 + noise() * 8, 2, Math.round(cool + 40), c, 0x0e, 1, Math.round(iat + 40)]],
      [0x601, 1, [0, 0xde, 0, Math.round(70 + thr * 1.2), 0, 0x29, 0, 0]],
      [0x620, 2, [0x20, 0, neutral ? 7 : 3, 0, 0, 0, 0, 0]],
    ];
    const stamp = Math.round(performance.now() * 1000) >>> 0;
    for (const [id, every, data] of frames) {
      if (k % every) continue;
      const b = data.map((x) => Math.max(0, Math.min(255, Math.round(x))));
      this._out(new Uint8Array([0x61, 0x72, 0x35, 17, 0x00, stamp >>> 24, (stamp >> 16) & 255, (stamp >> 8) & 255, stamp & 255,
                                0, 0, id >> 8, id & 255, ...b]));
    }
  }

  _transmit(data) {
    const reply = this.ecu.handle(data.slice(4));
    if (!reply) return;
    const msg = new Uint8Array([0, 0, 0x07, 0xe8, ...reply]);
    const packet = new Uint8Array([0x61, 0x72, 0x36, 5 + msg.length, 0x40, 0, 0, 0, 0, ...msg]);
    setTimeout(() => this._out(packet), 40);         // about the real ECU turnaround (~22 reads/s)
  }
}

/** Parse a logger CSV into {t, columns: Map<channel name, number[]>} for the demo ECU. */
function parseSampleCsv(text) {
  const lines = text.split(/\r?\n/).filter((l) => l && !l.startsWith("#"));
  const header = lines[0].split(",");
  const names = header.map((h) => (h.match(/^(.*) \(([^()]*)\)$/) || [null, h])[1]);
  const cols = header.map(() => []);
  for (const line of lines.slice(1)) {
    const cells = line.split(",");
    cells.forEach((c, i) => cols[i]?.push(c === "" ? NaN : Number(c)));
  }
  const ti = header.findIndex((h) => h.toLowerCase().startsWith("time"));
  return { t: cols[ti], columns: new Map(names.map((n, i) => [n, cols[i]])) };
}
