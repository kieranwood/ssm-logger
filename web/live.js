// Live logging over Web Serial - the browser counterpart of sources.EcuSource and the
// desktop logger's CsvRecorder. Decoding (defs.py) and fault codes (dtc.py) run in Python.
"use strict";

const SSM_ECU_INIT = new Uint8Array([0xaa]);
const SSM_READ_ADDRESSES = 0xa8;
const SSM_READ_REPLY = 0xe8;
const MAX_ADDRESSES_PER_READ = 84;   // measured on a 2012 Forester (ECU 7152345007); more -> NRC 0x13

// RomRaider's logger definitions, fetched from the same pinned commit as fetch_defs.py.
const ROMRAIDER_COMMIT = "d37c5eb786f3d2bc4b9353ffa859763f4ba596a5";
const DEFS_URL = `https://raw.githubusercontent.com/RomRaider/RomRaider/${ROMRAIDER_COMMIT}/definitions/log_defs.xml`;
const DEFS_SHA256 = "bb371ef1e6e956cb5081b12abe840eea1c6a00ce4c59fb790938c705433ef4e1";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function sha256Hex(bytes) {
  const d = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Download log_defs.xml (checked against its known hash) and load it into defs.py. */
async function loadDefs(py) {
  const r = await fetch(DEFS_URL);
  if (!r.ok) throw new Error(`could not download RomRaider's log_defs.xml (HTTP ${r.status})`);
  const bytes = new Uint8Array(await r.arrayBuffer());
  if ((await sha256Hex(bytes)) !== DEFS_SHA256) throw new Error("RomRaider's log_defs.xml has an unexpected checksum");
  py.FS.writeFile("/tmp/log_defs.xml", bytes);
  return py.globals.get("load_defs")("/tmp/log_defs.xml");
}

/** Logged channels: derived ones first, then every supported parameter (as logger.build_channels). */
function buildLiveChannels(params) {
  const names = new Set(params.map((p) => p.name));
  const chans = [];
  if (names.has("Air/Fuel Correction #1") && names.has("Air/Fuel Learning #1")) {
    chans.push({ name: "Total Fuel Trim", units: "%", decimals: 2, switch: false,
                 compute: (v) => v["Air/Fuel Correction #1"] + v["Air/Fuel Learning #1"] });
  }
  if (names.has("Air/Fuel Sensor #1")) {
    chans.push({ name: "Air/Fuel Sensor #1 AFR", units: "AFR", decimals: 2, switch: false,
                 compute: (v) => v["Air/Fuel Sensor #1"] * 14.7 });
  }
  for (const p of params) {
    chans.push({ name: p.name, units: p.units, decimals: p.decimals, switch: p.switch, compute: (v) => v[p.name] });
  }
  return chans;
}

class LiveSession {
  /**
   * port: a Web Serial SerialPort (or DemoSerialPort). Callbacks:
   *   onStatus(text, isError), onConnected({ecuId, channels, volts, firmware}), onSample(t, row)
   */
  constructor(py, port, { onStatus, onConnected, onSample }) {
    this.py = py;
    this.port = port;
    this.onStatus = onStatus;
    this.onConnected = onConnected;
    this.onSample = onSample;
    this.stopped = false;
    this.jobs = [];
    this.channels = null;          // fixed by the first connection
    this.rate = 0;
    this.decodePy = py.globals.get("decode");
    this.t0 = performance.now();
  }

  /** Run fn(request) on the polling loop between reads; request(payload, timeoutSec) -> Uint8Array|null. */
  runJob(fn) {
    return new Promise((resolve, reject) => this.jobs.push({ fn, resolve, reject }));
  }

  async _runJobs(op) {
    while (this.jobs.length) {
      const { fn, resolve, reject } = this.jobs.shift();
      const request = (payload, timeout) => {
        const p = new Uint8Array(payload);
        // undefined (not null) so Python sees None when the ECU doesn't answer
        return (p.length ? op.request(p, timeout * 1000) : op.recv(timeout * 1000)).then((r) => r ?? undefined);
      };
      try { resolve(await fn(request)); } catch (e) { reject(e); }
    }
  }

  async _connect() {
    this.onStatus("Connecting to OpenPort…");
    const op = new OpenPort(this.port);
    try {
      const firmware = await op.open();
      const volts = await op.batteryVoltage();
      this.onStatus(`Connecting to ECU (OBD ${volts.toFixed(1)} V)…`);
      await op.connectCan();
      const reply = await op.request(SSM_ECU_INIT, 2000);
      if (!reply || reply[0] !== 0xea) throw new OpenPortError("No reply from the ECU - is the ignition on?");
      const sel = JSON.parse(this.py.globals.get("select_params")(reply.slice(1)));
      this.requests = [];
      for (let i = 0; i < sel.addresses.length; i += MAX_ADDRESSES_PER_READ) {
        const chunk = sel.addresses.slice(i, i + MAX_ADDRESSES_PER_READ);
        const req = new Uint8Array(2 + chunk.length * 3);
        req[0] = SSM_READ_ADDRESSES;
        chunk.forEach((a, k) => req.set([(a >> 16) & 255, (a >> 8) & 255, a & 255], 2 + k * 3));
        this.requests.push({ n: chunk.length, req });
      }
      if (!this.channels) {
        this.params = sel.params;
        this.channels = buildLiveChannels(sel.params);
        this.ecuId = sel.ecuId;
        this.onConnected({ ecuId: sel.ecuId, channels: this.channels, volts, firmware });
      }
      return op;
    } catch (e) {
      await op.close();
      throw e;
    }
  }

  async _read(op) {
    const parts = [];
    for (const { n, req } of this.requests) {
      const reply = await op.request(req, 500);
      if (!reply || reply[0] !== SSM_READ_REPLY || reply.length !== n + 1) return null;
      parts.push(reply.slice(1));
    }
    return concatBytes(...parts);
  }

  _decode(data) {
    const res = this.decodePy(data);
    const values = res.toJs();
    res.destroy();
    const byName = {};
    this.params.forEach((p, i) => { byName[p.name] = values[i]; });
    return this.channels.map((c) => c.compute(byName));
  }

  async run() {
    while (!this.stopped) {
      let op;
      try {
        op = await this._connect();
      } catch (e) {
        if (this.stopped) break;
        this.onStatus(`Not connected: ${e.message} - retrying`, true);
        await sleep(2000);
        continue;
      }
      this.op = op;
      this.onStatus("Logging");
      let failures = 0, count = 0, tRate = performance.now();
      try {
        while (!this.stopped) {
          await this._runJobs(op);
          const data = await this._read(op);
          if (!data) {
            if (++failures >= 5) throw new OpenPortError("ECU stopped answering");
            continue;
          }
          failures = 0;
          this.onSample((performance.now() - this.t0) / 1000, this._decode(data));
          count++;
          const now = performance.now();
          if (now - tRate >= 1000) {
            this.rate = (count * 1000) / (now - tRate);
            count = 0;
            tRate = now;
          }
        }
      } catch (e) {
        if (!this.stopped) this.onStatus(`Connection lost: ${e.message} - retrying`, true);
        this.rate = 0;
      } finally {
        this.op = null;
        await op.close();
      }
      if (!this.stopped) await sleep(1000);
    }
    for (const j of this.jobs.splice(0)) j.reject(new Error("Disconnected"));
  }

  stop() {
    this.stopped = true;
  }
}

/** Builds a CSV in the desktop logger's format (logger.CsvRecorder) for download. */
class CsvRecorder {
  constructor(channels, ecuId) {
    const quote = (s) => (/[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);
    const labels = channels.map((c) => quote(c.units ? `${c.name} (${c.units})` : c.name));
    this.chunks = [`# ECU ${ecuId}\n`, ["time_s", "mark", ...labels].join(",") + "\n"];
    this.tStart = null;
    this.rows = 0;
    this.started = new Date();
  }

  static g4(v) {        // Python's "%.4g"
    if (v == null || Number.isNaN(v)) return "nan";
    if (!Number.isFinite(v)) return v > 0 ? "inf" : "-inf";
    let s = v.toPrecision(4);
    if (s.includes("e")) {
      let [m, e] = s.split("e");
      if (m.includes(".")) m = m.replace(/0+$/, "").replace(/\.$/, "");
      const n = Number(e);
      return `${m}e${n < 0 ? "-" : "+"}${String(Math.abs(n)).padStart(2, "0")}`;
    }
    if (s.includes(".")) s = s.replace(/0+$/, "").replace(/\.$/, "");
    return s;
  }

  write(t, row, mark) {
    if (this.tStart === null) this.tStart = t;
    this.chunks.push(`${(t - this.tStart).toFixed(3)},${mark || ""},${row.map(CsvRecorder.g4).join(",")}\n`);
    this.rows++;
  }

  filename() {
    const d = this.started, p = (n) => String(n).padStart(2, "0");
    return `ssm_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.csv`;
  }

  blob() {
    return new Blob(this.chunks, { type: "text/csv" });
  }
}
