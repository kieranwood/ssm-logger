// Boxer engine animation: the four cylinders seen from above (front at the top), pistons, valves,
// cams, spark and injection moving through the 1-3-2-4 cycle. A schematic driven by the same calculated
// timing as the Engine timing view (timing.js); slowed right down. Cylinders whose roughness monitor
// (the ECU's misfire count) reads above zero at the current moment are drawn without a burn.

"use strict";

const BoxerEngine = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const R = 40, L = 120, CX = 600;                     // crank throw, rod length, crank centre (svg px)
  // cylinder, crank offset (its firing TDC), y, bank. Odd cylinders on the right bank.
  const CYL = [{ n: 1, off: 0, y: 220, right: true }, { n: 2, off: 360, y: 300, right: false },
               { n: 3, off: 180, y: 460, right: true }, { n: 4, off: 540, y: 540, right: false }];
  const SPEEDS = { slow: 50, medium: 125, fast: 275 };   // crank degrees per second on screen
  const norm = (a) => ((a % 720) + 720) % 720;
  const stroke = (l) => (l < 180 ? "Power" : l < 360 ? "Exhaust" : l < 540 ? "Intake" : "Compression");
  /** How far l is through the window [a, b] (0-1), or -1 outside it. */
  const inWin = (l, a, b) => { const x = norm(l - a), w = b - a; return w > 0 && x <= w ? x / w : -1; };

  const st = { svg: null, cyl: [], pins: null, ang: 0, playing: false, speed: "slow", raf: 0, last: 0, geo: null, rough: {}, onAngle: null };

  function node(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    if (parent) parent.append(e);
    return e;
  }
  const set = (e, attrs) => { for (const k in attrs) e.setAttribute(k, attrs[k]); };

  function build(box) {
    box.replaceChildren();
    const svg = node("svg", { viewBox: "0 0 1200 760", class: "bx-svg", role: "img",
      "aria-label": "Schematic of a boxer engine seen from above, front at the top, with the four cylinders moving through their cycle" }, box);
    node("rect", { x: 250, y: 120, width: 700, height: 520, rx: 26, class: "bx-block" }, svg);
    node("rect", { x: 588, y: 110, width: 24, height: 560, rx: 8, class: "bx-crank" }, svg);
    for (const y of [150, 370, 610]) node("rect", { x: 578, y, width: 44, height: 18, rx: 5, class: "bx-main" }, svg);
    node("path", { d: "M600 95 L600 60 M590 75 L600 60 L610 75", class: "bx-arrow" }, svg);
    const text = (x, y, t, cls) => { const e = node("text", { x, y, class: cls }, svg); e.textContent = t; return e; };
    text(600, 48, "FRONT", "bx-t");
    text(1040, 100, "RIGHT BANK", "bx-t bank");
    text(160, 100, "LEFT BANK", "bx-t bank");
    text(600, 700, "crankshaft", "bx-t small");
    const CAM = "M-22 0 C-18 -9 -7 -13 0 -13 A13 13 0 1 1 0 13 C-7 13 -18 9 -22 0 Z";
    st.cyl = CYL.map((d) => {
      const g = node("g", { transform: d.right ? "" : "translate(1200 0) scale(-1 1)" }, svg);
      const y = d.y;
      const c = { d };
      c.wall = node("rect", { x: 648, y: y - 48, width: 154, height: 96, class: "bx-bore" }, g);
      c.gas = node("rect", { y: y - 44, height: 88 }, g);
      node("rect", { x: 802, y: y - 62, width: 84, height: 124, rx: 8, class: "bx-head" }, g);
      c.rod = node("line", { y1: y, y2: y, class: "bx-rod" }, g);
      c.piston = node("rect", { y: y - 44, width: 54, height: 88, rx: 5, class: "bx-piston" }, g);
      c.ring1 = node("line", { y1: y - 44, y2: y + 44, class: "bx-ring" }, g);
      c.ring2 = node("line", { y1: y - 44, y2: y + 44, class: "bx-ring" }, g);
      c.wrist = node("circle", { cy: y, r: 7, class: "bx-pin" }, g);
      c.crankpin = node("circle", { cy: y, r: 11, class: "bx-pin big" }, g);
      c.ivStem = node("line", { y1: y - 24, y2: y - 24, x2: 920, class: "bx-valve int" }, g);
      c.ivHead = node("line", { y1: y - 37, y2: y - 11, class: "bx-valve int head" }, g);
      c.evStem = node("line", { y1: y + 24, y2: y + 24, x2: 920, class: "bx-valve exh" }, g);
      c.evHead = node("line", { y1: y + 11, y2: y + 37, class: "bx-valve exh head" }, g);
      c.icam = node("g", {}, g);
      node("path", { d: CAM, class: "bx-cam int" }, c.icam);
      node("circle", { r: 3, class: "bx-cam-c int" }, c.icam);
      c.ecam = node("g", {}, g);
      node("path", { d: CAM, class: "bx-cam exh" }, c.ecam);
      node("circle", { r: 3, class: "bx-cam-c exh" }, c.ecam);
      node("rect", { x: 812, y: y - 5, width: 62, height: 10, rx: 3, class: "bx-plug" }, g);
      node("rect", { x: 874, y: y - 7, width: 14, height: 14, rx: 3, class: "bx-plug-cap" }, g);
      c.spark = node("circle", { cx: 806, cy: y, class: "bx-spark" }, g);
      node("rect", { x: 838, y: y - 58, width: 40, height: 9, rx: 3, class: "bx-injector", transform: `rotate(-28 858 ${y - 54})` }, g);
      c.spray = node("path", { d: `M842 ${y - 48} L812 ${y - 30} L822 ${y - 22} Z`, class: "bx-spray" }, g);
      // label: number and stroke, outside the mirrored group so the text reads the right way round
      const px = d.right ? 845 : 355, ly = y - 92;
      node("circle", { cx: px, cy: ly, r: 15, class: "bx-num" }, svg);
      c.numT = text(px, ly + 5, String(d.n), "bx-num-t");
      c.label = text(px + (d.right ? 24 : -24), ly + 4, "", `bx-stroke ${d.right ? "r" : "l"}`);
      return c;
    });
    st.svg = svg;
  }

  function buildPins(box) {
    box.replaceChildren();
    const svg = node("svg", { viewBox: "0 0 200 200", class: "bx-pins", role: "img", "aria-label": "Crankshaft seen from the front" }, box);
    node("circle", { cx: 100, cy: 100, r: 78, class: "bx-pins-ring" }, svg);
    node("circle", { cx: 100, cy: 100, r: 16, class: "bx-main" }, svg);
    st.pins = [1, 2, 3, 4].map((n) => {
      const arm = node("line", { x1: 100, y1: 100, class: "bx-arm" }, svg);
      const dot = node("circle", { r: 13, class: "bx-pin big" }, svg);
      const t = node("text", { class: "bx-pin-t" }, svg);
      t.textContent = n;
      return { n, arm, dot, t };
    });
  }

  function draw() {
    if (!st.svg || !st.geo) return;
    const g = st.geo, a = st.ang;
    const imid = (g.IVO + g.IVC) / 2, emid = (g.EVO + g.EVC) / 2;
    for (const c of st.cyl) {
      const { d } = c, y = d.y, l = norm(a - d.off), th = (l * Math.PI) / 180;
      const pin = CX + R * Math.cos(th);
      const xp = pin + Math.sqrt(L * L - R * R * Math.sin(th) ** 2);
      const crown = xp + 32;
      set(c.rod, { x1: pin.toFixed(1), x2: xp.toFixed(1) });
      set(c.piston, { x: (xp - 22).toFixed(1) });
      set(c.ring1, { x1: (crown - 8).toFixed(1), x2: (crown - 8).toFixed(1) });
      set(c.ring2, { x1: (crown - 15).toFixed(1), x2: (crown - 15).toFixed(1) });
      set(c.wrist, { cx: xp.toFixed(1) });
      set(c.crankpin, { cx: pin.toFixed(1) });
      const il = inWin(l, g.IVO, g.IVC), el = inWin(l, g.EVO, g.EVC);
      const ilift = il >= 0 ? Math.sin(il * Math.PI) * 16 : 0, elift = el >= 0 ? Math.sin(el * Math.PI) * 16 : 0;
      set(c.ivStem, { x1: (806 - ilift).toFixed(1) });
      set(c.ivHead, { x1: (806 - ilift).toFixed(1), x2: (806 - ilift).toFixed(1) });
      set(c.evStem, { x1: (806 - elift).toFixed(1) });
      set(c.evHead, { x1: (806 - elift).toFixed(1), x2: (806 - elift).toFixed(1) });
      c.ivHead.classList.toggle("open", ilift > 0.5);
      c.ivStem.classList.toggle("open", ilift > 0.5);
      c.evHead.classList.toggle("open", elift > 0.5);
      c.evStem.classList.toggle("open", elift > 0.5);
      set(c.icam, { transform: `translate(942 ${y - 24}) rotate(${((l - imid) / 2).toFixed(1)})` });
      set(c.ecam, { transform: `translate(942 ${y + 24}) rotate(${((l - emid) / 2).toFixed(1)})` });
      const sp = g.spark == null ? 999 : norm(l - g.spark), spark = sp < 28 ? 1 - sp / 28 : 0;
      set(c.spark, { r: (4 + 18 * spark).toFixed(1), opacity: spark > 0 ? (0.35 + 0.65 * spark).toFixed(2) : 0 });
      c.spray.style.opacity = g.inj && inWin(l, g.inj[0], g.inj[1]) >= 0 ? 0.85 : 0;
      // the charge: burning after the spark through the power stroke, unless this cylinder is misfiring
      const s = stroke(l), rough = !!st.rough[d.n];
      let gas;
      if (s === "Power") { const b = Math.max(0, 1 - l / 180); gas = `rgba(${Math.round(122 + 100 * b)},${Math.round(58 + 40 * b)},18,${(0.35 + 0.55 * b).toFixed(2)})`; }
      else if (s === "Exhaust") gas = "rgba(90,80,70,0.45)";
      else if (s === "Intake") gas = "rgba(76,201,240,0.18)";
      else gas = `rgba(120,150,180,${(0.15 + (0.3 * (l - 540)) / 180).toFixed(2)})`;
      if (sp < 40 && s !== "Power") gas = `rgba(255,170,60,${(0.4 + 0.5 * (1 - sp / 40)).toFixed(2)})`;
      if (rough && (s === "Power" || sp < 40)) gas = "rgba(120,150,180,0.22)";
      set(c.gas, { x: crown.toFixed(1), width: Math.max(0, 806 - crown).toFixed(1), fill: gas });
      c.wall.classList.toggle("rough", rough);
      const mis = rough && s === "Power";
      c.label.textContent = mis ? "MISFIRE" : s.toUpperCase();
      c.label.setAttribute("data-s", mis ? "misfire" : s.toLowerCase());
    }
    // front view of the crank: right-bank pins at the crank angle, left-bank pins opposite
    if (st.pins) {
      for (const [i, p] of st.pins.entries()) {
        const d = CYL.find((c) => c.n === p.n);
        const t = ((norm(a - d.off) + (d.right ? 0 : 180)) * Math.PI) / 180, r = i % 2 ? 46 : 58;
        const x = 100 + r * Math.cos(t), y = 100 + r * Math.sin(t);
        set(p.arm, { x2: x.toFixed(1), y2: y.toFixed(1) });
        set(p.dot, { cx: x.toFixed(1), cy: y.toFixed(1) });
        set(p.t, { x: x.toFixed(1), y: (y + 4).toFixed(1) });
      }
    }
    st.onAngle?.(a);
  }

  function frame(now) {
    if (!st.playing) return;
    const dt = Math.min(0.1, (now - (st.last || now)) / 1000);
    st.last = now;
    st.ang = norm(st.ang + SPEEDS[st.speed] * dt);
    draw();
    st.raf = requestAnimationFrame(frame);
  }

  return {
    /** Build the engine into box and the crank front view into pinBox (once). onAngle(a) on each frame. */
    mount(box, pinBox, onAngle) { build(box); buildPins(pinBox); st.onAngle = onAngle; },
    /** geo: EngineTiming.geometry(...); rough: {cyl: true} for cylinders misfiring now. */
    update(geo, rough) { st.geo = geo; st.rough = rough || {}; if (!st.playing) draw(); },
    setPlaying(on) { st.playing = on; cancelAnimationFrame(st.raf); st.last = 0; if (on) st.raf = requestAnimationFrame(frame); },
    isPlaying: () => st.playing,
    setSpeed(s) { st.speed = s; },
    setAngle(a) { st.ang = norm(a); draw(); },
    SPEEDS,
  };
})();
