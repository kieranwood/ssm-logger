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
  return {
    text: v("--text"), muted: v("--muted"), faint: v("--faint"), accent: v("--accent-fill"), bg: v("--panel"),
    track: v("--g-track"), face: v("--g-face"), faceLine: v("--g-face-line"), tickMinor: v("--g-tick-minor"),
    tickMajor: v("--g-tick-major"), slot: v("--g-slot"),
    // the lit colour, the tinted track and the status-pill background for each zone level
    lit: { none: v("--accent-fill"), cold: v("--g-cold"), warn: v("--g-warn"), danger: v("--g-danger") },
    tint: { none: v("--g-track"), cold: v("--g-cold-tint"), warn: v("--g-warn-tint"), danger: v("--g-danger-tint") },
    pill: { none: v("--accent-tint"), cold: v("--g-cold-pill"), warn: v("--warning-tint"), danger: v("--g-danger-pill") },
    richBg: v("--g-rich-bg"), richText: v("--g-rich-text"), leanBg: v("--g-lean-bg"), leanText: v("--g-lean-text"),
    bandBg: v("--g-band-bg"), bandLine: v("--g-band-line"), dangerText: v("--problem"), targetText: v("--accent"),
  };
}

function zoneAt(spec, v) {
  if (v == null || !Number.isFinite(v)) return null;
  if (spec.lo != null && spec.hi != null) v = Math.min(Math.max(v, spec.lo), spec.hi);   // off the scale
  for (const [a, b, level] of spec.zones) if (v >= a && v <= b) return level;
  return null;
}

/** The words for the zone a value is in ("REDLINE", "LEAN"), or "" when it's normal. */
function zoneStatus(spec, v) {
  const level = zoneAt(spec, v);
  if (!level) return "";
  if (spec.status && spec.status[level]) return spec.status[level];
  if (level === "cold") return "";
  const centre = spec.centre != null ? spec.centre : ((spec.lo ?? 0) + (spec.hi ?? 1)) / 2;
  if (spec.left || spec.right) return (v > centre ? spec.right : spec.left).toUpperCase();
  return v > centre ? "HIGH" : "LOW";
}

const lvl = (level) => level || "none";
const withAlpha = (hex, a) => (/^#[0-9a-f]{6}$/i.test(hex) ? hex + Math.round(a * 255).toString(16).padStart(2, "0") : hex);

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

/** Opacity for a reading in the danger zone: a slow pulse (g.now is the frame time in ms). */
function pulseAlpha(g) {
  return 0.675 + 0.325 * Math.cos((2 * Math.PI * (g.now || 0)) / 1600);
}

const eased = (g) => (g.pos != null && Number.isFinite(g.pos) ? g.pos : g.value);
const hasValue = (g) => g.value != null && Number.isFinite(g.value);
const signed = (v, d, centre) => (centre === 0 && v > 0 ? "+" : "") + fmtNum(v, d);

/** Big value (and units) at the top left of a tile. Returns the y below it. */
function tileValue(ctx, w, h, g, c, level) {
  const fs = Math.max(22, Math.min(h * 0.26, w * 0.2, 72));
  const y = fs + 2;
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  ctx.font = mono(fs);
  ctx.fillStyle = !hasValue(g) ? c.muted : level && level !== "cold" ? c.lit[level] : c.text;
  if (level === "danger") ctx.globalAlpha = pulseAlpha(g);
  const txt = signed(g.value, g.decimals, g.spec.centre);
  ctx.fillText(txt, 4, y);
  ctx.globalAlpha = 1;
  const tw = ctx.measureText(txt).width;
  ctx.font = font(14, 400);
  ctx.fillStyle = c.muted;
  ctx.fillText(g.units, 4 + tw + 8, y);
  return y;
}

/** Bar thickness for the tile's size, as in the design: small 20, medium 28, large 36. */
const barPx = (h) => (h < 130 ? 20 : h < 220 ? 28 : 36);

function drawDial(ctx, w, h, g, c) {
  const { lo, hi, zones, step, scale } = g.spec;
  const v = g.value, p = eased(g), has = hasValue(g);
  const level = zoneAt(g.spec, v);
  const lit = c.lit[lvl(level)];
  const R = Math.max(30, Math.min(w / 2 - 10, (h - 30) / 1.9));   // outer radius of the arc
  const cx = w / 2, cy = R + 10;
  const sw = R >= 150 ? 12 : R >= 105 ? 10 : 8;
  const r = R - sw / 2;                  // arc centre line
  const ro = r - sw / 2 - 6;             // outer end of the ticks
  const f = (x) => Math.max(0, Math.min(1, (x - lo) / (hi - lo)));
  const ang = (x) => ((135 + 270 * f(x)) * Math.PI) / 180;
  // face
  ctx.beginPath();
  ctx.arc(cx, cy, ro + 3, 0, Math.PI * 2);
  ctx.fillStyle = c.face;
  ctx.fill();
  ctx.strokeStyle = c.faceLine;
  ctx.lineWidth = 1;
  ctx.stroke();
  const inner = ro - (R < 110 ? 32 : 40);
  if (inner > 10) {
    ctx.beginPath();
    ctx.arc(cx, cy, inner, 0, Math.PI * 2);
    ctx.strokeStyle = c.track;
    ctx.stroke();
  }
  // track in zone tints, with small gaps between zones
  const segs = [];
  let at = lo;
  for (const [a, b, zl] of [...zones].sort((x, y) => x[0] - y[0])) {
    if (a > at) segs.push([at, a, null]);
    segs.push([Math.max(a, at), b, zl]);
    at = Math.max(at, b);
  }
  if (at < hi) segs.push([at, hi, null]);
  const gap = segs.length > 1 ? 2 / r : 0;
  ctx.lineWidth = sw;
  for (const [a, b, zl] of segs) {
    const a0 = ang(a) + (a > lo ? gap / 2 : 0), a1 = ang(b) - (b < hi ? gap / 2 : 0);
    if (a1 <= a0) continue;
    ctx.beginPath();
    ctx.arc(cx, cy, r, a0, a1);
    ctx.strokeStyle = c.tint[lvl(zl)];
    ctx.stroke();
  }
  // value arc, glowing in the zone's colour
  if (has) {
    const base = g.spec.centre != null ? g.spec.centre : lo;
    const a0 = ang(Math.min(base, p)), a1 = ang(Math.max(base, p));
    if (a1 - a0 > 0.002) {
      ctx.save();
      ctx.shadowColor = withAlpha(lit, 0.55);
      ctx.shadowBlur = 8;
      ctx.beginPath();
      ctx.arc(cx, cy, r, a0, a1);
      ctx.strokeStyle = lit;
      ctx.lineCap = "round";
      ctx.stroke();
      ctx.restore();
    }
  }
  // ticks: minor every half step, labels on the majors
  if (step) {
    const minor = step / 2, n = Math.round((hi - lo) / minor);
    const labelEvery = (hi - lo) / step > 8 ? 2 : 1;
    ctx.font = mono(R < 110 ? 9 : 12, 400);
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= n; i++) {
      const tv = lo + i * minor, a = ang(tv), major = i % 2 === 0;
      const len = major ? 10 : 5;
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(a) * ro, cy + Math.sin(a) * ro);
      ctx.lineTo(cx + Math.cos(a) * (ro - len), cy + Math.sin(a) * (ro - len));
      ctx.strokeStyle = major ? c.tickMajor : c.tickMinor;
      ctx.lineWidth = major ? 2 : 1.2;
      ctx.lineCap = major ? "round" : "butt";
      ctx.stroke();
      if (major && (i / 2) % labelEvery === 0) {
        const rl = ro - (R < 110 ? 18 : 24);
        const z = zoneAt(g.spec, tv);
        ctx.fillStyle = z === "danger" ? c.dangerText : c.muted;
        ctx.fillText(tickLabel(tv, scale, step), cx + Math.cos(a) * rl, cy + Math.sin(a) * rl);
      }
    }
    ctx.lineCap = "butt";
  }
  // peak since the last reset: a small marker outside the arc
  if (g.max != null && Number.isFinite(g.max)) {
    const a = ang(g.max), rt = r + sw / 2 + 2, rb = r + sw / 2 + 10, hw = 4.5;
    const pt = (rr, off) => [cx + rr * Math.cos(a) - off * Math.sin(a), cy + rr * Math.sin(a) + off * Math.cos(a)];
    ctx.beginPath();
    ctx.moveTo(...pt(rt, 0));
    ctx.lineTo(...pt(rb, hw));
    ctx.lineTo(...pt(rb, -hw));
    ctx.closePath();
    ctx.fillStyle = withAlpha(c.text, 0.75);
    ctx.fill();
  }
  // needle: a short tapered pointer across the arc
  if (has) {
    const a = ang(p), nb = ro - 18, nt = r + sw / 2 + 4, nw = R >= 150 ? 3.5 : 2.8;
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(a);
    ctx.shadowColor = withAlpha(c.text, 0.45);
    ctx.shadowBlur = 4;
    ctx.beginPath();
    ctx.moveTo(nb, -nw);
    ctx.lineTo(nt, 0);
    ctx.lineTo(nb, nw);
    ctx.closePath();
    ctx.fillStyle = c.text;
    ctx.fill();
    ctx.restore();
  }
  // readout in the middle: value, units, and the zone's status
  const fs = Math.max(18, R * (R < 100 ? 0.3 : 0.34));
  const status = has ? zoneStatus(g.spec, v) : "";
  const vy = cy + fs * 0.32 - (status ? 10 : 0);
  ctx.textAlign = "center";
  ctx.textBaseline = "alphabetic";
  ctx.font = mono(fs);
  ctx.fillStyle = !has ? c.muted : level === "danger" ? c.lit.danger : level === "cold" ? c.lit.cold : c.text;
  if (level === "danger") ctx.globalAlpha = pulseAlpha(g);
  ctx.fillText(fmtNum(v, g.decimals), cx, vy);
  ctx.globalAlpha = 1;
  ctx.font = font(13, 400);
  ctx.fillStyle = c.muted;
  ctx.fillText(g.units, cx, vy + 20);
  if (status) {
    ctx.font = font(11, 600);
    const tw = ctx.measureText(status).width + 18, ph = 22, py = vy + 30;
    if (level === "danger") ctx.globalAlpha = pulseAlpha(g);
    roundRect(ctx, cx - tw / 2, py, tw, ph, ph / 2);
    ctx.fillStyle = c.pill[lvl(level)];
    ctx.fill();
    ctx.fillStyle = lit;
    ctx.textBaseline = "middle";
    ctx.fillText(status, cx, py + ph / 2 + 0.5);
    ctx.globalAlpha = 1;
  }
  // lowest - highest underneath
  if (g.min != null && Number.isFinite(g.min)) {
    ctx.font = mono(12, 400);
    ctx.fillStyle = c.muted;
    ctx.textBaseline = "alphabetic";
    ctx.textAlign = "center";
    const t = R < 100 ? `${fmtNum(g.min, g.decimals)} – ${fmtNum(g.max, g.decimals)}`
      : `min ${fmtNum(g.min, g.decimals)} · max ${fmtNum(g.max, g.decimals)}`;
    ctx.fillText(t, cx, Math.min(h - 4, cy + R * 0.92 + 14));
  }
}

function minMaxLine(ctx, w, y, g, c) {
  if (g.min == null || !Number.isFinite(g.min)) return;
  ctx.fillStyle = c.track;
  ctx.fillRect(4, y - 22, w - 8, 1);
  ctx.font = mono(12, 400);
  ctx.textBaseline = "alphabetic";
  ctx.textAlign = "left";
  ctx.fillStyle = c.muted;
  ctx.fillText("min ", 4, y);
  let x = 4 + ctx.measureText("min ").width;
  ctx.fillStyle = c.text;
  ctx.fillText(fmtNum(g.min, g.decimals), x, y);
  ctx.textAlign = "right";
  const mx = fmtNum(g.max, g.decimals);
  ctx.fillText(mx, w - 4, y);
  ctx.fillStyle = c.muted;
  ctx.fillText("max ", w - 4 - ctx.measureText(mx).width, y);
}

/** Scale labels under a horizontal bar: [[value or text, x, align, colour]]. */
function scaleLabels(ctx, y, items, c) {
  ctx.font = mono(11, 400);
  ctx.textBaseline = "top";
  for (const [t, x, align, col] of items) {
    ctx.textAlign = align;
    ctx.fillStyle = col || c.muted;
    ctx.fillText(t, x, y);
  }
}

function drawBar(ctx, w, h, g, c) {
  const style = g.spec.style;
  if (g.spec.type === "centre") return style === "target" ? drawTarget(ctx, w, h, g, c) : drawSplit(ctx, w, h, g, c);
  return style === "battery" ? drawBattery(ctx, w, h, g, c) : drawFader(ctx, w, h, g, c);
}

/** Centre-out bar on rich / lean halves (fuel trims), or neutral halves (knock correction). */
function drawSplit(ctx, w, h, g, c) {
  const { lo, hi, step, scale, left, right } = g.spec;
  const centre = g.spec.centre != null ? g.spec.centre : (lo + hi) / 2;
  const v = g.value, p = eased(g), level = zoneAt(g.spec, v);
  tileValue(ctx, w, h, g, c, level);
  const bh = barPx(h), x0 = 4, x1 = w - 4, by = h - 30 - 28 - bh;
  const X = (x) => x0 + Math.max(0, Math.min(1, (x - lo) / (hi - lo))) * (x1 - x0);
  const xc = X(centre);
  ctx.save();
  roundRect(ctx, x0, by, x1 - x0, bh, 8);
  ctx.clip();
  ctx.fillStyle = left ? c.richBg : c.track;
  ctx.fillRect(x0, by, xc - x0, bh);
  ctx.fillStyle = right ? c.leanBg : c.track;
  ctx.fillRect(xc, by, x1 - xc, bh);
  ctx.font = font(10, 600);
  ctx.textBaseline = "middle";
  if (left && bh >= 20) { ctx.textAlign = "left"; ctx.fillStyle = c.richText; ctx.fillText(spaced(left), x0 + 10, by + bh / 2 + 0.5); }
  if (right && bh >= 20) { ctx.textAlign = "right"; ctx.fillStyle = c.leanText; ctx.fillText(spaced(right), x1 - 10, by + bh / 2 + 0.5); }
  ctx.restore();
  if (hasValue(g)) {
    const col = left || right ? (p >= centre ? c.lit.warn : c.lit.cold) : c.lit[lvl(level)];
    const a = X(Math.min(centre, p)), b = X(Math.max(centre, p)), inset = Math.round(bh * 0.29);
    ctx.save();
    ctx.shadowColor = withAlpha(col, 0.4);
    ctx.shadowBlur = 10;
    roundRect(ctx, a, by + inset, Math.max(b - a, 2), bh - 2 * inset, 3);
    ctx.fillStyle = col;
    if (level === "danger") ctx.globalAlpha = pulseAlpha(g);
    ctx.fill();
    ctx.restore();
  }
  ctx.fillStyle = c.text;
  ctx.fillRect(xc - 1, by, 2, bh);
  const sgn = (x) => (x > 0 && centre === 0 ? "+" : x < 0 ? "−" : "") + tickLabel(Math.abs(x), scale, step || (hi - lo) / 4);
  scaleLabels(ctx, by + bh + 8, [[sgn(lo), x0, "left"], [sgn(centre), xc, "center"], [sgn(hi), x1, "right"]], c);
  minMaxLine(ctx, w, h - 4, g, c);
}

const spaced = (s) => s.toUpperCase().split("").join(String.fromCharCode(8202));   // letter-spaced caps

/** A marker on a scale with the good band and the target (air/fuel ratio around stoich). */
function drawTarget(ctx, w, h, g, c) {
  const { lo, hi, step, scale, band, units } = g.spec;
  const target = g.spec.centre != null ? g.spec.centre : (lo + hi) / 2;
  const v = g.value, p = eased(g), level = zoneAt(g.spec, v);
  tileValue(ctx, w, h, g, c, level);
  const bh = barPx(h), x0 = 4, x1 = w - 4, by = h - 30 - 28 - bh;
  const X = (x) => x0 + Math.max(0, Math.min(1, (x - lo) / (hi - lo))) * (x1 - x0);
  roundRect(ctx, x0, by, x1 - x0, bh, 8);
  ctx.fillStyle = c.slot;
  ctx.fill();
  if (band) {
    const a = X(band[0]), b = X(band[1]);
    ctx.fillStyle = c.bandBg;
    ctx.fillRect(a, by, b - a, bh);
    ctx.fillStyle = c.bandLine;
    ctx.fillRect(a, by, 1, bh);
    ctx.fillRect(b - 1, by, 1, bh);
  }
  const xt = X(target);
  ctx.save();
  ctx.shadowColor = c.accent;
  ctx.shadowBlur = 8;
  ctx.fillStyle = c.accent;
  ctx.fillRect(xt - 1, by - 4, 2, bh + 8);
  ctx.restore();
  if (hasValue(g)) {
    const xd = X(p), d = 7;
    ctx.save();
    ctx.translate(xd, by + bh / 2);
    ctx.rotate(Math.PI / 4);
    roundRect(ctx, -d, -d, 2 * d, 2 * d, 2);
    ctx.lineWidth = 4;
    ctx.strokeStyle = c.bg;
    ctx.stroke();
    ctx.fillStyle = c.lit[lvl(level)];
    if (level === "danger") ctx.globalAlpha = pulseAlpha(g);
    ctx.fill();
    ctx.restore();
  }
  const word = /afr/i.test(g.units) ? "stoich" : "target";
  scaleLabels(ctx, by + bh + 8, [[tickLabel(lo, scale, step || 1), x0, "left"],
    [`${word} ${target}`, Math.min(Math.max(xt, x0 + 50), x1 - 50), "center", c.targetText],
    [tickLabel(hi, scale, step || 1), x1, "right"]], c);
  minMaxLine(ctx, w, h - 4, g, c);
}

/** A battery that fills with the voltage, with a line at the mark (12 V). */
function drawBattery(ctx, w, h, g, c) {
  const { lo, hi, step, scale, mark } = g.spec;
  const v = g.value, p = eased(g), level = zoneAt(g.spec, v);
  tileValue(ctx, w, h, g, c, level);
  const bh = h < 130 ? 30 : h < 220 ? 40 : 56, nub = 6;
  const x0 = 4, x1 = w - 4 - nub, by = h - 30 - 28 - bh;
  // body and terminal
  roundRect(ctx, x0 + 1, by + 1, x1 - x0 - 2, bh - 2, 9);
  ctx.lineWidth = 2;
  ctx.strokeStyle = c.faint;
  ctx.stroke();
  roundRect(ctx, x1, by + bh / 2 - 8, nub, 16, 3);
  ctx.fillStyle = c.faint;
  ctx.fill();
  const ix0 = x0 + 6, ix1 = x1 - 6, X = (x) => ix0 + Math.max(0, Math.min(1, (x - lo) / (hi - lo))) * (ix1 - ix0);
  if (hasValue(g)) {
    roundRect(ctx, ix0, by + 6, Math.max(2, X(p) - ix0), bh - 12, 4);
    ctx.fillStyle = c.lit[lvl(level)];
    if (level === "danger") ctx.globalAlpha = pulseAlpha(g);
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.fillStyle = withAlpha("#ffffff", 0.35);
    ctx.fillRect(ix0 + 3, by + 6, Math.max(0, X(p) - ix0 - 6), 1);
  }
  const items = [[tickLabel(lo, scale, step || 1), x0, "left"], [tickLabel(hi, scale, step || 1), x1, "right"]];
  if (mark != null) {
    const xm = X(mark);
    ctx.strokeStyle = c.lit.danger;
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(Math.round(xm) + 0.5, by - 6);
    ctx.lineTo(Math.round(xm) + 0.5, by + bh + 6);
    ctx.stroke();
    ctx.setLineDash([]);
    items.push([`${mark} ${g.units}`, xm, "center", c.dangerText]);
  }
  scaleLabels(ctx, by + bh + 8, items, c);
  minMaxLine(ctx, w, h - 4, g, c);
}

/** A vertical fader (throttle, load, airflow...): the value on the left, the slot on the right. */
function drawFader(ctx, w, h, g, c) {
  const { lo, hi, step, scale, left, right } = g.spec;
  const v = g.value, p = eased(g), level = zoneAt(g.spec, v);
  const top = 4, bottom = h - 30;
  const fw = h < 130 ? 26 : h < 220 ? 34 : 44;
  const fh = Math.max(40, bottom - top - 6);
  const sx = w - 4 - fw, sy = bottom - 6 - fh;
  // the value sits at the bottom left, level with the foot of the slot
  const fs = Math.max(22, Math.min(h * 0.26, (w - fw - 60) * 0.3, 72));
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  ctx.font = mono(fs);
  ctx.fillStyle = !hasValue(g) ? c.muted : level && level !== "cold" ? c.lit[level] : c.text;
  if (level === "danger") ctx.globalAlpha = pulseAlpha(g);
  const txt = fmtNum(v, g.decimals);
  ctx.fillText(txt, 4, sy + fh);
  ctx.globalAlpha = 1;
  const tw = ctx.measureText(txt).width;
  ctx.font = font(14, 400);
  ctx.fillStyle = c.muted;
  ctx.fillText(g.units, 4 + tw + 8, sy + fh);
  // labels beside the slot
  ctx.font = mono(10, 400);
  ctx.textAlign = "right";
  ctx.textBaseline = "top";
  ctx.fillText((right ? right + " " : "") + tickLabel(hi, scale, step || 1), sx - 8, sy);
  ctx.textBaseline = "bottom";
  ctx.fillText((left ? left + " " : "") + tickLabel(lo, scale, step || 1), sx - 8, sy + fh);
  // the slot, recessed
  roundRect(ctx, sx, sy, fw, fh, 9);
  ctx.fillStyle = c.slot;
  ctx.fill();
  ctx.save();
  ctx.clip();
  const grad = ctx.createLinearGradient(0, sy, 0, sy + 8);
  grad.addColorStop(0, "rgba(0,0,0,0.45)");
  grad.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = grad;
  ctx.fillRect(sx, sy, fw, 8);
  ctx.restore();
  const Y = (x) => sy + fh - 4 - Math.max(0, Math.min(1, (x - lo) / (hi - lo))) * (fh - 8);
  if (hasValue(g)) {
    const yt = Y(p);
    roundRect(ctx, sx + 4, yt, fw - 8, Math.max(2, sy + fh - 4 - yt), 5);
    ctx.fillStyle = c.lit[lvl(level)];
    ctx.fill();
    ctx.fillStyle = withAlpha("#ffffff", 0.45);
    ctx.fillRect(sx + 6, yt, fw - 12, 1);
  }
  if (g.max != null && Number.isFinite(g.max)) {
    ctx.fillStyle = withAlpha(c.text, 0.5);
    ctx.fillRect(sx, Y(g.max) - 1, fw, 2);
  }
  minMaxLine(ctx, w, h - 4, g, c);
}

function drawDigital(ctx, w, h, g, c) {
  const level = zoneAt(g.spec, g.value);
  const vy = tileValue(ctx, w, h, g, c, level);
  // trend over the last minute
  const [ts, vs] = g.hist || [[], []];
  const top = vy + 26, bot = h - 34;
  if (ts.length > 1 && bot - top > 16) {
    let mn = Infinity, mx = -Infinity;
    for (const x of vs) if (x != null && Number.isFinite(x)) { mn = Math.min(mn, x); mx = Math.max(mx, x); }
    if (mx > mn || mx === mn) {
      if (mx === mn) { mn -= 1; mx += 1; }
      const pad = (mx - mn) * 0.15; mn -= pad; mx += pad;
      const t1 = g.tNow, t0 = t1 - 60;
      const X = (t) => 4 + ((t - t0) / 60) * (w - 8);
      const Yv = (x) => bot - ((x - mn) / (mx - mn)) * (bot - top);
      ctx.beginPath();
      let pen = false;
      for (let i = 0; i < ts.length; i++) {
        const x = vs[i];
        if (x == null || !Number.isFinite(x)) { pen = false; continue; }
        if (pen) ctx.lineTo(X(ts[i]), Yv(x)); else ctx.moveTo(X(ts[i]), Yv(x));
        pen = true;
      }
      ctx.save();
      ctx.strokeStyle = c.accent;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.shadowColor = withAlpha(c.accent, 0.35);
      ctx.shadowBlur = 6;
      ctx.stroke();
      ctx.restore();
      ctx.font = font(11, 400);
      ctx.fillStyle = c.muted;
      ctx.textAlign = "left";
      ctx.textBaseline = "bottom";
      ctx.fillText("Last minute", 4, top - 4);
    }
  }
  minMaxLine(ctx, w, h - 4, g, c);
}

function drawLamp(ctx, w, h, g, c) {
  const on = g.value != null && Number.isFinite(g.value) && g.value >= 0.5;
  const r = Math.min(w, h) * 0.2;
  const cx = w / 2, cy = h * 0.42;
  ctx.save();
  if (on) { ctx.shadowColor = c.accent; ctx.shadowBlur = r; }
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.fillStyle = on ? c.accent : c.track;
  ctx.fill();
  ctx.restore();
  ctx.font = font(15, 600);
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
