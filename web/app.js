// SSM Log Viewer: open a recording CSV or log live from the car, chart it, and run analyse.py on
// it - all in the browser.
//
// The desktop app's own Python modules (recording.py, analyse.py, defs.py, dtc.py) run with
// Pyodide via web/webapp.py. The cable is driven over Web Serial (openport.js, live.js).
// Nothing is uploaded anywhere.
"use strict";

const PY_MODULES = ["recording.py", "analyse.py", "defs.py", "dtc.py", "capture.py", "guided.py", "report.py",
                    "profiles.py", "gauges.py"];
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
// Chart line colours (dark theme, light theme). A channel keeps its slot, so switching theme
// only changes the shade.
const COLORS_DARK = ["#5B9BFF", "#FF9F43", "#4ADE9A", "#FF6B8A", "#B794FF", "#F5D04A", "#3DD6D0", "#E8B796"];
const COLORS_LIGHT = ["#2F6FDB", "#D9700F", "#13965A", "#D63A5E", "#7B4FD6", "#A88400", "#0E8F8A", "#A8673E"];
const palette = () => (isDark() ? COLORS_DARK : COLORS_LIGHT);
function isDark() {
  const t = document.documentElement.dataset.theme;
  return t === "dark" || (t !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);
}
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
  maxChart: null,      // channel index of the maximised chart, or null
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
  view: "charts",      // or "gauges"
};

// ------------------------------------------------------------------ Python

function setStatus(text, error = false) {
  const el = $("status");
  el.textContent = text;
  el.hidden = !text;
  el.classList.toggle("error", error);
  // green with a pulsing dot while logging from the car
  el.classList.toggle("ok", !error && !!state.session && /^(Logging|Demo · logging)/.test(text));
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
  setupProfiles();
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
  state.maxChart = null;
  document.body.classList.remove("max-chart");
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
  $("tools").disabled = false;
  $("view-gauges").disabled = $("view-charts").disabled = false;
  document.body.classList.add("has-rec");
  document.body.classList.toggle("live", rec.live);
  setupGauges();
  setView(state.view);
}

function updateInfo() {
  const rec = state.rec;
  const n = rec.t.length;
  const dur = n ? rec.t[n - 1] - rec.t[0] : 0;
  $("title").textContent = rec.live ? "Live data" : rec.name;
  $("title").title = rec.name;
  const parts = [rec.info, `${(dur / 60).toFixed(1)} min`, `${n.toLocaleString("en")} rows`,
                 `${rec.channels.length} channels`].filter(Boolean);
  $("info").replaceChildren(...parts.map((t) => Object.assign(document.createElement("span"), { textContent: t })));
}

// Colours follow the order channels were switched on, like the desktop app, and stick to a
// channel until it is switched off.
const colorOf = new Map();          // channel -> palette slot
function colorFor(idx) {
  const P = palette();
  if (!colorOf.has(idx)) {
    const used = new Set(state.selected.filter((j) => j !== idx && colorOf.has(j)).map((j) => colorOf.get(j)));
    let slot = P.findIndex((_, k) => !used.has(k));
    if (slot < 0) slot = colorOf.size % P.length;
    colorOf.set(idx, slot);
  }
  return P[colorOf.get(idx)];
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
    row.setAttribute("role", "button");
    row.tabIndex = 0;
    row.innerHTML = `<span class="box"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
      `stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l5 5 9-10"></path></svg></span>` +
      `<span class="name"><span class="n"></span> <span class="u"></span></span><span class="val num"></span>`;
    row.querySelector(".n").textContent = c.name;
    row.querySelector(".u").textContent = c.units || "";
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleChannel(i); }
    });
    row.addEventListener("click", () => toggleChannel(i));
    list.appendChild(row);
  });
  refreshChannelList();
}

function refreshChannelList() {
  for (const row of $("channels").children) {
    const i = Number(row.dataset.idx);
    const gauges = state.view === "gauges";
    const on = gauges ? gv.sel.includes(state.rec.channels[i].name) : state.selected.includes(i);
    row.classList.toggle("on", on);
    row.setAttribute("aria-pressed", String(on));
    row.style.setProperty("--ch", !on ? "transparent" : gauges ? "var(--accent)" : colorFor(i));
  }
  const n = state.view === "gauges" ? gv.sel.length : state.selected.length;
  $("ch-count").textContent = `${n} ${state.view === "gauges" ? "shown" : "charted"}`;
  updateValues();
}

function toggleChannel(i) {
  if (state.view === "gauges") { toggleGauge(state.rec.channels[i].name); return; }
  const at = state.selected.indexOf(i);
  if (at >= 0) {
    if (state.maxChart === i) maximiseChart(null);
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

// ---- drag a chart's grip (⠿) onto another chart to move it above (top half) or below (bottom half)
let dragChart = null;

function chartDragToMove(el, grip, i) {
  grip.addEventListener("dragstart", (e) => {
    dragChart = i;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", state.rec.channels[i].name);
    e.dataTransfer.setDragImage(el, 40, 12);
    requestAnimationFrame(() => el.classList.add("dragging"));
  });
  grip.addEventListener("dragend", () => {
    dragChart = null;
    el.classList.remove("dragging");
    for (const c of state.charts.values()) c.el.classList.remove("drop-before", "drop-after");
  });
  el.addEventListener("dragover", (e) => {
    if (dragChart == null || dragChart === i) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const r = el.getBoundingClientRect();
    const after = e.clientY > r.top + r.height / 2;
    el.classList.toggle("drop-after", after);
    el.classList.toggle("drop-before", !after);
  });
  el.addEventListener("dragleave", (e) => {
    if (!el.contains(e.relatedTarget)) el.classList.remove("drop-before", "drop-after");
  });
  el.addEventListener("drop", (e) => {
    e.preventDefault();
    const after = el.classList.contains("drop-after");
    el.classList.remove("drop-before", "drop-after");
    if (dragChart != null) moveChart(dragChart, i, after);
  });
}

/** Move chart i to just above (or below) chart target, keeping its zoom and data. */
function moveChart(i, target, after) {
  if (i === target || !state.charts.has(i) || !state.charts.has(target)) return;
  state.selected.splice(state.selected.indexOf(i), 1);
  state.selected.splice(state.selected.indexOf(target) + (after ? 1 : 0), 0, i);
  const t = state.charts.get(target).el;
  $("charts").insertBefore(state.charts.get(i).el, after ? t.nextSibling : t);
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

const nulled = (arr) => Array.from(arr, (v) => (v == null || Number.isNaN(v) ? null : v));

function compareSeries(i) {
  const cmp = state.compare;
  if (!cmp || state.rec.live) return null;
  const y = cmp.byName.get(state.rec.channels[i].name);
  return y ? { t: Array.from(cmp.t, (v) => v + cmp.shift), y: nulled(y) } : null;
}

function seriesData(i) {
  const rec = state.rec;
  if (rec.live) return [rec.t, rec.y[i]];
  const cmp = compareSeries(i);
  if (!cmp) return [rec.t, nulled(rec.y[i])];
  return uPlot.join([[Array.from(rec.t), nulled(rec.y[i])], [cmp.t, cmp.y]]);
}

function addChart(i) {
  const rec = state.rec;
  const ch = rec.channels[i];
  $("empty")?.remove();

  const el = document.createElement("div");
  el.className = "chart";
  const label = document.createElement("div");
  label.className = "label";
  label.innerHTML = `<span class="grip" draggable="true" title="Drag to move this chart">` +
    `<svg width="12" height="16" viewBox="0 0 12 16" fill="currentColor" aria-hidden="true"><circle cx="3" cy="3" r="1.4"></circle>` +
    `<circle cx="9" cy="3" r="1.4"></circle><circle cx="3" cy="8" r="1.4"></circle><circle cx="9" cy="8" r="1.4"></circle>` +
    `<circle cx="3" cy="13" r="1.4"></circle><circle cx="9" cy="13" r="1.4"></circle></svg></span>` +
    `<span class="sw"></span><span class="n"></span><span class="v num"></span>`;
  label.querySelector(".n").textContent = ch.name;
  label.querySelector(".sw").style.background = colorFor(i);
  el.appendChild(label);
  const maxBtn = document.createElement("button");
  maxBtn.className = "chart-max";
  maxBtn.addEventListener("click", () => maximiseChart(state.maxChart === i ? null : i));
  el.appendChild(maxBtn);
  el.classList.toggle("maxed", state.maxChart === i);
  setMaxButton(maxBtn, state.maxChart === i);
  chartDragToMove(el, label.querySelector(".grip"), i);

  // keep charts in selection order
  const pos = state.selected.indexOf(i);
  const next = state.selected.slice(pos + 1).map((j) => state.charts.get(j)).find(Boolean);
  $("charts").insertBefore(el, next ? next.el : null);

  const axis = { stroke: cssVar("--axis"), grid: { stroke: cssVar("--grid"), width: 1 },
                 ticks: { stroke: cssVar("--grid"), width: 1 },
                 font: '11px "JetBrains Mono", ui-monospace, monospace',
                 labelFont: '500 12px "Space Grotesk", system-ui, sans-serif' };
  const existing = [...state.charts.values()][0];
  const opts = {
    width: chartWidth(),
    height: chartHeight(i),
    legend: { show: false },
    cursor: { sync: { key: "ssm" }, drag: { x: true, y: false }, points: { size: 6 } },
    scales: { x: { time: false } },
    axes: [
      { ...axis, size: 28 },
      { ...axis, size: 60, label: ch.units, labelSize: 16 },
    ],
    series: [
      {},
      { stroke: colorFor(i), width: 1.6, paths: ch.switch ? uPlot.paths.stepped({ align: 1 }) : undefined },
      ...(compareSeries(i) ? [{ stroke: cssVar("--compare"), width: 1.1, dash: [6, 4], spanGaps: true }] : []),
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

const LEVELS = {
  problem: { title: "Problems", label: "Problems",
             icon: '<path d="M8.3 2h7.4L22 8.3v7.4L15.7 22H8.3L2 15.7V8.3z"></path><path d="M9 9l6 6"></path><path d="M15 9l-6 6"></path>' },
  warning: { title: "Warnings", label: "Warnings",
             icon: '<path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"></path><path d="M12 9v4"></path><path d="M12 17h.01"></path>' },
  info: { title: "For information", label: "Info",
          icon: '<circle cx="12" cy="12" r="9"></circle><path d="M12 16v-4"></path><path d="M12 8h.01"></path>' },
  ok: { title: "Looks fine", label: "OK", icon: '<circle cx="12" cy="12" r="9"></circle><path d="M8 12l3 3 5-6"></path>' },
};

function showFindings(fs = state.rec.findings, title = "Analysis") {
  $("findings-title").textContent = title;
  const rec = state.rec, n = rec.t.length;
  const meta = [n ? `${((rec.t[n - 1] - rec.t[0]) / 60).toFixed(1)} min` : "", `${n.toLocaleString("en")} rows`];
  $("findings-meta").replaceChildren(...meta.filter(Boolean).map((t) => Object.assign(document.createElement("span"), { textContent: t })));
  const counts = {};
  for (const f of fs) counts[f.level] = (counts[f.level] || 0) + 1;
  $("counts").replaceChildren(...Object.keys(LEVELS).filter((lv) => counts[lv]).map((lv) => {
    const c = document.createElement("span");
    c.className = `count lv-${lv}`;
    c.innerHTML = `<b class="num"></b><span></span>`;
    c.querySelector("b").textContent = counts[lv];
    c.querySelector("span").textContent = LEVELS[lv].label;
    return c;
  }));

  const list = $("finding-list");
  list.replaceChildren();
  for (const lv of Object.keys(LEVELS)) {
    const group = fs.filter((f) => f.level === lv);
    if (!group.length) continue;
    const sec = document.createElement("section");
    sec.className = `f-group lv-${lv}`;
    sec.innerHTML = `<h3>${LEVELS[lv].title} <span class="num"></span></h3><ol></ol>`;
    sec.querySelector("h3 span").textContent = group.length;
    for (const f of group) {
      const li = document.createElement("li");
      li.className = `lv-${f.level}`;
      li.innerHTML = `<button class="f-row" type="button" aria-expanded="false">` +
        `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ` +
        `stroke-linejoin="round" aria-hidden="true" class="f-icon">${LEVELS[lv].icon}</svg>` +
        `<span class="title"></span>` +
        `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" ` +
        `stroke-linejoin="round" aria-hidden="true" class="chev"><path d="M6 9l6 6 6-6"></path></svg></button>` +
        `<div class="detail"></div>`;
      li.querySelector(".title").textContent = f.title;
      li.querySelector(".detail").textContent = f.detail;
      if (f.when) {
        const w = document.createElement("div");
        w.className = "when num";
        w.textContent = `When: ${f.when}`;
        li.querySelector(".detail").appendChild(w);
      }
      li.querySelector(".f-row").addEventListener("click", () => selectFinding(li, f));
      sec.querySelector("ol").append(li);
    }
    list.append(sec);
  }
  $("findings").hidden = false;
  if (state.maxChart != null) maximiseChart(null);     // the findings panel needs the room
  resizeCharts();
}

function selectFinding(li, f) {
  if (state.view === "gauges") setView("charts");
  const was = li.classList.contains("sel");
  for (const el of $("finding-list").querySelectorAll("li")) {
    el.classList.remove("sel");
    el.querySelector(".f-row").setAttribute("aria-expanded", "false");
  }
  if (was) {
    state.highlight = [];
    redrawAll();
    return;
  }
  li.classList.add("sel");
  li.querySelector(".f-row").setAttribute("aria-expanded", "true");
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

function analyseNow(fresh = false) {
  const rec = state.rec;
  if (!rec.live && !fresh) {
    showFindings(rec.findings, "Analysis");
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
  showFindings(rec.findings, "Analysis");
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
    if (state.view === "gauges") {
      drawGauges();
    } else if (state.following) {
      for (const [i, c] of state.charts) c.u.setData(seriesData(i), false);
      if (!scrolling) followLatest();          // scrollFrame moves the window smoothly otherwise
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
    // the header pill keeps showing the rate, unless another message has taken it over
    const pill = $("status").textContent;
    const base = pill.startsWith("Demo · logging") ? "Demo · logging" : pill.startsWith("Logging") ? "Logging" : null;
    if (base) setStatus(s.rate ? `${base} · ${s.rate.toFixed(1)}/s` : base);
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
  if (state.alertsOn) {
    const [fired, active] = JSON.parse(state.py.globals.get("alert_update")(t, row));
    if (fired.length && state.alertSettings.sound) beep();
    if (active.join() !== state.alertActive.join()) {
      state.alertActive = active;
      showAlertBanner();
    }
  }
  if (state.capArmed) {
    const reason = state.py.globals.get("capture_update")(t, row);
    if (reason) {
      addMark();
      setStatus(`Auto-capture triggered: ${reason}`);
    }
  }
  if (state.recorder) state.recorder.write(t, row, state.pendingMark);
  state.pendingMark = null;
  // map the data clock to the wall clock, so the charts can scroll between samples
  const est = t - performance.now() / 1000;
  state.clockOffset = state.clockOffset == null || Math.abs(est - state.clockOffset) > 2
    ? est : state.clockOffset + 0.05 * (est - state.clockOffset);
  state.dirty = true;
  scheduleLiveRedraw();
  startScrolling();
}

// ---- smooth scrolling: while following live data the x window slides every frame, a fixed
// moment behind the newest sample, instead of jumping each time new data is drawn (10 times a second).
const SCROLL_LAG = 0.35;           // seconds behind the newest sample, so the line end is always drawn
let scrolling = false;

function startScrolling() {
  if (scrolling || REDUCED_MOTION.matches) return;
  scrolling = true;
  requestAnimationFrame(scrollFrame);
}

function scrollFrame(now) {
  const rec = state.rec;
  if (!rec?.live || !state.session) { scrolling = false; return; }
  requestAnimationFrame(scrollFrame);
  if (!state.following || state.view !== "charts" || !state.charts.size || state.clockOffset == null) return;
  const t = rec.t;
  if (!t.length) return;
  const edge = Math.min(now / 1000 + state.clockOffset - SCROLL_LAG, t[t.length - 1]);
  const min = state.windowSec ? Math.max(t[0], edge - state.windowSec) : t[0];
  const max = Math.max(edge, min + 1);
  state.syncing = true;               // every chart is set here; no need for syncX to echo it
  for (const c of state.charts.values()) c.u.setScale("x", { min, max });
  state.syncing = false;
}
const REDUCED_MOTION = matchMedia("(prefers-reduced-motion: reduce)");

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
      if (text === "Logging") setStatus(demo ? "Demo · logging" : "Logging");
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
      setupAlerts();
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
  state.alertActive = [];
  showAlertBanner();
  stopReadinessAuto();
  state.session = null;
  document.body.classList.remove("connected");
  $("cursor-readout").textContent = "Disconnected. The charts stay open for review.";
  if (state.rec?.live) {
    state.rec.live = false;
    state.rec.name += " (stopped)";
    gv.idx = Math.max(0, state.rec.t.length - 1);     // gauges stay on the last values
    updateInfo();
    document.body.classList.remove("live");
  }
}

async function connectCar() {
  if (!("serial" in navigator)) return;
  unlockAudio();
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
    const port = new DemoSerialPort(ecu);
    port.onCable = onCableEvent;
    startLive(port, { demo: true });
  } catch (e) {
    setStatus(`Could not start the demo: ${e.message}`, true);
  }
}

function disconnect() {
  state.session?.stop();
}

/** The cable was unplugged or plugged back in: pause logging, then carry on without the port chooser. */
function onCableEvent(type, port) {
  const s = state.session;
  if (!s) return;
  if (type === "disconnect" && port === s.port) {
    s.portLost();
  } else if (type === "connect" && s.cableGone && s.matches(port)) {
    setStatus("Cable is back - reconnecting…");
    s.portFound(port);
  }
}
if ("serial" in navigator) {
  navigator.serial.addEventListener("disconnect", (e) => onCableEvent("disconnect", e.target));
  navigator.serial.addEventListener("connect", (e) => onCableEvent("connect", e.target));
}

function toggleRecording() {
  if (state.recorder) {
    stopRecording();
    return;
  }
  const s = state.session;
  if (!s?.channels) return;
  state.recorder = new CsvRecorder(s.channels, s.ecuId);
  $("record").querySelector(".lbl").textContent = "Stop & save";
  $("record").classList.add("recording");
}

function stopRecording() {
  const rec = state.recorder;
  state.recorder = null;
  $("record").querySelector(".lbl").textContent = "Record";
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

// ------------------------------------------------------------------ alerts (capture.Alerter)

const ALERT_KEY = "ssm-logger.alerts";
let audioCtx = null;

function unlockAudio() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
  } catch { /* no audio */ }
}

function beep() {
  if (!audioCtx) return;
  try {
    const osc = audioCtx.createOscillator(), gain = audioCtx.createGain();
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.15, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.35);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + 0.35);
  } catch { /* ignore */ }
}

function applyAlertSettings(json) {
  const names = JSON.stringify(state.rec.channels.map((c) => c.name));
  const res = JSON.parse(state.py.globals.get("alert_setup")(json, names));
  state.alertSettings = res.settings;
  state.alertActive = [];
  showAlertBanner();
  if (res.missing.length) setStatus(`Alerts: this ECU doesn't log ${res.missing.join(", ")}`);
}

function setupAlerts() {
  let json = "";
  try { json = localStorage.getItem(ALERT_KEY) || ""; } catch { /* private mode */ }
  applyAlertSettings(json);
  setAlertsOn(true);
}

function setAlertsOn(on) {
  state.alertsOn = on;
  $("alerts").classList.toggle("armed", on);
  $("alerts").querySelector(".lbl").textContent = on ? "Alerts on" : "Alerts off";
  if (!on) {
    state.alertActive = [];
    showAlertBanner();
  } else {
    unlockAudio();
  }
}

function showAlertBanner() {
  const active = state.alertActive || [];
  const banner = $("alert-banner");
  banner.hidden = !active.length;
  banner.replaceChildren();
  if (active.length) {
    banner.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ` +
      `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 ` +
      `1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"></path><path d="M12 9v4"></path><path d="M12 17h.01"></path></svg>`;
    const txt = document.createElement("span");
    txt.textContent = active.join("   ·   ");
    banner.append(txt);
  }
  resizeCharts();
}

function openAlertSettings() {
  const st = state.alertSettings;
  $("cap-channels").replaceChildren(...state.rec.channels.map((c) => Object.assign(document.createElement("option"), { value: c.name })));
  $("alert-rules").replaceChildren(...st.rules.map(ruleRow));
  $("alert-sound").checked = st.sound;
  $("alert-running").checked = st.running_only;
  $("alert-dialog").showModal();
}

function readRules(tbody) {
  const num = (el, d) => (Number.isFinite(parseFloat(el.value)) ? parseFloat(el.value) : d);
  return [...tbody.children].map((tr) => ({
    channel: tr.querySelector(".ch").value.trim(),
    op: tr.querySelector(".op").value,
    value: num(tr.querySelector(".val"), 0),
    hold_s: Math.max(0, num(tr.querySelector(".hold"), 0)),
    enabled: tr.querySelector(".on").checked,
  })).filter((r) => r.channel);
}

function saveAlertDialog() {
  const json = JSON.stringify({ rules: readRules($("alert-rules")), sound: $("alert-sound").checked,
                                running_only: $("alert-running").checked });
  try { localStorage.setItem(ALERT_KEY, json); } catch { /* private mode */ }
  applyAlertSettings(json);
  $("alert-dialog").close();
}

// ------------------------------------------------------------------ data slices for Python

function dataBytes(a = -Infinity, b = Infinity) {
  const rec = state.rec;
  let i0 = 0;
  while (i0 < rec.t.length && rec.t[i0] < a) i0++;
  let i1 = i0;
  while (i1 < rec.t.length && rec.t[i1] <= b) i1++;
  const n = i1 - i0;
  const t = Float64Array.from(rec.t.slice(i0, i1));
  const y = new Float64Array(rec.channels.length * n);
  rec.y.forEach((col, c) => { for (let k = 0; k < n; k++) y[c * n + k] = col[i0 + k] ?? NaN; });
  return { n, t: new Uint8Array(t.buffer), y: new Uint8Array(y.buffer),
           names: JSON.stringify(rec.channels.map((c) => c.name)) };
}

function visibleRange() {
  const first = [...state.charts.values()][0];
  return first ? [first.u.scales.x.min, first.u.scales.x.max] : [-Infinity, Infinity];
}

// ------------------------------------------------------------------ guided tests (guided.py)

function openGuided() {
  if (!state.guidedTests) state.guidedTests = JSON.parse(state.py.globals.get("guided_list")());
  const sel = $("guided-test");
  if (!sel.options.length) {
    for (const g of state.guidedTests) sel.add(new Option(g.name, g.key));
  }
  describeGuided();
  if (!$("guided-dialog").open) $("guided-dialog").show();     // non-modal: the charts stay usable
}

function describeGuided() {
  const g = state.guidedTests.find((x) => x.key === $("guided-test").value);
  const d = $("guided-desc");
  d.replaceChildren();
  const p = document.createElement("p");
  p.textContent = g.purpose;
  const ol = document.createElement("ol");
  for (const step of g.steps) ol.appendChild(Object.assign(document.createElement("li"), { textContent: step }));
  const needs = Object.assign(document.createElement("p"), { className: "sub", textContent: `Needs: ${g.needs.join(", ")}` });
  d.append(p, ol, needs);
}

function guidedStart() {
  if (!state.rec?.live || !state.rec.t.length) return;
  state.guidedStart = latestT();
  $("guided-test").disabled = true;
  $("guided-start").textContent = "Restart";
  $("guided-finish").disabled = false;
  clearInterval(state.guidedTimer);
  state.guidedTimer = setInterval(() => {
    $("guided-elapsed").textContent = `running for ${Math.round(latestT() - state.guidedStart)} s`;
  }, 500);
}

function guidedFinish() {
  clearInterval(state.guidedTimer);
  $("guided-test").disabled = false;
  $("guided-start").textContent = "Start";
  $("guided-finish").disabled = true;
  runGuided(state.guidedStart, latestT());
}

function runGuided(a, b) {
  const key = $("guided-test").value;
  const d = dataBytes(a, b);
  const marks = state.rec.marks.map(([t]) => t).filter((t) => t >= a && t <= b);
  const fs = JSON.parse(state.py.globals.get("guided_run")(key, d.t, d.y, d.names, JSON.stringify(marks)));
  $("guided-elapsed").textContent = `checked ${Math.round(b - a)} s of data`;
  const name = state.guidedTests.find((x) => x.key === key).name;
  state.highlight = [];
  showFindings(fs, `Guided test: ${name}`);
  redrawAll();
}

// ------------------------------------------------------------------ readiness tracker

function openReadiness() {
  $("readiness-dialog").show();
  checkReadiness();
}

async function checkReadiness() {
  if (!state.session?.op) {
    $("readiness-msg").textContent = "Not connected to the ECU right now.";
    return;
  }
  if (state.readinessBusy) return;
  state.readinessBusy = true;
  $("readiness-msg").textContent = "Checking…";
  try {
    const r = JSON.parse(await state.session.runJob((req) => state.py.globals.get("read_readiness").callPromising(req)));
    $("readiness-summary").replaceChildren(...r.summary.map(([a, b]) => {
      const tr = document.createElement("tr");
      tr.append(Object.assign(document.createElement("td"), { textContent: a }),
                Object.assign(document.createElement("td"), { textContent: b }));
      return tr;
    }));
    const prev = state.readinessPrev || {};
    const newly = [];
    $("readiness-rows").replaceChildren(...r.monitors.map((m) => {
      const tr = document.createElement("tr");
      const done = prev[m.name] === "NOT complete" && m.since === "complete";
      if (done) newly.push(m.name);
      const cells = [[m.name, ""], [m.since, done ? "done" : m.since === "NOT complete" ? "warn" : ""],
                     [m.drive, m.drive === "NOT complete" ? "warn" : ""], [m.hint, "hint"]];
      for (const [text, cls] of cells) tr.appendChild(Object.assign(document.createElement("td"), { textContent: text, className: cls }));
      return tr;
    }));
    state.readinessPrev = Object.fromEntries(r.monitors.map((m) => [m.name, m.since]));
    const time = new Date().toLocaleTimeString();
    $("readiness-msg").textContent = (newly.length ? `Completed: ${newly.join(", ")}. ` : "") +
      `Checked at ${time}.` + (r.errors.length ? ` (${r.errors.join("; ")})` : "");
  } catch (e) {
    $("readiness-msg").textContent = jspiHint(e);
  } finally {
    state.readinessBusy = false;
  }
}

function stopReadinessAuto() {
  clearInterval(state.readinessTimer);
  state.readinessTimer = null;
  $("readiness-auto").checked = false;
}

// ------------------------------------------------------------------ compare

async function openCompare(file) {
  if (!file) return;
  if (state.rec?.live) {
    setStatus("Compare works on saved recordings - disconnect first, or compare in the desktop app.", true);
    return;
  }
  const py = await state.pyReady;
  py.FS.writeFile("/tmp/compare.csv", await file.text());
  let res;
  try {
    res = py.globals.get("load")("/tmp/compare.csv");
  } catch (e) {
    setStatus(`Could not open ${file.name}: ${pyError(e)}`, true);
    return;
  }
  const [metaJson, tBytes, yBytes] = res.toJs();
  res.destroy();
  const meta = JSON.parse(metaJson);
  const t = bytesToF64(tBytes), flat = bytesToF64(yBytes), n = t.length;
  const byName = new Map(meta.channels.map((c, i) => [c.name, flat.subarray(i * n, (i + 1) * n)]));
  const shift = Math.round((state.rec.t[0] - t[0]) * 10) / 10;
  state.compare = { name: file.name, t, byName, shift };
  $("compare-name").textContent = `Comparing: ${file.name} (dashed)`;
  $("compare-shift").value = shift;
  $("compare-bar").hidden = false;
  rebuildCharts();
}

function closeCompare() {
  state.compare = null;
  $("compare-bar").hidden = true;
  rebuildCharts();
}

function rebuildCharts() {
  const [min, max] = visibleRange();
  for (const c of state.charts.values()) { c.u.destroy(); c.el.remove(); }
  state.charts.clear();
  state.selected.forEach(addChart);
  if (Number.isFinite(min)) zoomTo(min, max);
}

// ------------------------------------------------------------------ report (report.py)

function openReport() {
  const live = !!state.session?.op;
  $("report-ecu").checked = live;
  $("report-ecu").disabled = !live;
  $("report-data").disabled = !state.rec?.t.length;
  $("report-msg").textContent = live ? "" : "ECU readings are only available while connected to the car.";
  $("report-create").disabled = false;
  $("report-dialog").showModal();
}

async function createReport() {
  $("report-create").disabled = true;
  const rec = state.rec;
  const withData = $("report-data").checked && rec?.t.length;
  const d = withData ? dataBytes() : null;
  const now = new Date(), p = (x) => String(x).padStart(2, "0");
  const opts = JSON.stringify({
    title: $("report-title").value.trim(), includeVin: $("report-vin").checked,
    names: rec ? rec.channels.map((c) => c.name) : [],
    units: rec ? Object.fromEntries(rec.channels.map((c) => [c.name, c.units])) : {},
    ecuId: state.session?.ecuId || (rec?.info.match(/ECU (\w+)/) || [])[1] || "",
    source: rec?.live ? "live session" : rec?.name || "",
    generated: `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())} ${p(now.getHours())}:${p(now.getMinutes())}`,
  });
  const build = state.py.globals.get("report_html");
  let page;
  try {
    if ($("report-ecu").checked) {
      $("report-msg").textContent = "Reading the ECU…";
      page = await state.session.runJob((req) => build.callPromising(req, opts, d?.t, d?.y));
    } else {
      page = build(undefined, opts, d?.t, d?.y);
    }
  } catch (e) {
    $("report-msg").textContent = jspiHint(e);
    $("report-create").disabled = false;
    return;
  }
  const name = `report_${now.getFullYear()}${p(now.getMonth() + 1)}${p(now.getDate())}_${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}.html`;
  download(new Blob([page], { type: "text/html" }), name);
  setStatus(`Saved ${name} to your downloads - open it to view, share or print`);
  $("report-dialog").close();
}

// ------------------------------------------------------------------ wiring

function resizeCharts() {
  const w = chartWidth();
  for (const [i, c] of state.charts) c.u.setSize({ width: w, height: chartHeight(i) });
}

/** A maximised chart fills the chart area; the others keep the standard height. */
function chartHeight(i) {
  return state.maxChart === i ? Math.max(CHART_HEIGHT, $("charts").clientHeight - 31) : CHART_HEIGHT;   // 30 px title strip
}

function setMaxButton(btn, maxed) {
  btn.textContent = maxed ? "⤡ Restore" : "⤢";
  btn.title = maxed ? "Restore all charts (Esc)" : "Maximise this chart";
  btn.setAttribute("aria-label", btn.title);
}

/** Show one chart filling the window (i), or all of them again (null). Zoom and cursor carry over. */
function maximiseChart(i) {
  if (i != null && !state.charts.has(i)) i = null;
  state.maxChart = i;
  document.body.classList.toggle("max-chart", i != null);
  for (const [j, c] of state.charts) {
    c.el.classList.toggle("maxed", j === i);
    setMaxButton(c.el.querySelector(".chart-max"), j === i);
  }
  resizeCharts();
  if (i != null) $("charts").scrollTop = 0;
  else state.charts.get(lastMaxed)?.el.scrollIntoView({ block: "nearest" });
  lastMaxed = i;
}
let lastMaxed = null;

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
$("alerts").addEventListener("click", () => setAlertsOn(!state.alertsOn));
$("alert-add").addEventListener("click", () => $("alert-rules").appendChild(ruleRow({ channel: "", op: ">", value: 0, hold_s: 2, enabled: true })));
$("alert-cancel").addEventListener("click", () => $("alert-dialog").close());
$("alert-save").addEventListener("click", saveAlertDialog);
$("tools").addEventListener("click", (e) => {
  e.stopPropagation();
  const m = $("tools-menu");
  m.hidden = !m.hidden;
  $("tools").setAttribute("aria-expanded", String(!m.hidden));
});
document.addEventListener("click", () => { $("tools-menu").hidden = true; $("tools").setAttribute("aria-expanded", "false"); });
$("tools-menu").addEventListener("click", (e) => {
  if (e.target.closest(".menu-radios")) { e.stopPropagation(); return; }   // the engine profile choices
  const act = e.target.closest("button")?.dataset.act;
  $("tools-menu").hidden = true;
  if (act === "guided") openGuided();
  else if (act === "compare") $("compare-file").click();
  else if (act === "report") openReport();
  else if (act === "readiness") openReadiness();
  else if (act === "capset") openCapSettings();
  else if (act === "alertset") openAlertSettings();
});
// ------------------------------------------------------------------ gauge view (gauges.py, gauges.js)

const GAUGE_KEY = "ssm-logger.gauges";
const gv = {
  specs: {}, types: [], sel: [], typeOf: {}, index: new Map(), tiles: new Map(), cache: new Map(),
  idx: 0, resetT: -Infinity, playing: false, playT: 0, lastFrame: 0,
  defaultSel: [], layout: "",      // layout: a saved layout's name, DEFAULT_LAYOUT, or "" (changed since)
  sizeOf: {},                      // per-gauge sizes (Size: Per gauge), name -> key of TILE_SIZES
};
const DEFAULT_LAYOUT = "__default";
// Per-gauge sizes, in cells of the "Per gauge" grid (50 x 45 px plus 10 px gaps): [columns, rows].
const TILE_SIZES = { s: [3, 3, "S"], m: [4, 4, "M"], l: [6, 6, "L"], w: [6, 3, "Wide"] };
const CELL_W = 50, CELL_GAP = 10;
const perGauge = () => $("g-size").value === "per";

function loadGaugePrefs() {
  try { return JSON.parse(localStorage.getItem(GAUGE_KEY) || "{}"); } catch { return {}; }
}

/** Remember the current gauges. custom: the user changed them, so no saved layout matches now.
 *  layouts: the new set of saved layouts, when one was saved or deleted. */
function saveGaugePrefs(custom = true, layouts = undefined) {
  const p = loadGaugePrefs();
  if (custom) gv.layout = "";
  const types = { ...(p.types || {}) };
  for (const n of gv.index.keys()) delete types[n];          // replace this log's overrides
  Object.assign(types, gv.typeOf);
  const sizes = { ...(p.sizes || {}) };
  for (const n of gv.index.keys()) delete sizes[n];
  Object.assign(sizes, gv.sizeOf);
  const next = { selected: gv.sel.slice(), types, size: $("g-size").value, sizes,
                 layouts: layouts ?? p.layouts ?? {}, layout: gv.layout };
  try { localStorage.setItem(GAUGE_KEY, JSON.stringify(next)); } catch { /* private mode */ }
  refreshLayoutSelect();
}

// ---------------------------------------------------------------- saved layouts

function savedLayouts() {
  return loadGaugePrefs().layouts || {};
}

function refreshLayoutSelect() {
  const sel = $("g-layout");
  const names = Object.keys(savedLayouts()).sort((a, b) => a.localeCompare(b));
  sel.replaceChildren();
  if (gv.layout === "") sel.append(new Option("Custom (unsaved)", ""));
  sel.append(new Option("Default", DEFAULT_LAYOUT));
  for (const n of names) sel.append(new Option(n, n));
  sel.value = gv.layout;
  $("g-layout-del").disabled = !names.includes(gv.layout);
}

function applyLayout(name) {
  if (name === DEFAULT_LAYOUT) {
    gv.sel = gv.defaultSel.slice();
    gv.typeOf = {};
    gv.sizeOf = {};
    $("g-size").value = "m";
  } else {
    const L = savedLayouts()[name];
    if (!L) return;
    const sel = (L.selected || []).filter((n) => gv.index.has(n));
    if (!sel.length) {
      setStatus(`None of the channels in the layout “${name}” are in this log.`, true);
      refreshLayoutSelect();
      return;
    }
    gv.sel = sel;
    gv.typeOf = {};
    for (const [n, t] of Object.entries(L.types || {})) if (gv.index.has(n)) gv.typeOf[n] = t;
    gv.sizeOf = pickSizes(L.sizes);
    $("g-size").value = L.size || "m";
    const missing = (L.selected || []).length - sel.length;
    if (missing) setStatus(`Layout “${name}”: ${missing} of its gauges aren't in this log.`);
  }
  gv.layout = name;
  $("gauge-grid").className = `size-${$("g-size").value}`;
  saveGaugePrefs(false);
  buildGaugeTiles();
  refreshChannelList();
}

let layoutAction = null;
function openLayoutDialog(mode) {
  layoutAction = mode;
  const save = mode === "save";
  $("layout-title").textContent = save ? "Save layout" : "Delete layout";
  $("layout-name-row").hidden = !save;
  $("layout-ok").textContent = save ? "Save" : "Delete";
  $("layout-ok").className = save ? "btn primary" : "btn danger";
  if (save) {
    const current = gv.layout && gv.layout !== DEFAULT_LAYOUT ? gv.layout : "";
    $("layout-name").value = current;
    describeLayoutName();
  } else {
    $("layout-msg").textContent = `Delete the layout “${gv.layout}”? The gauges on screen stay as they are.`;
  }
  $("layout-dialog").showModal();
  if (save) $("layout-name").select();
}

function describeLayoutName() {
  const name = $("layout-name").value.trim();
  const reserved = name.toLowerCase() === "default";
  $("layout-ok").disabled = !name || reserved;
  $("layout-msg").textContent = reserved ? "“Default” is the built-in layout; pick another name."
    : name in savedLayouts() ? `Replaces the saved layout “${name}”.`
    : "Saves the gauges shown, their styles and sizes, to use with any recording or live session.";
}

function confirmLayout() {
  const layouts = savedLayouts();
  if (layoutAction === "save") {
    const name = $("layout-name").value.trim();
    if (!name || name.toLowerCase() === "default") return;
    layouts[name] = { selected: gv.sel.slice(), types: { ...gv.typeOf }, size: $("g-size").value,
                      sizes: { ...gv.sizeOf } };
    gv.layout = name;
    saveGaugePrefs(false, layouts);
    setStatus(`Saved the layout “${name}”.`);
  } else {
    const name = gv.layout;
    delete layouts[name];
    gv.layout = "";
    saveGaugePrefs(false, layouts);
    setStatus(`Deleted the layout “${name}”.`);
  }
  $("layout-dialog").close();
}

$("g-layout").addEventListener("change", (e) => { if (e.target.value) applyLayout(e.target.value); });
$("g-layout-save").addEventListener("click", () => openLayoutDialog("save"));
$("g-layout-del").addEventListener("click", () => openLayoutDialog("delete"));
$("g-layout-reset").addEventListener("click", () => applyLayout(DEFAULT_LAYOUT));
$("layout-name").addEventListener("input", describeLayoutName);
$("layout-name").addEventListener("keydown", (e) => { if (e.key === "Enter" && !$("layout-ok").disabled) confirmLayout(); });
$("layout-ok").addEventListener("click", confirmLayout);
$("layout-cancel").addEventListener("click", () => $("layout-dialog").close());

/** Load the gauge specs for the current recording; fresh=false keeps the time position. */
function setupGauges(fresh = true) {
  const rec = state.rec;
  const d = dataBytes();
  const chans = rec.channels.map((c) => [c.name, c.units || "", !!c.switch]);
  const res = JSON.parse(state.py.globals.get("gauge_specs")(JSON.stringify(chans), d.t, d.y, d.names));
  gv.specs = res.specs;
  gv.types = res.types;
  gv.index = new Map(rec.channels.map((c, i) => [c.name, i]));
  gv.defaultSel = res.selection.slice();
  if (fresh) {
    const prefs = loadGaugePrefs();
    gv.layout = !prefs.selected ? DEFAULT_LAYOUT
      : prefs.layout === DEFAULT_LAYOUT || (prefs.layout && prefs.layout in (prefs.layouts || {})) ? prefs.layout : "";
    const saved = (prefs.selected || []).filter((n) => gv.index.has(n));
    gv.sel = saved.length ? saved : res.selection.slice();
    gv.typeOf = {};
    for (const [n, t] of Object.entries(prefs.types || {})) if (gv.index.has(n)) gv.typeOf[n] = t;
    gv.sizeOf = pickSizes(prefs.sizes);
    if (prefs.size) $("g-size").value = prefs.size;
    $("gauge-grid").className = `size-${$("g-size").value}`;
    gv.resetT = -Infinity;
    gv.idx = 0;
    stopPlay();
  }
  gv.cache.clear();
  refreshLayoutSelect();
  buildGaugeTiles();
}

/** The saved per-gauge sizes that apply to this log's channels. */
function pickSizes(saved) {
  const out = {};
  for (const [n, k] of Object.entries(saved || {})) if (gv.index.has(n) && k in TILE_SIZES) out[n] = k;
  return out;
}

/** Per gauge mode: span each tile over its size's cells, never wider than the grid. */
function layoutTiles() {
  const grid = $("gauge-grid");
  const per = perGauge();
  const cols = Math.max(1, Math.floor((grid.clientWidth + CELL_GAP) / (CELL_W + CELL_GAP)));
  for (const [name, tile] of gv.tiles) {
    const [c, r] = TILE_SIZES[gv.sizeOf[name] || "m"];
    tile.el.style.gridColumn = per ? `span ${Math.min(c, cols)}` : "";
    tile.el.style.gridRow = per ? `span ${r}` : "";
    tile.sizeSel.hidden = !per;
  }
}

function buildGaugeTiles() {
  const grid = $("gauge-grid");
  grid.replaceChildren();
  gv.tiles.clear();
  for (const name of gv.sel) {
    const i = gv.index.get(name);
    const ch = state.rec.channels[i];
    const el = document.createElement("div");
    el.className = "gauge";
    el.innerHTML = `<div class="g-head"><span class="g-name"></span><select class="g-type" aria-label="Gauge style"></select>` +
      `<select class="g-tsize" aria-label="Gauge size" title="Size of this gauge: S, M, L or Wide"></select>` +
      `<button class="g-close" aria-label="Remove gauge" title="Remove">✕</button></div><canvas></canvas>`;
    const label = el.querySelector(".g-name");
    label.textContent = name;
    label.title = ch.units ? `${name} (${ch.units})` : name;
    const sel = el.querySelector(".g-type");
    for (const [k, text] of gv.types) sel.append(new Option(k === gv.specs[name].default ? `${text} ·` : text, k));
    sel.value = gv.typeOf[name] || gv.specs[name].default;
    sel.title = "Gauge style (· marks the usual one for this channel)";
    sel.addEventListener("change", () => {
      if (sel.value === gv.specs[name].default) delete gv.typeOf[name];
      else gv.typeOf[name] = sel.value;
      saveGaugePrefs();
      drawGauges();
    });
    const sizeSel = el.querySelector(".g-tsize");
    for (const [k, [, , text]] of Object.entries(TILE_SIZES)) sizeSel.append(new Option(text, k));
    sizeSel.value = gv.sizeOf[name] || "m";
    sizeSel.addEventListener("change", () => {
      if (sizeSel.value === "m") delete gv.sizeOf[name];
      else gv.sizeOf[name] = sizeSel.value;
      saveGaugePrefs();
      layoutTiles();
      requestAnimationFrame(drawGauges);
    });
    el.querySelector(".g-close").addEventListener("click", () => toggleGauge(name));
    dragToMove(el, name);
    grid.append(el);
    gv.tiles.set(name, { i, el, sizeSel, canvas: el.querySelector("canvas") });
  }
  layoutTiles();
  drawGauges();
}

// ---- drag a tile onto another to move it before (left half) or after (right half) that one
let dragName = null, fromControl = false;

function dragToMove(el, name) {
  el.draggable = true;
  el.title = "Drag to move this gauge";
  el.addEventListener("mousedown", (e) => { fromControl = !!e.target.closest("select, button"); });
  el.addEventListener("dragstart", (e) => {
    if (fromControl) { e.preventDefault(); return; }
    dragName = name;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", name);
    requestAnimationFrame(() => el.classList.add("dragging"));
  });
  el.addEventListener("dragend", () => {
    dragName = null;
    el.classList.remove("dragging");
    for (const t of gv.tiles.values()) t.el.classList.remove("drop-before", "drop-after");
  });
  el.addEventListener("dragover", (e) => {
    if (!dragName || dragName === name) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const after = e.clientX > el.getBoundingClientRect().left + el.offsetWidth / 2;
    el.classList.toggle("drop-after", after);
    el.classList.toggle("drop-before", !after);
  });
  el.addEventListener("dragleave", (e) => {
    if (!el.contains(e.relatedTarget)) el.classList.remove("drop-before", "drop-after");
  });
  el.addEventListener("drop", (e) => {
    e.preventDefault();
    const after = el.classList.contains("drop-after");
    el.classList.remove("drop-before", "drop-after");
    if (dragName) moveGauge(dragName, name, after);
  });
}

/** Move gauge name to just before (or after) gauge target. */
function moveGauge(name, target, after) {
  if (name === target || !gv.sel.includes(name) || !gv.sel.includes(target)) return;
  gv.sel.splice(gv.sel.indexOf(name), 1);
  gv.sel.splice(gv.sel.indexOf(target) + (after ? 1 : 0), 0, name);
  saveGaugePrefs();
  buildGaugeTiles();
}

function toggleGauge(name) {
  const at = gv.sel.indexOf(name);
  if (at >= 0) gv.sel.splice(at, 1);
  else gv.sel.push(name);
  saveGaugePrefs();
  buildGaugeTiles();
  refreshChannelList();
}

/** First index with t[k] >= x. */
function bsearch(t, x) {
  let a = 0, b = t.length;
  while (a < b) { const m = (a + b) >> 1; if (t[m] < x) a = m + 1; else b = m; }
  return a;
}

/** Min and max of col[from..to], extended incrementally while from stays put. */
function rangeOf(key, col, t, from, to) {
  let c = gv.cache.get(key);
  if (!c || c.from !== from || c.t0 !== t[from] || to < c.to) {
    c = { from, t0: t[from], to: from - 1, mn: Infinity, mx: -Infinity };
  }
  for (let k = c.to + 1; k <= to; k++) {
    const x = col[k];
    if (x != null && x === x) { if (x < c.mn) c.mn = x; if (x > c.mx) c.mx = x; }
  }
  c.to = to;
  gv.cache.set(key, c);
  return c.mn <= c.mx ? [c.mn, c.mx] : [null, null];
}

function gaugeIdx() {
  const n = state.rec.t.length;
  if (!n) return null;
  return state.rec.live ? n - 1 : Math.min(gv.idx, n - 1);
}

function drawGauges() {
  if (state.view !== "gauges" || !state.rec) return;
  const rec = state.rec, t = rec.t, n = t.length;
  const idx = gaugeIdx();
  if (!rec.live) {
    $("g-slider").max = Math.max(0, n - 1);
    $("g-time").textContent = idx == null ? "" : `${(t[idx] - t[0]).toFixed(1)} s of ${(t[n - 1] - t[0]).toFixed(0)} s`;
  }
  let from = idx == null ? 0 : bsearch(t, gv.resetT);
  if (idx != null && from > idx) from = 0;          // scrubbed back before the reset point
  const colors = gaugeColors();
  for (const [name, tile] of gv.tiles) {
    const ch = rec.channels[tile.i], col = rec.y[tile.i];
    const type = gv.typeOf[name] || gv.specs[name].default;
    let spec = gv.specs[name].by_type[type];
    const value = idx == null ? null : col[idx];
    const [mn, mx] = idx == null ? [null, null] : rangeOf(`${name}|mm`, col, t, from, idx);
    if (type !== "digital" && type !== "lamp" && (spec.lo == null || spec.hi == null)) {
      const [a, b] = n ? rangeOf(`${name}|all`, col, t, 0, n - 1) : [null, null];
      const [lo, hi, step] = niceRange(a ?? 0, b ?? 1);
      spec = { ...spec, lo, hi, step };
    }
    let hist = null;
    if (type === "digital" && idx != null) {
      const k0 = bsearch(t, t[idx] - 60), every = Math.max(1, Math.ceil((idx - k0 + 1) / 240));
      const ts = [], vs = [];
      for (let k = k0; k <= idx; k += every) { ts.push(t[k]); vs.push(col[k]); }
      hist = [ts, vs];
    }
    const g = { spec, value: value ?? null, min: mn, max: mx,
                decimals: spec.decimals ?? Math.min(ch.decimals ?? 2, 2),
                units: ch.units || "", hist, tNow: idx == null ? 0 : t[idx] };
    tile.g = g;
    // needles and bars glide to the new value; the number shown is always the real one
    const has = g.value != null && Number.isFinite(g.value);
    if (!has || tile.pos == null || !Number.isFinite(tile.pos) || REDUCED_MOTION.matches) tile.pos = g.value;
    g.pos = tile.pos;
    drawGauge(tile.canvas, g, colors);
  }
  if (!easing && !REDUCED_MOTION.matches) { easing = true; gv.easeLast = performance.now(); requestAnimationFrame(easeFrame); }
}

const EASE_TAU = 0.12;             // seconds for a needle to cover ~63% of the way to a new value
let easing = false;

function easeFrame(now) {
  if (state.view !== "gauges" || !state.rec) { easing = false; return; }
  const k = 1 - Math.exp(-Math.min(0.1, (now - gv.easeLast) / 1000) / EASE_TAU);
  gv.easeLast = now;
  let moving = false, colors = null;
  for (const tile of gv.tiles.values()) {
    const g = tile.g;
    if (!g || g.value == null || !Number.isFinite(g.value) || tile.pos == null) continue;
    const span = (g.spec.hi ?? 1) - (g.spec.lo ?? 0) || 1;
    if (Math.abs(g.value - tile.pos) < span * 0.0015) { if (tile.pos === g.value) continue; tile.pos = g.value; }
    else { tile.pos += (g.value - tile.pos) * k; moving = true; }
    g.pos = tile.pos;
    drawGauge(tile.canvas, g, colors ??= gaugeColors());
  }
  if (moving) requestAnimationFrame(easeFrame);
  else easing = false;
}

function setView(v) {
  if (!state.rec) return;
  state.view = v;
  const gauges = v === "gauges";
  if (gauges && state.maxChart != null) maximiseChart(null);
  $("charts").hidden = gauges;
  $("gauges").hidden = !gauges;
  $("view-gauges").setAttribute("aria-pressed", String(gauges));
  $("view-charts").setAttribute("aria-pressed", String(!gauges));
  refreshChannelList();
  if (gauges) {
    if (!state.rec.live && state.cursorIdx != null) gv.idx = state.cursorIdx;
    $("g-slider").value = gv.idx;
    layoutTiles();
    drawGauges();
  } else {
    stopPlay();
    resizeCharts();
    if (state.rec.live) {
      for (const [i, c] of state.charts) c.u.setData(seriesData(i), false);
      if (state.following) followLatest(); else redrawAll();
    }
  }
}

function startPlay() {
  const rec = state.rec;
  if (!rec || rec.live || rec.t.length < 2) return;
  if (gv.idx >= rec.t.length - 1) gv.idx = 0;
  gv.playing = true;
  gv.playT = rec.t[gv.idx];
  gv.lastFrame = performance.now();
  $("g-play").textContent = "❚❚ Pause";
  requestAnimationFrame(playFrame);
}

function stopPlay() {
  gv.playing = false;
  $("g-play").textContent = "▶ Play";
}

function playFrame(now) {
  if (!gv.playing || !state.rec || state.rec.live) return;
  const t = state.rec.t;
  gv.playT += ((now - gv.lastFrame) / 1000) * Number($("g-speed").value);
  gv.lastFrame = now;
  gv.idx = Math.min(bsearch(t, gv.playT), t.length - 1);
  if (gv.idx >= t.length - 1) stopPlay();
  $("g-slider").value = gv.idx;
  drawGauges();
  if (gv.playing) requestAnimationFrame(playFrame);
}

$("view-gauges").addEventListener("click", () => setView("gauges"));
$("view-charts").addEventListener("click", () => setView("charts"));
$("g-play").addEventListener("click", () => (gv.playing ? stopPlay() : startPlay()));
$("g-slider").addEventListener("input", (e) => {
  gv.idx = Number(e.target.value);
  if (gv.playing) gv.playT = state.rec.t[gv.idx];
  drawGauges();
});
$("g-reset").addEventListener("click", () => {
  const idx = gaugeIdx();
  gv.resetT = idx == null ? -Infinity : state.rec.t[idx];
  drawGauges();
});
$("g-size").addEventListener("change", (e) => {
  $("gauge-grid").className = `size-${e.target.value}`;
  saveGaugePrefs();
  layoutTiles();
  requestAnimationFrame(drawGauges);
});
// the grid's width changes with the window and with the Analysis panel opening or closing
new ResizeObserver(() => requestAnimationFrame(() => { layoutTiles(); drawGauges(); })).observe($("gauge-grid"));
// ------------------------------------------------------------------ theme

const THEME_KEY = "ssm-logger.theme";
const ICON = (d) => `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ` +
  `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const THEMES = {
  auto: ICON('<circle cx="12" cy="12" r="9"></circle><path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor"></path>'),
  light: ICON('<circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"></path>'),
  dark: ICON('<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"></path>'),
};

function currentTheme() {
  const t = document.documentElement.dataset.theme;
  return t === "light" || t === "dark" ? t : "auto";
}

/** Charts and gauges read their colours when drawn, so redraw them in the new theme. */
function repaintTheme() {
  if (state.charts.size) rebuildCharts();
  requestAnimationFrame(drawGauges);
}

function setTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  try { localStorage.setItem(THEME_KEY, t); } catch { /* private mode: lasts this visit */ }
  updateThemeButton();
  repaintTheme();
}

function updateThemeButton() {
  const t = currentTheme();
  $("theme").innerHTML = THEMES[t];
  $("theme").title = `Theme: ${t === "auto" ? "following the system" : t} - click to switch`;
  $("theme").setAttribute("aria-label", $("theme").title);
}

$("theme").addEventListener("click", () => {
  const order = ["auto", "light", "dark"];
  setTheme(order[(order.indexOf(currentTheme()) + 1) % order.length]);
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (currentTheme() === "auto") repaintTheme();
});
updateThemeButton();

// ------------------------------------------------------------------ engine profile

const PROFILE_KEY = "ssm-logger.profile";

let profileChoice = "auto";

function setupProfiles() {
  const box = $("profile");
  box.replaceChildren();
  let saved = "auto";
  try { saved = localStorage.getItem(PROFILE_KEY) || "auto"; } catch { /* private mode */ }
  const choices = JSON.parse(state.py.globals.get("profile_choices")());
  if (!choices.some(([k]) => k === saved)) saved = "auto";
  for (const [key, label] of choices) {
    // "Naturally aspirated 2.5 L (FB25, EJ25 non-turbo)" -> name and engines on two lines
    const m = label.match(/^(.*?) \((.*)\)$/);
    const row = document.createElement("label");
    row.className = "mi radio";
    row.innerHTML = `<input type="radio" name="engine"><span class="mi-text"><b></b><small></small></span>`;
    row.querySelector("input").value = key;
    row.querySelector("input").checked = key === saved;
    row.querySelector("b").textContent = m ? m[1] : label;
    row.querySelector("small").textContent = m ? m[2] : key === "auto" ? "Detect from the logged channels" : "";
    row.querySelector("input").addEventListener("change", () => chooseProfile(key, m ? m[1] : label));
    box.append(row);
  }
  profileChoice = saved;
  state.py.globals.get("set_profile")(saved);
}

function chooseProfile(key, label) {
  profileChoice = key;
  try { localStorage.setItem(PROFILE_KEY, key); } catch { /* private mode */ }
  state.py.globals.get("set_profile")(key);
  $("tools-menu").hidden = true;
  setStatus(`Engine profile: ${label}`);
  if (state.rec) setupGauges(false);
  // refresh an open analysis so it reflects the new thresholds
  if (state.rec && !$("findings").hidden && $("findings-title").textContent === "Analysis") analyseNow(true);
}


$("compare-file").addEventListener("change", (e) => { openCompare(e.target.files[0]); e.target.value = ""; });
$("compare-close").addEventListener("click", closeCompare);
$("compare-shift").addEventListener("change", (e) => {
  if (!state.compare) return;
  state.compare.shift = parseFloat(e.target.value) || 0;
  rebuildCharts();
});
$("guided-test").addEventListener("change", describeGuided);
$("guided-start").addEventListener("click", guidedStart);
$("guided-finish").addEventListener("click", guidedFinish);
$("guided-visible").addEventListener("click", () => runGuided(...visibleRange()));
$("guided-close").addEventListener("click", () => $("guided-dialog").close());
$("readiness-check").addEventListener("click", checkReadiness);
$("readiness-close").addEventListener("click", () => { stopReadinessAuto(); $("readiness-dialog").close(); });
$("readiness-auto").addEventListener("change", (e) => {
  clearInterval(state.readinessTimer);
  state.readinessTimer = e.target.checked ? setInterval(checkReadiness, 30_000) : null;
});
$("report-cancel").addEventListener("click", () => $("report-dialog").close());
$("report-create").addEventListener("click", createReport);
$("capnow").addEventListener("click", captureNow);
$("cap-add").addEventListener("click", () => $("cap-rules").appendChild(ruleRow({ channel: "", op: ">", value: 0, hold_s: 2, enabled: true })));
$("cap-cancel").addEventListener("click", () => $("cap-dialog").close());
$("cap-save").addEventListener("click", saveCapDialog);
$("window").addEventListener("change", (e) => {
  state.windowSec = Number(e.target.value);
  if (state.following) followLatest();
});

document.addEventListener("keydown", (e) => {
  if (!state.rec || e.target.closest?.("input:not([type=range]), select, textarea, dialog")) return;
  if (e.key === "Escape" && state.maxChart != null) { maximiseChart(null); return; }
  if (e.key === "g" || e.key === "G") { setView(state.view === "gauges" ? "charts" : "gauges"); return; }
  if (!state.rec.live) {
    if (e.key === " " && state.view === "gauges") { e.preventDefault(); gv.playing ? stopPlay() : startPlay(); }
    return;
  }
  if (e.key === "m" || e.key === "M") addMark();
  else if (e.key === "c" || e.key === "C") captureNow();
  else if (e.key === " ") { e.preventDefault(); setFollowing(!state.following); }
});
window.addEventListener("beforeunload", (e) => {
  if (state.recorder?.rows) e.preventDefault();       // unsaved recording
});

// Dropping a file opens it. Only file drags count: dragging a gauge to reorder it is a drag too.
let dragDepth = 0;
const isFileDrag = (e) => !!e.dataTransfer && [...e.dataTransfer.types].includes("Files");
window.addEventListener("dragenter", (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault(); dragDepth++; $("drop").hidden = false;
});
window.addEventListener("dragleave", (e) => {
  if (!isFileDrag(e)) return;
  if (--dragDepth <= 0) { dragDepth = 0; $("drop").hidden = true; }
});
window.addEventListener("dragover", (e) => { if (isFileDrag(e)) e.preventDefault(); });
window.addEventListener("drop", (e) => {
  if (!isFileDrag(e)) return;
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
// The bundled fonts load after the first paint: redraw canvas text (charts, gauges) once they're in.
document.fonts?.ready.then(() => { if (state.rec) repaintTheme(); });
