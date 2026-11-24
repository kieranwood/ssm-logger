// SSM Log Viewer: open a recording CSV, chart it, and run analyse.py on it - all in the browser.
//
// The CSV parsing (recording.py) and the checks (analyse.py) are the desktop app's own Python
// modules, run with Pyodide. Nothing is uploaded anywhere.
"use strict";

const PY_MODULES = ["recording.py", "analyse.py"];
// Deployed site: modules are copied to py/. Local dev (serving the repo root): they sit one level up.
const PY_BASES = ["py/", "../"];
// Deploys stamp ?v=<commit> on app.js; reuse it so the Python modules are refreshed together.
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

const PY_GLUE = `
import json, math
import numpy as np
import analyse, recording

def _clean(v):
    return None if isinstance(v, float) and not math.isfinite(v) else v

def load(path):
    rec = recording.load_csv(path)
    series = {c.name: rec.y[i] for i, c in enumerate(rec.channels)}
    findings = analyse.analyse(rec.t, series)
    meta = {
        "channels": [{"name": c.name, "units": c.units, "decimals": c.decimals, "switch": c.is_switch}
                     for c in rec.channels],
        "marks": [[float(t), int(n)] for t, n in rec.marks],
        "info": rec.info,
        "findings": [{"level": f.level, "title": f.title, "detail": f.detail,
                      "times": [[_clean(float(a)), _clean(float(b))] for a, b in f.times],
                      "when": analyse._fmt_ranges(f.times, limit=12) if f.times else ""}
                     for f in findings],
    }
    return (json.dumps(meta),
            np.ascontiguousarray(rec.t, dtype="<f8").tobytes(),
            np.ascontiguousarray(rec.y, dtype="<f8").tobytes())
`;

const $ = (id) => document.getElementById(id);
const state = {
  py: null,            // Pyodide instance, once ready
  pyReady: null,       // promise
  rec: null,           // {name, channels, t, y: [Float64Array], marks, info, findings}
  selected: [],        // channel indices, in chart order
  charts: new Map(),   // channel index -> {u, el, valEl}
  highlight: [],       // [[t0, t1]] shaded on every chart (from the selected finding)
  syncing: false,
  cursorIdx: null,
};

// ------------------------------------------------------------------ Python

function setStatus(text, error = false) {
  $("status").textContent = text;
  $("status").classList.toggle("error", error);
}

async function fetchModule(name) {
  for (const base of PY_BASES) {
    const r = await fetch(`${base}${name}?v=${VERSION}`, { cache: VERSION === "dev" ? "no-store" : "default" })
      .catch(() => null);
    if (r && r.ok) return r.text();
  }
  throw new Error(`could not load ${name}`);
}

async function startPython() {
  const py = await loadPyodide();
  await py.loadPackage("numpy");
  const sources = await Promise.all(PY_MODULES.map(fetchModule));
  PY_MODULES.forEach((name, i) => py.FS.writeFile(`/home/pyodide/${name}`, sources[i]));
  py.runPython(PY_GLUE);
  state.py = py;
  setStatus("");
  return py;
}

function bytesToF64(u8) {
  const copy = u8.slice();                     // aligned, standalone buffer
  return new Float64Array(copy.buffer);
}

async function openText(name, text) {
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
    setStatus(`Could not open ${name}: ${String(e.message || e).trim().split("\n").pop()}`, true);
    return;
  }
  const [metaJson, tBytes, yBytes] = res.toJs();
  res.destroy();
  const meta = JSON.parse(metaJson);
  const t = bytesToF64(tBytes);
  const flat = bytesToF64(yBytes);
  const n = t.length;
  const y = meta.channels.map((_, i) => flat.subarray(i * n, (i + 1) * n));
  showRecording({ name, t, y, ...meta });
  setStatus("");
}

// ------------------------------------------------------------------ recording UI

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

  const dur = rec.t.length ? rec.t[rec.t.length - 1] - rec.t[0] : 0;
  $("info").textContent = [rec.name, rec.info, `${(dur / 60).toFixed(1)} min`,
    `${rec.t.length} rows, ${rec.channels.length} channels`].filter(Boolean).join("  |  ");
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
  showFindings();
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
  const idx = state.cursorIdx;
  for (const row of $("channels").children) {
    const i = Number(row.dataset.idx);
    const v = idx == null ? null : rec.y[i][idx];
    row.querySelector(".val").textContent = fmt(v, rec.channels[i].decimals);
  }
  for (const [i, c] of state.charts) {
    const v = idx == null ? null : rec.y[i][idx];
    const units = rec.channels[i].units;
    c.valEl.textContent = idx == null ? "" : `${fmt(v, rec.channels[i].decimals)} ${units}`;
  }
  $("cursor-readout").textContent = idx == null
    ? "Drag across a chart to zoom in · double-click to zoom out"
    : `Cursor at ${rec.t[idx].toFixed(2)} s`;
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
  ctx.setLineDash([4, 4]);
  for (const [t] of state.rec.marks) {
    const x = Math.round(toX(t)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(x, bbox.top);
    ctx.lineTo(x, bbox.top + bbox.height);
    ctx.stroke();
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

  const ys = Array.from(rec.y[i], (v) => (Number.isNaN(v) ? null : v));
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
      setCursor: [(u) => {
        if (u.cursor.idx !== state.cursorIdx && u.cursor.idx != null) {
          state.cursorIdx = u.cursor.idx;
          updateValues();
        }
      }],
    },
  };
  const u = new uPlot(opts, [rec.t, ys], el);
  if (existing) u.setScale("x", { ...existing.u.scales.x });
  state.charts.set(i, { u, el, valEl: label.querySelector(".v") });
  updateValues();
}

function zoomTo(min, max) {
  const first = [...state.charts.values()][0];
  if (first) first.u.setScale("x", { min, max });
}

function resetZoom() {
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
    const t = state.rec.t;
    const a = Math.min(...state.highlight.map((r) => r[0]));
    const b = Math.max(...state.highlight.map((r) => r[1]));
    const pad = Math.max((b - a) * 0.15, 5);
    zoomTo(Math.max(t[0], a - pad), Math.min(t[t.length - 1], b + pad));
  }
  redrawAll();
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

$("file").addEventListener("change", (e) => {
  openFile(e.target.files[0]);
  e.target.value = "";
});
$("analyse").addEventListener("click", () => ($("findings").hidden ? showFindings() : null));
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

state.pyReady = startPython().catch((e) => {
  setStatus(`Python failed to load: ${e.message || e}`, true);
  throw e;
});

function openUrl(url) {
  setStatus("Downloading…");
  return fetch(url)
    .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.text(); })
    .then((text) => openText(url.split("/").pop(), text))
    .catch((e) => setStatus(`Could not load ${url}: ${e.message}`, true));
}

const SAMPLE_URL = "samples/p0171-failing-af-sensor.csv";
$("sample").addEventListener("click", () => openUrl(SAMPLE_URL));

// ?url=path/to/log.csv opens a recording straight away (same-origin or CORS-enabled URLs);
// ?sample opens the bundled sample.
const params = new URLSearchParams(location.search);
const startUrl = params.get("url") || (params.has("sample") ? SAMPLE_URL : null);
if (startUrl) openUrl(startUrl);
