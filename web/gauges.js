// Gauge drawing for the gauge view. Which gauge suits a channel, and its range and zones, comes
// from gauges.py (shared with the desktop logger, which draws the same gauges with Qt).
"use strict";

/** Port of gauges.nice_range: round a data range out to tidy numbers -> [lo, hi, step]. */
function niceRange(lo, hi) {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [0, 1, 0.25];
  if (hi - lo < 1e-9) { const pad = Math.abs(hi) * 0.1 || 1; lo -= pad; hi += pad; }
  const raw = (hi - lo) / 5;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const r = (x) => Math.round(x * 1e9) / 1e9;
  return [r(Math.floor(lo / step) * step), r(Math.ceil(hi / step) * step), step];
}

function gaugeColors() {
  const s = getComputedStyle(document.documentElement);
  const v = (n) => s.getPropertyValue(n).trim();
  return { text: v("--text"), muted: v("--muted"), track: v("--g-track"), accent: v("--accent"),
           warn: v("--warning"), danger: v("--problem"), cold: v("--g-cold"), bg: v("--panel") };
}

function zoneAt(spec, v) {
  if (v == null || !Number.isFinite(v)) return null;
  if (spec.lo != null && spec.hi != null) v = Math.min(Math.max(v, spec.lo), spec.hi);   // off the scale
  for (const [a, b, level] of spec.zones) if (v >= a && v <= b) return level;
  return null;
}

function levelColor(c, level) {
  return level === "danger" ? c.danger : level === "warn" ? c.warn : level === "cold" ? c.cold : c.accent;
}

/** Fewer decimals as numbers get bigger: 1699 rpm, 14.2 V, 0.935 V. */
function fmtNum(v, decimals) {
  if (v == null || !Number.isFinite(v)) return "–";
  const a = Math.abs(v);
  return v.toFixed(Math.min(decimals, a >= 1000 ? 0 : a >= 100 ? 1 : decimals));
}

function tickLabel(v, scale, step) {
  const x = v / (scale || 1);
  const d = step / (scale || 1) < 1 ? Math.min(2, Math.ceil(-Math.log10(step / (scale || 1)))) : 0;
  return x.toFixed(d);
}

/**
 * g: {spec (with lo/hi resolved), value, min, max, decimals, units, hist: [[t...], [v...]], tNow}
 */
function drawGauge(canvas, g, c) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.lineCap = "butt";
  const draw = { dial: drawDial, bar: drawBar, centre: drawBar, digital: drawDigital, lamp: drawLamp }[g.spec.type];
  (draw || drawDigital)(ctx, w, h, g, c);
}

// Labels in Space Grotesk, numbers in JetBrains Mono (both bundled in web/fonts).
function font(px, weight = 600) {
  return `${weight} ${Math.round(px)}px "Space Grotesk", ui-sans-serif, system-ui, sans-serif`;
}
function mono(px, weight = 500) {
  return `${weight} ${Math.round(px)}px "JetBrains Mono", ui-monospace, Menlo, monospace`;
}

function drawDial(ctx, w, h, g, c) {
  const { lo, hi, zones, step, scale } = g.spec;
  const r = Math.min(w / 2 - 8, h * 0.56);
  const cx = w / 2, cy = h * 0.55;
  const a0 = (150 * Math.PI) / 180, sweep = (240 * Math.PI) / 180;
  const ang = (v) => a0 + Math.max(0, Math.min(1, (v - lo) / (hi - lo))) * sweep;
  const lw = Math.max(5, r * 0.1);
  const arc = (from, to, color, width = lw, rad = r - lw / 2) => {
    ctx.beginPath();
    ctx.arc(cx, cy, rad, ang(from), ang(to));
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.stroke();
  };
  arc(lo, hi, c.track);
  for (const [a, b, level] of zones) arc(a, b, levelColor(c, level), lw * 0.35, r - lw * 0.175);
  const v = g.value;
  const has = v != null && Number.isFinite(v);
  const col = levelColor(c, zoneAt(g.spec, v));
  if (has) {
    const base = g.spec.centre != null ? g.spec.centre : lo;
    arc(Math.min(base, v), Math.max(base, v), col, lw * 0.55, r - lw * 0.65);
  }
  // ticks and labels
  if (step) {
    ctx.fillStyle = c.muted;
    ctx.strokeStyle = c.muted;
    ctx.font = mono(Math.max(9, r * 0.12));
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const n = Math.round((hi - lo) / step);
    for (let i = 0; i <= n; i++) {
      const tv = lo + i * step, a = ang(tv);
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(a) * (r - lw * 1.2), cy + Math.sin(a) * (r - lw * 1.2));
      ctx.lineTo(cx + Math.cos(a) * (r - lw * 1.9), cy + Math.sin(a) * (r - lw * 1.9));
      ctx.lineWidth = 1.5;
      ctx.stroke();
      const every = n > 8 ? 2 : 1;           // keep labels readable on busy scales
      if (i % every === 0) ctx.fillText(tickLabel(tv, scale, step), cx + Math.cos(a) * (r - lw * 3.1), cy + Math.sin(a) * (r - lw * 3.1));
    }
  }
  // min / max markers
  for (const m of [g.min, g.max]) {
    if (m == null || !Number.isFinite(m)) continue;
    const a = ang(m);
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(a) * (r + 1), cy + Math.sin(a) * (r + 1));
    ctx.lineTo(cx + Math.cos(a) * (r - lw - 2), cy + Math.sin(a) * (r - lw - 2));
    ctx.strokeStyle = c.text;
    ctx.globalAlpha = 0.45;
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.globalAlpha = 1;
  }
  // needle
  if (has) {
    const a = ang(v);
    ctx.beginPath();
    ctx.moveTo(cx - Math.cos(a) * r * 0.08, cy - Math.sin(a) * r * 0.08);
    ctx.lineTo(cx + Math.cos(a) * (r - lw * 0.8), cy + Math.sin(a) * (r - lw * 0.8));
    ctx.strokeStyle = col === c.accent ? c.text : col;
    ctx.lineWidth = Math.max(2, r * 0.03);
    ctx.lineCap = "round";
    ctx.stroke();
    ctx.lineCap = "butt";
  }
  ctx.beginPath();
  ctx.arc(cx, cy, Math.max(4, r * 0.06), 0, Math.PI * 2);
  ctx.fillStyle = c.text;
  ctx.fill();
  // readout
  ctx.textAlign = "center";
  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = has ? levelColor(c, zoneAt(g.spec, v)) === c.accent ? c.text : col : c.muted;
  ctx.font = mono(r * 0.3);
  const below = cy + r * 0.62;
  ctx.fillText(fmtNum(v, g.decimals), cx, below);
  ctx.fillStyle = c.muted;
  ctx.font = font(Math.max(10, r * 0.13), 500);
  ctx.fillText(scale && scale !== 1 ? `${g.units} ×${scale}` : g.units, cx, cy - r * 0.32);
  minMaxLine(ctx, w, h - 4, g, c);
}

function minMaxLine(ctx, w, y, g, c) {
  if (g.min == null || !Number.isFinite(g.min)) return;
  ctx.font = mono(11, 400);
  ctx.fillStyle = c.muted;
  ctx.textBaseline = "alphabetic";
  ctx.textAlign = "left";
  ctx.fillText(`min ${fmtNum(g.min, g.decimals)}`, 8, y);
  ctx.textAlign = "right";
  ctx.fillText(`max ${fmtNum(g.max, g.decimals)}`, w - 8, y);
}

function drawBar(ctx, w, h, g, c) {
  const { lo, hi, zones, step, scale, left, right } = g.spec;
  const centre = g.spec.type === "centre" ? (g.spec.centre != null ? g.spec.centre : (lo + hi) / 2) : lo;
  const v = g.value;
  const has = v != null && Number.isFinite(v);
  const level = zoneAt(g.spec, v);
  const col = levelColor(c, level);
  // value
  ctx.textAlign = "center";
  ctx.textBaseline = "alphabetic";
  ctx.font = mono(Math.min(h * 0.27, w * 0.18));
  ctx.fillStyle = !has ? c.muted : level ? col : c.text;
  const vy = h * 0.36;
  ctx.fillText(fmtNum(v, g.decimals), w / 2, vy);
  ctx.font = font(12, 500);
  ctx.fillStyle = c.muted;
  ctx.fillText(g.units, w / 2, vy + 16);
  // bar
  const x0 = 12, x1 = w - 12, bh = Math.max(10, h * 0.1), by = h * 0.56;
  const X = (x) => x0 + Math.max(0, Math.min(1, (x - lo) / (hi - lo))) * (x1 - x0);
  ctx.fillStyle = c.track;
  roundRect(ctx, x0, by, x1 - x0, bh, bh / 2);
  ctx.fill();
  for (const [a, b, lv] of zones) {
    ctx.fillStyle = levelColor(c, lv);
    ctx.fillRect(X(a), by + bh + 2, X(b) - X(a), 3);
  }
  if (has) {
    ctx.fillStyle = col;
    const a = X(Math.min(centre, v)), b = X(Math.max(centre, v));
    roundRect(ctx, a, by, Math.max(b - a, 2), bh, Math.min(bh / 2, (b - a) / 2));
    ctx.fill();
  }
  if (g.spec.type === "centre") {
    ctx.fillStyle = c.text;
    ctx.fillRect(X(centre) - 1, by - 3, 2, bh + 6);
  }
  for (const m of [g.min, g.max]) {
    if (m == null || !Number.isFinite(m)) continue;
    ctx.fillStyle = c.text;
    ctx.globalAlpha = 0.45;
    ctx.fillRect(X(m) - 1, by - 4, 2, bh + 8);
    ctx.globalAlpha = 1;
  }
  // scale labels
  ctx.font = mono(10, 400);
  ctx.fillStyle = c.muted;
  ctx.textBaseline = "top";
  const ly = by + bh + 8;
  const labels = [[lo, "left"], [hi, "right"]];
  if (g.spec.type === "centre") labels.push([centre, "center"]);
  for (const [lv, align] of labels) {
    ctx.textAlign = align;
    ctx.fillText(tickLabel(lv, scale, step || (hi - lo) / 4), align === "left" ? x0 : align === "right" ? x1 : X(lv), ly);
  }
  if (left || right) {
    ctx.textBaseline = "bottom";
    ctx.textAlign = "left";
    ctx.fillText(left, x0, by - 3);
    ctx.textAlign = "right";
    ctx.fillText(right, x1, by - 3);
  }
  minMaxLine(ctx, w, h - 4, g, c);
}

function drawDigital(ctx, w, h, g, c) {
  const v = g.value;
  const has = v != null && Number.isFinite(v);
  const level = zoneAt(g.spec, v);
  ctx.textAlign = "center";
  ctx.textBaseline = "alphabetic";
  ctx.font = mono(Math.min(h * 0.32, w * 0.2));
  ctx.fillStyle = !has ? c.muted : level ? levelColor(c, level) : c.text;
  const vy = h * 0.4;
  ctx.fillText(fmtNum(v, g.decimals), w / 2, vy);
  ctx.font = font(12, 500);
  ctx.fillStyle = c.muted;
  ctx.fillText(g.units, w / 2, vy + 16);
  // trend over the last minute
  const [ts, vs] = g.hist || [[], []];
  const top = h * 0.58, bot = h - 22;
  if (ts.length > 1) {
    let mn = Infinity, mx = -Infinity;
    for (const x of vs) if (x != null && Number.isFinite(x)) { mn = Math.min(mn, x); mx = Math.max(mx, x); }
    if (mx > mn || mx === mn) {
      if (mx === mn) { mn -= 1; mx += 1; }
      const t1 = g.tNow, t0 = t1 - 60;
      const X = (t) => 8 + ((t - t0) / 60) * (w - 16);
      const Y = (x) => bot - ((x - mn) / (mx - mn)) * (bot - top);
      ctx.beginPath();
      let pen = false;
      for (let i = 0; i < ts.length; i++) {
        const x = vs[i];
        if (x == null || !Number.isFinite(x)) { pen = false; continue; }
        if (pen) ctx.lineTo(X(ts[i]), Y(x)); else ctx.moveTo(X(ts[i]), Y(x));
        pen = true;
      }
      ctx.strokeStyle = c.accent;
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.font = mono(9, 400);
      ctx.fillStyle = c.muted;
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText("last minute", 8, top - 12);
    }
  }
  minMaxLine(ctx, w, h - 4, g, c);
}

function drawLamp(ctx, w, h, g, c) {
  const on = g.value != null && Number.isFinite(g.value) && g.value >= 0.5;
  const r = Math.min(w, h) * 0.2;
  const cx = w / 2, cy = h * 0.42;
  if (on) {
    ctx.beginPath();
    ctx.arc(cx, cy, r * 1.45, 0, Math.PI * 2);
    ctx.fillStyle = c.accent;
    ctx.globalAlpha = 0.18;
    ctx.fill();
    ctx.globalAlpha = 1;
  }
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.fillStyle = on ? c.accent : c.track;
  ctx.fill();
  ctx.font = font(15, 650);
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.fillStyle = on ? c.text : c.muted;
  ctx.fillText(g.value == null || !Number.isFinite(g.value) ? "–" : on ? "ON" : "off", cx, cy + r * 1.5);
}

function roundRect(ctx, x, y, w, h, r) {
  r = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
