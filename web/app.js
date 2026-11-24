// SSM Log Viewer: open a recording CSV or log live from the car, chart it, and run analyse.py on
// it - all in the browser.
//
// The desktop app's own Python modules (recording.py, analyse.py, defs.py, dtc.py) run with
// Pyodide via web/webapp.py. The cable is driven over Web Serial (openport.js, live.js).
// Nothing is uploaded anywhere.
"use strict";

const PY_MODULES = ["recording.py", "analyse.py", "defs.py", "dtc.py", "capture.py"];
// Deployed site: modules are copied to py/. Local dev (serving the repo root): they sit one level up.
const PY_BASES = ["py/", "../"];
// Deploys stamp ?v=<commit> on app.js; reuse it so every other file is refreshed together.
const VERSION = new URL(document.currentScript.src).searchParams.get("v") || "dev";

// Same defaults and colours as the desktop logger (logger.py).
const DEFAULT_CHANNELS = [
  "Total Fuel Trim", "Air/Fuel Correction #1", "Air/Fuel Learning #1",
  "Air/Fuel Sensor #1 AFR", "Rear O2 Sensor", "Air/Fuel Correction #3",
  "Air/Fuel Sensor #1 Current", "Air/Fuel Sensor #1 Resistance",
  "Air Flow Sensor Voltage", "Engine Speed", "Coolant Temperature",
  "Battery Voltage", "CPC Valve Duty Ratio",
];
const COLORS = ["#4e9fff", "#ff8a3d", "#3ecf8e", "#e5484d", "#b97cff", "#f5c542",
                "#2ec4d6", "#ff6fae", "#9aa5b1"];
const LEVEL_MARK = { problem: "✖", warning: "▲", info: "•", ok: "✔" };
const CHART_HEIGHT = 140;
const HISTORY_ROWS = 80_000;          // live history kept in memory (~1 hour at 22 samples/s)
const SAMPLE_URL = "samples/p0171-failing-af-sensor.csv";
const HELP_TEXT = "Drag across a chart to zoom in · double-click to zoom out";

const $ = (id) => document.getElementById(id);
const state = {
  py: null,            // Pyodide instance, once ready
  pyReady: null,       // promise
  defsReady: null,     // promise, once live mode has been used
  rec: null,           // {name, channels, t, y, marks, info, findings, live}
  selected: [],        // channel indices, in chart order
  charts: new Map(),   // channel index -> {u, el, valEl}
  highlight: [],       // [[t0, t1]] shaded on every chart (from the selected finding)
  syncing: false,
  cursorIdx: null,
  // live mode
  session: null,
  following: true,     // live charts track the newest data
  windowSec: 120,
  recorder: null,
  pendingMark: null,
  dirty: false,
};

// ------------------------------------------------------------------ Python

function setStatus(text, error = false) {
  $("status").textContent = text;
  $("status").classList.toggle("error", error);
}

const fetchOpts = { cache: VERSION === "dev" ? "no-store" : "default" };

async function fetchModule(name) {
  for (const base of PY_BASES) {
    const r = await fetch(`${base}${name}?v=${VERSION}`, fetchOpts).catch(() => null);
    if (r && r.ok) return r.text();
  }
  throw new Error(`could not load ${name}`);
}

async function startPython() {
  const py = await loadPyodide();
  await py.loadPackage("numpy");
  const glue = fetch(`webapp.py?v=${VERSION}`, fetchOpts).then((r) => r.text());
  const sources = await Promise.all(PY_MODULES.map(fetchModule));
  PY_MODULES.forEach((name, i) => py.FS.writeFile(`/home/pyodide/${name}`, sources[i]));
  py.FS.writeFile("/home/pyodide/webapp.py", await glue);
  py.runPython("from webapp import *");
  state.py = py;
  setStatus("");
  return py;
}

function bytesToF64(u8) {
  const copy = u8.slice();                     // aligned, standalone buffer
  return new Float64Array(copy.buffer);
}

function pyError(e) {
  return String(e.message || e).trim().split("\n").pop();
}

async function openText(name, text) {
  if (state.session) {
    setStatus("Disconnect from the car before opening a recording.", true);
    return;
  }
  setStatus(state.py ? "Reading…" : "Waiting for Python to load…");
  const py = await state.pyReady;
  setStatus("Reading…");
  await new Promise((r) => setTimeout(r));      // let the status paint
  const path = "/tmp/recording.csv";
  py.FS.writeFile(path, text);
  let res;
  try {
    res = py.globals.get("load")(path);
  } catch (e) {
    setStatus(`Could not open ${name}: ${pyError(e)}`, true);
    return;
  }
  const [metaJson, tBytes, yBytes] = res.toJs();
  res.destroy();
  const meta = JSON.parse(metaJson);
  const t = bytesToF64(tBytes);
  const flat = bytesToF64(yBytes);
  const n = t.length;
  const y = meta.channels.map((_, i) => flat.subarray(i * n, (i + 1) * n));
  showRecording({ name, t, y, ...meta, live: false });
  showFindings();
  setStatus("");
}

// ------------------------------------------------------------------ channel list

function fmt(v, decimals) {
  return v == null || Number.isNaN(v) ? "–" : v.toFixed(decimals);
}

function showRecording(rec) {
  for (const c of state.charts.values()) c.u.destroy();
  $("charts").replaceChildren();
  state.charts.clear();
  state.rec = rec;
  state.highlight = [];
  state.cursorIdx = null;
  $("findings").hidden = true;

  updateInfo();
  document.title = `${rec.name} - SSM Log Viewer`;

  const byName = new Map(rec.channels.map((c, i) => [c.name, i]));
  colorOf.clear();
  state.selected = DEFAULT_CHANNELS.filter((n) => byName.has(n)).map((n) => byName.get(n));
  if (!state.selected.length) state.selected = rec.channels.slice(0, 6).map((_, i) => i);

  state.selected.forEach(colorFor);
  buildChannelList();
  state.selected.forEach(addChart);
  $("filter").disabled = false;
  $("analyse").disabled = false;
  $("reset").disabled = false;
  document.body.classList.toggle("live", rec.live);
}

function updateInfo() {
  const rec = state.rec;
  const n = rec.t.length;
  const dur = n ? rec.t[n - 1] - rec.t[0] : 0;
  $("info").textContent = [rec.name, rec.info, `${(dur / 60).toFixed(1)} min`,
    `${n} rows, ${rec.channels.length} channels`].filter(Boolean).join("  |  ");
}

// Colours follow the order channels were switched on, like the desktop app, and stick to a
// channel until it is switched off.
const colorOf = new Map();
function colorFor(idx) {
  if (!colorOf.has(idx)) {
    const used = new Set(state.selected.filter((j) => j !== idx && colorOf.has(j)).map((j) => colorOf.get(j)));
    colorOf.set(idx, COLORS.find((c) => !used.has(c)) || COLORS[colorOf.size % COLORS.length]);
  }
  return colorOf.get(idx);
}

function buildChannelList() {
  const list = $("channels");
  list.replaceChildren();
  state.rec.channels.forEach((c, i) => {
    const row = document.createElement("div");
    row.className = "ch";
    row.dataset.idx = i;
    row.dataset.search = `${c.name} ${c.units}`.toLowerCase();
    row.title = c.units ? `${c.name} (${c.units})` : c.name;
    row.innerHTML = `<span class="swatch"></span><span class="name"></span><span class="val"></span>`;
    row.querySelector(".name").textContent = row.title;
    row.addEventListener("click", () => toggleChannel(i));
    list.appendChild(row);
  });
  refreshChannelList();
}

function refreshChannelList() {
  for (const row of $("channels").children) {
    const i = Number(row.dataset.idx);
    const on = state.selected.includes(i);
    row.classList.toggle("on", on);
    row.querySelector(".swatch").style.background = on ? colorFor(i) : "transparent";
  }
  updateValues();
}

function toggleChannel(i) {
  const at = state.selected.indexOf(i);
  if (at >= 0) {
    state.selected.splice(at, 1);
    const c = state.charts.get(i);
    c.u.destroy();
    c.el.remove();
    state.charts.delete(i);
    colorOf.delete(i);
  } else {
    state.selected.push(i);
    addChart(i);
  }
  refreshChannelList();
}

function updateValues() {
  const rec = state.rec;
  if (!rec) return;
  const n = rec.t.length;
  // live: show the newest values unless the cursor is over a chart
  const idx = state.cursorIdx != null && state.cursorIdx < n ? state.cursorIdx : rec.live && n ? n - 1 : null;
  for (const row of $("channels").children) {
    const i = Number(row.dataset.idx);
    const v = idx == null ? null : rec.y[i][idx];
    row.querySelector(".val").textContent = fmt(v, rec.channels[i].decimals);
  }
  for (const [i, c] of state.charts) {
    const v = idx == null ? null : rec.y[i][idx];
    c.valEl.textContent = idx == null ? "" : `${fmt(v, rec.channels[i].decimals)} ${rec.channels[i].units}`;
  }
  if (!rec.live) {
    $("cursor-readout").textContent = state.cursorIdx == null ? HELP_TEXT : `Cursor at ${rec.t[idx].toFixed(2)} s`;
  }
}

// ------------------------------------------------------------------ charts

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function chartWidth() {
  return $("charts").clientWidth;
}

function drawOverlays(u) {
  const { ctx, bbox } = u;
  const toX = (v) => u.valToPos(v, "x", true);
  ctx.save();
  ctx.beginPath();
  ctx.rect(bbox.left, bbox.top, bbox.width, bbox.height);
  ctx.clip();
  ctx.fillStyle = cssVar("--highlight");
  for (const [a, b] of state.highlight) {
    const x0 = toX(a), x1 = toX(b);
    ctx.fillRect(x0, bbox.top, Math.max(x1 - x0, 2), bbox.height);
  }
  ctx.strokeStyle = cssVar("--mark");
  ctx.fillStyle = cssVar("--mark");
  ctx.setLineDash([4, 4]);
  ctx.font = `${11 * devicePixelRatio}px sans-serif`;
  for (const [t, n] of state.rec.marks) {
    const x = Math.round(toX(t)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(x, bbox.top);
    ctx.lineTo(x, bbox.top + bbox.height);
    ctx.stroke();
    ctx.fillText(String(n), x + 3 * devicePixelRatio, bbox.top + 12 * devicePixelRatio);
  }
  ctx.restore();
}

function syncX(src) {
  if (state.syncing) return;
  state.syncing = true;
  const { min, max } = src.scales.x;
  for (const c of state.charts.values()) {
    if (c.u !== src) c.u.setScale("x", { min, max });
  }
  state.syncing = false;
}

function seriesData(i) {
  const rec = state.rec;
  return [rec.t, rec.live ? rec.y[i] : Array.from(rec.y[i], (v) => (Number.isNaN(v) ? null : v))];
}

function addChart(i) {
  const rec = state.rec;
  const ch = rec.channels[i];
  $("empty")?.remove();

  const el = document.createElement("div");
  el.className = "chart";
  const label = document.createElement("div");
  label.className = "label";
  label.innerHTML = `<span class="n"></span><span class="v"></span>`;
  label.querySelector(".n").textContent = ch.name;
  el.appendChild(label);

  // keep charts in selection order
  const pos = state.selected.indexOf(i);
  const next = state.selected.slice(pos + 1).map((j) => state.charts.get(j)).find(Boolean);
  $("charts").insertBefore(el, next ? next.el : null);

  const axis = { stroke: cssVar("--axis"), grid: { stroke: cssVar("--grid"), width: 1 },
                 ticks: { stroke: cssVar("--grid"), width: 1 } };
  const existing = [...state.charts.values()][0];
  const opts = {
    width: chartWidth(),
    height: CHART_HEIGHT,
    legend: { show: false },
    cursor: { sync: { key: "ssm" }, drag: { x: true, y: false }, points: { size: 6 } },
    scales: { x: { time: false } },
    axes: [
      { ...axis, size: 28 },
      { ...axis, size: 60, label: ch.units, labelSize: 16 },
    ],
    series: [
      {},
      { stroke: colorFor(i), width: 1.25, paths: ch.switch ? uPlot.paths.stepped({ align: 1 }) : undefined },
    ],
    hooks: {
      draw: [drawOverlays],
      setScale: [(u, key) => key === "x" && syncX(u)],
      setSelect: [(u) => { if (rec.live && u.select.width > 0) setFollowing(false); }],
      setCursor: [(u) => {
        const idx = u.cursor.idx;
        if (idx == null && !rec.live) return;      // keep the last position when reviewing
        if (idx !== state.cursorIdx) {
          state.cursorIdx = idx;
          updateValues();
        }
      }],
    },
  };
  const u = new uPlot(opts, seriesData(i), el);
  if (existing) u.setScale("x", { ...existing.u.scales.x });
  else if (rec.live) followLatest(u);
  state.charts.set(i, { u, el, valEl: label.querySelector(".v") });
  updateValues();
}

function zoomTo(min, max) {
  const first = [...state.charts.values()][0];
  if (first) first.u.setScale("x", { min, max });
}

function resetZoom() {
  if (state.rec.live) {
    setFollowing(true);
    return;
  }
  const t = state.rec.t;
  zoomTo(t[0], t[t.length - 1]);
}

function redrawAll() {
  for (const c of state.charts.values()) c.u.redraw(false);
}

// ------------------------------------------------------------------ findings

function showFindings() {
  const fs = state.rec.findings;
  const counts = {};
  for (const f of fs) counts[f.level] = (counts[f.level] || 0) + 1;
  $("counts").textContent = ["problem", "warning", "info", "ok"]
    .filter((lv) => counts[lv]).map((lv) => `${counts[lv]} ${lv}`).join(" · ");

  const list = $("finding-list");
  list.replaceChildren();
  fs.forEach((f) => {
    const li = document.createElement("li");
    li.className = `lv-${f.level}`;
    li.innerHTML = `<div class="title"></div><div class="detail"></div>`;
    li.querySelector(".title").textContent = `${LEVEL_MARK[f.level]}  ${f.title}`;
    li.querySelector(".detail").textContent = f.detail;
    if (f.when) {
      const w = document.createElement("div");
      w.className = "when";
      w.textContent = `When: ${f.when}`;
      li.querySelector(".detail").appendChild(w);
    }
    li.addEventListener("click", () => selectFinding(li, f));
    list.appendChild(li);
  });
  $("findings").hidden = false;
  resizeCharts();
}

function selectFinding(li, f) {
  const was = li.classList.contains("sel");
  for (const el of $("finding-list").children) el.classList.remove("sel");
  if (was) {
    state.highlight = [];
    redrawAll();
    return;
  }
  li.classList.add("sel");
  state.highlight = f.times.filter(([a, b]) => a != null && b != null);
  if (state.highlight.length) {
    if (state.rec.live) setFollowing(false);
    const t = state.rec.t;
    const a = Math.min(...state.highlight.map((r) => r[0]));
    const b = Math.max(...state.highlight.map((r) => r[1]));
    const pad = Math.max((b - a) * 0.15, 5);
    zoomTo(Math.max(t[0], a - pad), Math.min(t[t.length - 1], b + pad));
  }
  redrawAll();
}

function analyseNow() {
  const rec = state.rec;
  if (!rec.live) {
    showFindings();
    return;
  }
  if (rec.t.length < 10) {
    setStatus("Not enough data yet to analyse", true);
    return;
  }
  const n = rec.t.length;
  const t = Float64Array.from(rec.t);
  const y = new Float64Array(rec.channels.length * n);
  rec.y.forEach((col, i) => y.set(col.map((v) => (v == null ? NaN : v)), i * n));
  try {
    const json = state.py.globals.get("analyse_arrays")(new Uint8Array(t.buffer), new Uint8Array(y.buffer),
                                                        JSON.stringify(rec.channels.map((c) => c.name)));
    rec.findings = JSON.parse(json);
  } catch (e) {
    setStatus(`Analysis failed: ${pyError(e)}`, true);
    return;
  }
  state.highlight = [];
  showFindings();
}

// ------------------------------------------------------------------ live mode

function setFollowing(on) {
  state.following = on;
  $("pause").textContent = on ? "Pause" : "Resume";
  $("pause").title = on ? "Freeze the charts to look back (Space) - logging carries on" : "Follow the newest data again (Space)";
  if (on) {
    state.highlight = [];
    followLatest();
  }
}

function followLatest(only = null) {
  const t = state.rec.t;
  if (!t.length) return;
  const max = t[t.length - 1];
  const min = state.windowSec ? Math.max(t[0], max - state.windowSec) : t[0];
  if (only) only.setScale("x", { min, max: Math.max(max, min + 1) });
  else zoomTo(min, Math.max(max, min + 1));
}

let liveRedrawQueued = false;
function scheduleLiveRedraw() {
  if (liveRedrawQueued) return;
  liveRedrawQueued = true;
  setTimeout(() => {
    liveRedrawQueued = false;
    if (!state.rec?.live) return;
    if (state.following) {
      for (const [i, c] of state.charts) c.u.setData(seriesData(i), false);
      followLatest();
    } else if (state.dirty) {
      for (const [i, c] of state.charts) c.u.setData(seriesData(i), false);
      redrawAll();
    }
    state.dirty = false;
    captureTick();
    updateValues();
    updateInfo();
    updateLiveStatus();
  }, 100);
}

function updateLiveStatus() {
  const s = state.session;
  if (!s) return;
  const rec = state.recorder;
  const parts = [s.rate ? `${s.rate.toFixed(1)} samples/s` : ""];
  if (rec) parts.push(`recording ${rec.rows} rows`);
  const t = state.rec?.t;
  if (t?.length && (state.capArmed || state.capPending)) {
    const cs = state.py.globals.get("capture_status")(t[t.length - 1]);
    state.capPending = cs.startsWith("capturing");
    parts.push(`auto-capture ${cs}`);
  }
  if (s.statusText === "Logging") {
    $("cursor-readout").textContent = ["Logging", ...parts].filter(Boolean).join(" · ") +
      (state.following ? "" : " · paused view (Space to resume)");
  }
}

function onLiveSample(t, row) {
  const rec = state.rec;
  if (!rec?.live) return;
  if (rec.t.length >= HISTORY_ROWS) {
    const drop = HISTORY_ROWS / 4;
    rec.t.splice(0, drop);
    rec.y.forEach((col) => col.splice(0, drop));
    if (state.cursorIdx != null) state.cursorIdx = Math.max(0, state.cursorIdx - drop);
  }
  rec.t.push(t);
  row.forEach((v, i) => rec.y[i].push(Number.isFinite(v) ? v : null));
  if (state.capArmed) {
    const reason = state.py.globals.get("capture_update")(t, row);
    if (reason) {
      addMark();
      setStatus(`Auto-capture triggered: ${reason}`);
    }
  }
  if (state.recorder) state.recorder.write(t, row, state.pendingMark);
  state.pendingMark = null;
  state.dirty = true;
  scheduleLiveRedraw();
}

async function ensureDefs() {
  const py = await state.pyReady;
  if (!state.defsReady) {
    state.defsReady = loadDefs(py).catch((e) => { state.defsReady = null; throw e; });
  }
  await state.defsReady;
  return py;
}

async function startLive(port, { demo }) {
  setStatus("Preparing…");
  let py;
  try {
    py = await ensureDefs();
  } catch (e) {
    setStatus(`Could not start: ${e.message}`, true);
    return;
  }
  setStatus("");
  $("findings").hidden = true;
  const session = new LiveSession(py, port, {
    onStatus: (text, error) => {
      session.statusText = text;
      if (text === "Logging") setStatus(demo ? "Demo - replaying the sample recording" : "");
      else setStatus(text, error);
      $("cursor-readout").textContent = text;
    },
    onConnected: ({ ecuId, channels, volts }) => {
      showRecording({
        name: demo ? "Live demo" : "Live", info: `ECU ${ecuId}${demo ? " (demo)" : ""} · OBD ${volts.toFixed(1)} V`,
        channels, t: [], y: channels.map(() => []), marks: [], findings: [], live: true,
      });
      setFollowing(true);
      setupCapture(channels, ecuId);
    },
    onSample: onLiveSample,
  });
  state.session = session;
  document.body.classList.add("connected");
  session.run().finally(() => {
    if (state.session === session) endLive();
  });
}

function endLive() {
  if (state.recorder) stopRecording();
  const due = state.py?.globals.get("capture_due")(Number.MAX_VALUE);
  if (due) saveCapture(...JSON.parse(due));
  state.capArmed = false;
  state.capPending = false;
  updateCapButton();
  state.session = null;
  document.body.classList.remove("connected");
  $("cursor-readout").textContent = "Disconnected. The charts stay open for review.";
  if (state.rec?.live) {
    state.rec.live = false;
    state.rec.name += " (stopped)";
    updateInfo();
    document.body.classList.remove("live");
  }
}

async function connectCar() {
  if (!("serial" in navigator)) return;
  let port;
  try {
    port = await navigator.serial.requestPort();
  } catch {
    return;                       // chooser cancelled
  }
  startLive(port, { demo: false });
}

async function startDemo() {
  setStatus("Preparing demo…");
  try {
    const [py, csv] = await Promise.all([
      ensureDefs(),
      fetch(SAMPLE_URL).then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.text(); }),
    ]);
    const ecu = new DemoEcu(JSON.parse(py.globals.get("param_table")()), parseSampleCsv(csv));
    startLive(new DemoSerialPort(ecu), { demo: true });
  } catch (e) {
    setStatus(`Could not start the demo: ${e.message}`, true);
  }
}

function disconnect() {
  state.session?.stop();
}

function toggleRecording() {
  if (state.recorder) {
    stopRecording();
    return;
  }
  const s = state.session;
  if (!s?.channels) return;
  state.recorder = new CsvRecorder(s.channels, s.ecuId);
  $("record").textContent = "Stop & save";
  $("record").classList.add("recording");
}

function stopRecording() {
  const rec = state.recorder;
  state.recorder = null;
  $("record").textContent = "Record";
  $("record").classList.remove("recording");
  if (!rec || !rec.rows) return;
  download(rec.blob(), rec.filename());
  setStatus(`Saved ${rec.filename()} (${rec.rows} rows) to your downloads`);
}

function download(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
}

// ------------------------------------------------------------------ auto-capture (capture.py)

const CAP_KEY = "ssm-logger.capture";
const POLL_CODES_MS = 10_000;

function loadCapSettings() {
  try { return localStorage.getItem(CAP_KEY) || ""; } catch { return ""; }
}

function storeCapSettings(json) {
  try { localStorage.setItem(CAP_KEY, json); } catch { /* private mode: settings last this session */ }
}

function applyCapSettings(json) {
  const names = JSON.stringify(state.rec.channels.map((c) => c.name));
  const res = JSON.parse(state.py.globals.get("capture_setup")(json, names));
  state.capSettings = res.settings;
  state.capMissing = res.missing;
}

function setupCapture(channels, ecuId) {
  state.capEcuId = ecuId;
  state.capArmed = false;
  state.capPolling = false;
  state.capNextPoll = 0;
  applyCapSettings(loadCapSettings());
  updateCapButton();
}

function updateCapButton() {
  $("autocap").classList.toggle("armed", !!state.capArmed);
  $("autocap").textContent = state.capArmed ? "Auto-capture on" : "Auto-capture";
}

function setArmed(on) {
  state.capArmed = on;
  updateCapButton();
  if (!on) return;
  state.py.globals.get("capture_reset_codes")();     // codes present now are not "new"
  state.capNextPoll = 0;
  if (state.capMissing.length) {
    setStatus(`Auto-capture: this ECU doesn't log ${state.capMissing.join(", ")} - those rules are ignored`);
  }
}

function latestT() {
  const t = state.rec.t;
  return t[t.length - 1];
}

function captureNow() {
  if (!state.rec?.live || !state.rec.t.length) return;
  if (state.py.globals.get("capture_trigger")(latestT(), "manual capture")) {
    addMark();
    setStatus(`Capturing - saves ${state.capSettings.after_s} s from now`);
  } else {
    setStatus("A capture is already in progress");
  }
}

function captureTick() {
  if (!state.rec?.live || !state.rec.t.length || !state.capSettings) return;
  if (state.capArmed && state.capSettings.on_new_code && !state.capPolling && state.session?.op &&
      performance.now() >= state.capNextPoll) {
    state.capPolling = true;
    state.capNextPoll = performance.now() + POLL_CODES_MS;
    state.session.runJob((request) => state.py.globals.get("capture_poll_codes").callPromising(request))
      .then((json) => state.py.globals.get("capture_codes")(latestT(), json))
      .catch((e) => {
        state.capNextPoll = Infinity;          // don't keep failing every 10 s
        setStatus(`Auto-capture can't check for new codes: ${jspiHint(e)}`, true);
      })
      .finally(() => { state.capPolling = false; });
  }
  const due = state.py.globals.get("capture_due")(latestT());
  if (due) saveCapture(...JSON.parse(due));
}

function saveCapture(tTrig, reason) {
  const rec = state.rec, st = state.capSettings;
  const lo = tTrig - st.before_s, hi = tTrig + st.after_s;
  let i0 = rec.t.findIndex((v) => v >= lo);
  if (i0 < 0) i0 = rec.t.length;
  let i1 = i0;
  while (i1 < rec.t.length && rec.t[i1] <= hi) i1++;
  const n = i1 - i0;
  const t = Float64Array.from(rec.t.slice(i0, i1));
  const y = new Float64Array(rec.channels.length * n);
  rec.y.forEach((col, c) => { for (let k = 0; k < n; k++) y[c * n + k] = col[i0 + k] ?? NaN; });
  const when = new Date(Date.now() - (latestT() - tTrig) * 1000);
  const p = (x) => String(x).padStart(2, "0");
  const ymd = `${when.getFullYear()}-${p(when.getMonth() + 1)}-${p(when.getDate())}`;
  const hms = `${p(when.getHours())}:${p(when.getMinutes())}:${p(when.getSeconds())}`;
  const labels = rec.channels.map((c) => (c.units ? `${c.name} (${c.units})` : c.name));
  const text = state.py.globals.get("capture_csv_text")(JSON.stringify(labels), state.capEcuId,
    new Uint8Array(t.buffer), new Uint8Array(y.buffer), tTrig, reason, `${ymd} ${hms}`);
  const name = `ssm_${ymd.replace(/-/g, "")}_${hms.replace(/:/g, "")}_capture.csv`;
  download(new Blob([text], { type: "text/csv" }), name);
  setStatus(`Captured ${reason} - saved ${name} to your downloads`);
}

function ruleRow(r) {
  const tr = document.createElement("tr");
  tr.innerHTML = `<td><input type="checkbox" class="on"></td>
    <td><input type="text" class="ch" list="cap-channels"></td>
    <td><select class="op"><option>&gt;</option><option>&lt;</option></select></td>
    <td><input type="number" class="val" step="any"></td>
    <td><input type="number" class="hold" min="0" step="0.5"></td>
    <td><button class="btn small del" aria-label="Remove rule">✕</button></td>`;
  tr.querySelector(".on").checked = r.enabled;
  tr.querySelector(".ch").value = r.channel;
  tr.querySelector(".op").value = r.op;
  tr.querySelector(".val").value = r.value;
  tr.querySelector(".hold").value = r.hold_s;
  tr.querySelector(".del").addEventListener("click", () => tr.remove());
  return tr;
}

function openCapSettings() {
  const st = state.capSettings;
  $("cap-channels").replaceChildren(...state.rec.channels.map((c) => Object.assign(document.createElement("option"), { value: c.name })));
  $("cap-rules").replaceChildren(...st.rules.map(ruleRow));
  $("cap-code").checked = st.on_new_code;
  $("cap-running").checked = st.running_only;
  $("cap-before").value = st.before_s;
  $("cap-after").value = st.after_s;
  $("cap-dialog").showModal();
}

function saveCapDialog() {
  const num = (el, d) => (Number.isFinite(parseFloat(el.value)) ? parseFloat(el.value) : d);
  const rules = [...$("cap-rules").children].map((tr) => ({
    channel: tr.querySelector(".ch").value.trim(),
    op: tr.querySelector(".op").value,
    value: num(tr.querySelector(".val"), 0),
    hold_s: Math.max(0, num(tr.querySelector(".hold"), 0)),
    enabled: tr.querySelector(".on").checked,
  })).filter((r) => r.channel);
  const json = JSON.stringify({
    rules, on_new_code: $("cap-code").checked, running_only: $("cap-running").checked,
    before_s: Math.min(600, Math.max(1, num($("cap-before"), 30))),
    after_s: Math.min(600, Math.max(1, num($("cap-after"), 30))),
  });
  storeCapSettings(json);
  applyCapSettings(json);
  $("cap-dialog").close();
}

function addMark() {
  const rec = state.rec;
  if (!rec?.live || !rec.t.length) return;
  const n = rec.marks.length + 1;
  rec.marks.push([rec.t[rec.t.length - 1], n]);
  state.pendingMark = n;
  redrawAll();
}

// ------------------------------------------------------------------ fault codes

async function readCodes() {
  const dlg = $("codes-dialog");
  $("codes-text").textContent = "Reading…";
  $("codes-clear").disabled = true;
  $("codes-confirm").hidden = true;
  if (!dlg.open) dlg.showModal();
  if (!state.session?.op) {
    $("codes-text").textContent = "Not connected to the ECU right now - wait for logging to resume, then try again.";
    return;
  }
  try {
    const text = await state.session.runJob((request) => state.py.globals.get("read_codes").callPromising(request));
    $("codes-text").textContent = text;
    $("codes-clear").disabled = false;
  } catch (e) {
    $("codes-text").textContent = jspiHint(e);
  }
}

async function clearCodes() {
  $("codes-confirm").hidden = true;
  $("codes-text").textContent = "Clearing…";
  try {
    const err = await state.session.runJob((request) => state.py.globals.get("clear_codes").callPromising(request));
    if (err) {
      $("codes-text").textContent = `Clear failed: ${err}`;
      return;
    }
  } catch (e) {
    $("codes-text").textContent = jspiHint(e);
    return;
  }
  await readCodes();
  $("codes-text").textContent = `Codes cleared.\n\n${$("codes-text").textContent}`;
}

function jspiHint(e) {
  const msg = pyError(e);
  return /run_sync|JSPI|WebAssembly\.(Suspending|promising)|stack switching/i.test(msg)
    ? "Reading fault codes needs Chrome or Edge 137 or newer.\n\n" + msg
    : `Error: ${msg}`;
}

// ------------------------------------------------------------------ wiring

function resizeCharts() {
  const w = chartWidth();
  for (const c of state.charts.values()) c.u.setSize({ width: w, height: CHART_HEIGHT });
}

function openFile(file) {
  if (!file) return;
  file.text().then((text) => openText(file.name, text));
}

function openUrl(url) {
  setStatus("Downloading…");
  return fetch(url)
    .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.text(); })
    .then((text) => openText(url.split("/").pop(), text))
    .catch((e) => setStatus(`Could not load ${url}: ${e.message}`, true));
}

$("file").addEventListener("change", (e) => {
  openFile(e.target.files[0]);
  e.target.value = "";
});
$("analyse").addEventListener("click", analyseNow);
$("close-findings").addEventListener("click", () => {
  $("findings").hidden = true;
  state.highlight = [];
  redrawAll();
  resizeCharts();
});
$("reset").addEventListener("click", resetZoom);
$("filter").addEventListener("input", (e) => {
  const q = e.target.value.toLowerCase();
  for (const row of $("channels").children) row.hidden = q && !row.dataset.search.includes(q);
});
window.addEventListener("resize", resizeCharts);

$("sample").addEventListener("click", () => openUrl(SAMPLE_URL));
$("demo").addEventListener("click", startDemo);
$("demo-empty").addEventListener("click", startDemo);
$("connect").addEventListener("click", connectCar);
$("connect-empty").addEventListener("click", connectCar);
$("disconnect").addEventListener("click", disconnect);
$("pause").addEventListener("click", () => setFollowing(!state.following));
$("record").addEventListener("click", toggleRecording);
$("mark").addEventListener("click", addMark);
$("codes").addEventListener("click", readCodes);
$("codes-reread").addEventListener("click", readCodes);
$("codes-clear").addEventListener("click", () => { $("codes-confirm").hidden = false; });
$("codes-clear-cancel").addEventListener("click", () => { $("codes-confirm").hidden = true; });
$("codes-clear-yes").addEventListener("click", clearCodes);
$("codes-close").addEventListener("click", () => $("codes-dialog").close());
$("autocap").addEventListener("click", () => setArmed(!state.capArmed));
$("capnow").addEventListener("click", captureNow);
$("capset").addEventListener("click", openCapSettings);
$("cap-add").addEventListener("click", () => $("cap-rules").appendChild(ruleRow({ channel: "", op: ">", value: 0, hold_s: 2, enabled: true })));
$("cap-cancel").addEventListener("click", () => $("cap-dialog").close());
$("cap-save").addEventListener("click", saveCapDialog);
$("window").addEventListener("change", (e) => {
  state.windowSec = Number(e.target.value);
  if (state.following) followLatest();
});

document.addEventListener("keydown", (e) => {
  if (!state.rec?.live || e.target.closest("input, select, textarea, dialog")) return;
  if (e.key === "m" || e.key === "M") addMark();
  else if (e.key === "c" || e.key === "C") captureNow();
  else if (e.key === " ") { e.preventDefault(); setFollowing(!state.following); }
});
window.addEventListener("beforeunload", (e) => {
  if (state.recorder?.rows) e.preventDefault();       // unsaved recording
});

let dragDepth = 0;
window.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; $("drop").hidden = false; });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("drop").hidden = true; } });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  $("drop").hidden = true;
  openFile(e.dataTransfer.files[0]);
});

// Web Serial only exists in Chrome/Edge on desktop; explain instead of hiding the button.
if (!("serial" in navigator)) {
  for (const id of ["connect", "connect-empty"]) {
    $(id).disabled = true;
    $(id).title = "Connecting to the car needs Chrome or Edge on a desktop computer (Web Serial).";
  }
  $("serial-note").hidden = false;
}

state.pyReady = startPython().catch((e) => {
  setStatus(`Python failed to load: ${e.message || e}`, true);
  throw e;
});

// ?url=path/to/log.csv opens a recording straight away (same-origin or CORS-enabled URLs);
// ?sample opens the bundled sample; ?demo starts the live demo.
const params = new URLSearchParams(location.search);
const startUrl = params.get("url") || (params.has("sample") ? SAMPLE_URL : null);
if (startUrl) openUrl(startUrl);
else if (params.has("demo")) startDemo();
