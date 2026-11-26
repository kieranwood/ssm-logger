// Engine timing: one 720° cycle of all four cylinders, built from the logged engine speed, ignition
// timing, injector pulse and (when logged) cam advance, on top of the engine profile's base valve timing.
// Calculated, not measured. 0° = cylinder 1 at TDC on its firing stroke; firing order 1-3-2-4, so each
// cylinder's events come 180° after the one before.

"use strict";

const EngineTiming = (() => {
  const ORDER = [1, 3, 2, 4];
  const STROKES = [["Power", 0, 180], ["Exhaust", 180, 360], ["Intake", 360, 540], ["Compression", 540, 720]];
  const SWEEP_S = 2.4;                                  // seconds per animated cycle (slowed right down)
  const norm = (a) => ((a % 720) + 720) % 720;
  /** [a, b] shifted by off and wrapped into 0-720: one or two pieces. */
  function segs(a, b, off) {
    if (!(b > a)) return [];
    const len = Math.min(720, b - a), x = norm(a + off), y = x + len;
    return y <= 720 ? [[x, y]] : [[x, 720], [0, y - 720]];
  }
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };

  const st = { root: null, view: "rows", playing: false, ang: 0, last: 0, raf: 0, geo: null, rows: [], wheel: null };

  // ---------------------------------------------------------------- geometry
  /** inputs: {rpm, adv, pw, icam, ecam}; base: {ivo, ivc, ir, evo, evc, er}. Crank degrees, 0-720. */
  function geometry(inp, base) {
    const icam = Number.isFinite(inp.icam) ? inp.icam : 0, ecam = Number.isFinite(inp.ecam) ? inp.ecam : 0;
    const IVO = 360 - (base.ivo + icam), IVC = 540 + (base.ivc - icam);       // advance opens and closes earlier
    const EVO = 180 - (base.evo + ecam), EVC = 360 + (base.evc - ecam);
    const rpm = Number.isFinite(inp.rpm) ? inp.rpm : 0;
    const injDeg = Number.isFinite(inp.pw) && rpm > 0 ? inp.pw * rpm * 0.006 : 0;    // ms x deg per ms
    const spark = Number.isFinite(inp.adv) ? 720 - inp.adv : null;
    return {
      IVO, IVC, EVO, EVC, spark, injDeg,
      inj: injDeg > 0 ? [IVO - injDeg, IVO] : null,                 // placed to end as the intake valve opens
      // how far the intake (and exhaust) AVCS could move the window, when not logged: a dashed ghost
      intGhost: !Number.isFinite(inp.icam) && base.ir ? [IVO - base.ir, IVC - base.ir] : null,
      exhGhost: !Number.isFinite(inp.ecam) && base.er ? [EVO + base.er, EVC + base.er] : null,
    };
  }

  // ---------------------------------------------------------------- cylinder rows (HTML)
  function buildRows(box) {
    box.replaceChildren();
    const axis = el("div", "td-axis num");
    for (let a = 0; a <= 720; a += 90) {
      const s = el("span", "", `${a}°`);
      s.style.left = `${a / 7.2}%`;
      axis.append(s);
    }
    const ax = el("div", "td-row-wrap");
    ax.append(el("div", "td-cyl"), axis);
    box.append(ax);
    st.rows = ORDER.map((cyl, k) => {
      const wrap = el("div", "td-row-wrap");
      const lab = el("div", "td-cyl", `<b>Cylinder ${cyl}</b><span class="now"></span>`);
      const lane = el("div", "td-lane");
      for (const [name, a, b] of STROKES) {
        for (const [x, y] of segs(a, b, k * 180)) {
          const s = el("div", `td-stroke s-${name.toLowerCase()}`);
          s.style.left = `${x / 7.2}%`;
          s.style.width = `${(y - x) / 7.2}%`;
          if (y - x > 60) s.append(el("span", "", name));
          lane.append(s);
        }
      }
      const layers = el("div", "td-layers");
      const spark = el("i", "td-spark");
      const crank = el("i", "td-crank");
      lane.append(layers, spark, crank);
      wrap.append(lab, lane);
      box.append(wrap);
      return { off: k * 180, lab, layers, spark, crank };
    });
    box.append(el("p", "td-foot", "0° = cylinder 1 at top dead centre on its firing stroke. Each cylinder fires 180° after the one before it."));
  }

  function drawRows(g) {
    const bar = (cls, a, b, off) => segs(a, b, off).map(([x, y]) => {
      const e = el("i", cls);
      e.style.left = `${x / 7.2}%`;
      e.style.width = `${(y - x) / 7.2}%`;
      return e;
    });
    for (const r of st.rows) {
      const parts = [];
      if (g.exhGhost) parts.push(...bar("td-bar exh ghost", g.exhGhost[0], g.exhGhost[1], r.off));
      parts.push(...bar("td-bar exh", g.EVO, g.EVC, r.off));
      if (g.intGhost) parts.push(...bar("td-bar int ghost", g.intGhost[0], g.intGhost[1], r.off));
      parts.push(...bar("td-bar int", g.IVO, g.IVC, r.off));
      if (g.inj) parts.push(...bar("td-bar inj", g.inj[0], g.inj[1], r.off));
      r.layers.replaceChildren(...parts);
      r.spark.hidden = g.spark == null;
      if (g.spark != null) r.spark.style.left = `${norm(g.spark + r.off) / 7.2}%`;
    }
  }

  // ---------------------------------------------------------------- wheel (SVG)
  const CX = 300, CY = 300, RAD = [252, 206, 160, 114];
  const pt = (r, d) => { const t = ((-90 + d / 2) * Math.PI) / 180; return [CX + r * Math.cos(t), CY + r * Math.sin(t)]; };
  const P = (r, d) => pt(r, d).map((v) => v.toFixed(1)).join(" ");
  function arc(r, a, b) {
    if (b - a >= 719.9) b = a + 719.9;
    return `M${P(r, a)}A${r} ${r} 0 ${(b - a) / 2 > 180 ? 1 : 0} 1 ${P(r, b)}`;
  }

  function buildWheel(box) {
    box.replaceChildren();
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "-30 -16 660 632");
    svg.setAttribute("class", "td-wheel");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Valve, injection and spark events for all four cylinders around one 720° cycle");
    const path = (cls) => { const p = document.createElementNS(NS, "path"); p.setAttribute("class", cls); svg.append(p); return p; };
    const bands = {};
    for (const [name] of STROKES) bands[name] = path(`w-band s-${name.toLowerCase()}`);
    ORDER.forEach((c, k) => {
      for (const [name, a, b] of STROKES) {
        const d = segs(a, b, k * 180).map(([x, y]) => arc(RAD[k], x, y)).join("");
        bands[name].setAttribute("d", (bands[name].getAttribute("d") || "") + d);
      }
    });
    let ticks = "";
    const text = (x, y, t, cls) => {
      const e = document.createElementNS(NS, "text");
      e.setAttribute("x", x); e.setAttribute("y", y); e.setAttribute("class", cls);
      e.textContent = t;
      svg.append(e);
    };
    for (let d = 0; d < 720; d += 90) {
      ticks += `M${P(276, d)}L${P(284, d)}`;
      const [x, y] = pt(296, d);
      text(x.toFixed(1), y.toFixed(1), `${d}°`, "w-lab num");
    }
    path("w-ticks").setAttribute("d", ticks);
    ORDER.forEach((c, k) => { const [x, y] = pt(RAD[k], 710); text(x.toFixed(1), y.toFixed(1), `C${c}`, "w-cyl"); });
    const w = {
      svg, exhG: path("w-exh ghost"), exh: path("w-exh"), intG: path("w-int ghost"), int: path("w-int"), inj: path("w-inj"), spark: path("w-spark"),
    };
    const g = document.createElementNS(NS, "line");
    g.setAttribute("x1", "300"); g.setAttribute("y1", "300"); g.setAttribute("x2", "300"); g.setAttribute("y2", "28");
    g.setAttribute("class", "w-crank");
    svg.append(g);
    w.crank = g;
    const hub = document.createElementNS(NS, "circle");
    hub.setAttribute("cx", "300"); hub.setAttribute("cy", "300"); hub.setAttribute("r", "72"); hub.setAttribute("class", "w-hub");
    svg.append(hub);
    for (const t of svg.querySelectorAll(".w-cyl")) svg.append(t);      // cylinder labels on top of the arcs
    const wrap = el("div", "td-wheel-wrap");
    wrap.append(svg);
    const mid = el("div", "td-wheel-mid", `<b class="num rpm"></b><span>rpm</span><span class="num adv"></span>`);
    wrap.append(mid);
    w.mid = mid;
    box.append(wrap, el("p", "td-foot td-center", "One turn of the wheel = one 720° cycle. Rings from outside in: cylinders 1, 3, 2, 4. Top = cylinder 1 at firing TDC."));
    st.wheel = w;
  }

  function drawWheel(g, inp) {
    const w = st.wheel;
    const ring = (fn) => ORDER.map((c, k) => fn(RAD[k], k * 180)).join("");
    const arcs = (a, b, dr) => ring((r, off) => segs(a, b, off).map(([x, y]) => arc(r + dr, x, y)).join(""));
    w.exh.setAttribute("d", arcs(g.EVO, g.EVC, 11));
    w.exhG.setAttribute("d", g.exhGhost ? arcs(g.exhGhost[0], g.exhGhost[1], 11) : "");
    w.int.setAttribute("d", arcs(g.IVO, g.IVC, -3));
    w.intG.setAttribute("d", g.intGhost ? arcs(g.intGhost[0], g.intGhost[1], -3) : "");
    w.inj.setAttribute("d", g.inj ? arcs(g.inj[0], g.inj[1], -15) : "");
    w.spark.setAttribute("d", g.spark == null ? "" : ring((r, off) => { const s = norm(g.spark + off); return `M${P(r - 20, s)}L${P(r + 20, s)}`; }));
    w.mid.querySelector(".rpm").textContent = Number.isFinite(inp.rpm) ? Math.round(inp.rpm) : "–";
    w.mid.querySelector(".adv").textContent = Number.isFinite(inp.adv) ? `${inp.adv.toFixed(1)}° BTDC` : "";
  }

  // ---------------------------------------------------------------- crank line
  function placeCrank() {
    const a = st.ang;
    for (const r of st.rows) {
      r.crank.style.left = `${a / 7.2}%`;
      const local = norm(a - r.off);
      const [name] = STROKES.find(([, x, y]) => local >= x && local < y);
      const now = r.lab.querySelector(".now");
      now.textContent = `${name} stroke`;
      now.dataset.s = name.toLowerCase();
    }
    if (st.wheel) st.wheel.crank.setAttribute("transform", `rotate(${(a / 2).toFixed(1)} 300 300)`);
  }

  function frame(now) {
    if (!st.playing) return;
    const dt = Math.min(0.1, (now - (st.last || now)) / 1000);
    st.last = now;
    st.ang = norm(st.ang + (720 / SWEEP_S) * dt);
    placeCrank();
    st.raf = requestAnimationFrame(frame);
  }

  function setPlaying(on) {
    st.playing = on;
    cancelAnimationFrame(st.raf);
    st.last = 0;
    if (on) st.raf = requestAnimationFrame(frame);
  }

  // ---------------------------------------------------------------- public
  /** Build into the row and wheel containers (once). */
  function mount(rowsBox, wheelBox) {
    buildRows(rowsBox);
    buildWheel(wheelBox);
    placeCrank();
  }

  /** Redraw for new inputs and base valve timing. */
  function draw(inp, base) {
    const g = geometry(inp, base);
    st.geo = g;
    drawRows(g);
    drawWheel(g, inp);
    return g;
  }

  return { mount, draw, setPlaying, isPlaying: () => st.playing, STROKES };
})();
