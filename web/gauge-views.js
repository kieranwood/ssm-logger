// Fixed gauge views (besides the free Grid): By system, Cluster and Signal path.
// Each is a hand-arranged dashboard of specific channels; anything not in the log is left out.
// build() makes the DOM once; update() runs on every new value. Dials are canvases handed back to
// app.js (api.addDial) so they share the grid's drawing, spring motion and danger pulse.

"use strict";

const GaugeViews = (() => {
  const CH = {
    rpm: "Engine Speed", speed: "Vehicle Speed", cool: "Coolant Temperature", load: "Engine Load",
    ign: "Ignition Timing", inj: "Fuel Injector #1 Pulse Width", stft: "Air/Fuel Correction #1",
    ltft: "Air/Fuel Learning #1", tft: "Total Fuel Trim", afr: "Air/Fuel Sensor #1 AFR", o2: "Rear O2 Sensor",
    tps: "Throttle Opening Angle", maf: "Mass Air Flow", iat: "Intake Air Temperature",
    acc: "Accelerator Opening Angle", batt: "Battery Voltage", alt: "Alternator Duty",
    mrp: "Manifold Relative Pressure", map: "Manifold Absolute Pressure",
  };
  const VIEWS = [
    ["grid", "Grid", "M4 4h7v7H4z", "M13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z"],
    ["systems", "By system", "M3 5h8v14H3z", "M13 5h8v6h-8zM13 13h8v6h-8z"],
    ["cluster", "Cluster", "M4.5 17a8.5 8.5 0 1 1 15 0", "M12 13l4-5"],
    ["signal", "Signal path", "M3 12h4l3-6 4 12 3-6h4", ""],
  ];
  const ICON = {
    engine: '<path d="M7 8h10l2 3v5H5v-5z"></path><path d="M9 8V5h6v3M3 12h2M19 12h2"></path>',
    fuel: '<path d="M12 3s6 6.5 6 11a6 6 0 0 1-12 0c0-4.5 6-11 6-11z"></path>',
    air: '<path d="M3 8h11a3 3 0 1 0-3-3"></path><path d="M3 12h15a3 3 0 1 1-3 3"></path><path d="M3 16h7"></path>',
    temp: '<path d="M14 14.8V4a2 2 0 0 0-4 0v10.8a4 4 0 1 0 4 0z"></path>',
    exhaust: '<path d="M3 15h10a4 4 0 0 0 4-4V7"></path><path d="M17 4v3M20 9c1 1 1 2 0 3M14 18c1 1 1 2 0 3"></path>',
    ecu: '<rect x="6" y="6" width="12" height="12" rx="2"></rect><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"></path>',
    mil: '<path d="M5 10h2l2-3h6l1 3h2v2h2v-2h1v6h-1v-2h-2v3h-3l-2 2H9l-2-2H5z"></path>',
    batt: '<path d="M3 7h18v12H3z"></path><path d="M7 7V5M17 7V5M7 13h3M15.5 11.5v3M14 13h3"></path>',
    alert: '<path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"></path><path d="M12 9v4M12 17h.01"></path>',
    ok: '<circle cx="12" cy="12" r="9"></circle><path d="M8 12l3 3 5-6"></path>',
    arrow: '<path d="M5 12h14"></path><path d="M13 6l6 6-6 6"></path>',
  };
  const svg = (k, size = 16) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
    `stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[k]}</svg>`;

  function h(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }
  const pct = (v, a, b) => Math.max(0, Math.min(100, ((v - a) / (b - a)) * 100));
  const ok = (d) => d && d.value != null && Number.isFinite(d.value);
  const fmt = (v, dec) => (Number.isFinite(v) ? v.toFixed(dec) : "–");
  const sgn = (v, dec) => (Number.isFinite(v) ? (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(dec) : "–");

  // ---------------------------------------------------------------- building blocks
  // Each returns an element with ._update(get) that redraws it from the current values.

  /** Label and value; opts: unit, dec, signed, color(d) -> class. */
  function readout(label, name, opts = {}) {
    const e = h("div", "gv-read");
    e.innerHTML = `<span class="k"></span><span class="v num"><b></b><small></small></span>`;
    e.querySelector(".k").textContent = label;
    e._update = (get) => {
      const d = get(name);
      const b = e.querySelector("b");
      b.textContent = !ok(d) ? "–" : opts.signed ? sgn(d.value, opts.dec ?? 1) : fmt(d.value, opts.dec ?? d.decimals);
      e.querySelector("small").textContent = opts.unit ?? d?.units ?? "";
      e.dataset.z = ok(d) ? (opts.zone ? opts.zone(d) : zoneAt(d.spec, d.value) || "") : "";
    };
    return e;
  }

  /** Readout above a centre bar (fuel trims): rich blue to the left, lean amber to the right. */
  function centreBar(label, name, R, opts = {}) {
    const e = h("div", "gv-item");
    const r = readout(label, name, { signed: true, dec: 1, unit: "%", zone: (d) => (Math.abs(d.value) >= (opts.warnAbs ?? 10) ? "warn" : "") });
    const bar = h("div", "gv-cbar", `<span class="l">RICH</span><span class="r">LEAN</span><i class="fill"></i><i class="zero"></i>`);
    e.append(r, bar);
    e._update = (get) => {
      r._update(get);
      const d = get(name), f = bar.querySelector(".fill");
      if (!ok(d)) { f.style.width = "0"; return; }
      const w = Math.min(50, (Math.abs(d.value) / R) * 50);
      f.style.left = `${d.value >= 0 ? 50 : 50 - w}%`;
      f.style.width = `${w}%`;
      f.classList.toggle("rich", d.value < 0);
    };
    return e;
  }

  /** AFR on a 10-20 scale: the good band, a stoich line and a diamond for the value. */
  function afrTarget(label, name) {
    const e = h("div", "gv-item");
    const r = readout(label, name, { dec: 2, unit: "AFR" });
    const bar = h("div", "gv-target", `<i class="band"></i><i class="mid"></i><i class="dot"></i>`);
    const scale = h("div", "gv-scale num", "<span>rich 10</span><span>stoich 14.7</span><span>20 lean</span>");
    e.append(r, bar, scale);
    bar.querySelector(".band").style.left = `${pct(14.2, 10, 20)}%`;
    bar.querySelector(".band").style.width = `${pct(15.2, 10, 20) - pct(14.2, 10, 20)}%`;
    bar.querySelector(".mid").style.left = `${pct(14.7, 10, 20)}%`;
    scale.children[1].style.left = `${pct(14.7, 10, 20)}%`;
    e._update = (get) => {
      r._update(get);
      const d = get(name), dot = bar.querySelector(".dot");
      dot.hidden = !ok(d);
      if (ok(d)) dot.style.left = `${pct(d.value, 10, 20)}%`;
      dot.dataset.z = r.dataset.z;
    };
    return e;
  }

  /** A readout beside a small vertical bar (rear O2, throttle), with end labels and a peak line. */
  function vbar(label, name, lo, hi, ends, opts = {}) {
    const e = h("div", "gv-item gv-vrow");
    const r = readout(label, name, opts);
    const side = h("div", "gv-vside", `<div class="ends"><span></span><span></span></div><div class="gv-vbar"><i class="fill"></i><i class="peak"></i></div>`);
    side.querySelectorAll(".ends span")[0].textContent = ends[0];
    side.querySelectorAll(".ends span")[1].textContent = ends[1];
    e.append(r, side);
    e._update = (get) => {
      r._update(get);
      const d = get(name);
      side.querySelector(".fill").style.height = ok(d) ? `${pct(d.value, lo, hi)}%` : "0";
      const pk = side.querySelector(".peak");
      pk.hidden = !(d && Number.isFinite(d.max));
      if (!pk.hidden) pk.style.bottom = `${pct(d.max, lo, hi)}%`;
      side.querySelector(".fill").dataset.z = r.dataset.z;
    };
    return e;
  }

  /** A readout above a thin horizontal bar (airflow, throttle, accelerator). */
  function hbar(label, name, lo, hi, opts = {}) {
    const e = h("div", "gv-item");
    const r = readout(label, name, opts);
    const bar = h("div", "gv-hbar", `<i class="fill"></i>`);
    e.append(r, bar);
    e._update = (get) => {
      r._update(get);
      const d = get(name);
      bar.querySelector(".fill").style.width = ok(d) ? `${pct(d.value, lo, hi)}%` : "0";
      bar.querySelector(".fill").dataset.z = r.dataset.z;
    };
    return e;
  }

  /** Battery voltage as a filling battery on 10-16 V, with a dashed line at 12 V. */
  function battery(label, name) {
    const e = h("div", "gv-item");
    const r = readout(label, name, { dec: 2, unit: "V" });
    const b = h("div", "gv-batt", `<div class="body"><i class="fill"></i><i class="mark"></i></div><i class="nub"></i>`);
    const scale = h("div", "gv-scale num", "<span>10</span><span>12 V</span><span>16</span>");
    e.append(r, b, scale);
    b.querySelector(".mark").style.left = `${pct(12, 10, 16)}%`;
    scale.children[1].style.left = `${pct(12, 10, 16)}%`;
    e._update = (get) => {
      r._update(get);
      const d = get(name), f = b.querySelector(".fill");
      f.style.width = ok(d) ? `${pct(d.value, 10, 16)}%` : "0";
      f.dataset.z = r.dataset.z;
    };
    return e;
  }

  /** A note that appears when a check fails (or a quiet "fine" line). check(get) -> {level, html} | null */
  function flag(check) {
    const e = h("div", "gv-flag");
    e._update = (get) => {
      const res = check(get);
      e.hidden = !res;
      if (!res) return;
      e.dataset.level = res.level;
      e.innerHTML = svg(res.level === "ok" ? "ok" : "alert") + `<span>${res.html}</span>`;
    };
    return e;
  }

  function section(title, icon, note) {
    const s = h("section", "gv-sec");
    const head = h("div", "gv-sec-head", `<span class="ic">${svg(icon)}</span><h3></h3>`);
    head.querySelector("h3").textContent = title;
    if (note != null) {
      const n = h("span", "gv-note");
      if (typeof note === "string") n.textContent = note;
      head.append(n);
      s._note = n;
    }
    s.append(head);
    return s;
  }

  // ---------------------------------------------------------------- checks shared by the views
  const totalTrim = (get) => {
    const t = get(CH.tft);
    if (ok(t)) return t.value;
    const a = get(CH.stft), b = get(CH.ltft);
    return ok(a) || ok(b) ? (ok(a) ? a.value : 0) + (ok(b) ? b.value : 0) : NaN;
  };
  const running = (get) => { const r = get(CH.rpm); return !r || !ok(r) || r.value > 400; };

  /** Front wideband says stoich while the rear narrowband sits rich (or lean): they disagree. */
  function sensorCheck(get, quiet = true) {
    const a = get(CH.afr), o = get(CH.o2);
    if (!ok(a) || !ok(o) || !running(get)) return null;
    const stoich = a.value >= 14.2 && a.value <= 15.2;
    if (stoich && o.value > 0.8) return { level: "danger", html: "<b>Sensors disagree:</b> front says stoich, rear reads rich." };
    if (stoich && o.value < 0.2) return { level: "warn", html: "<b>Sensors disagree:</b> front says stoich, rear reads lean." };
    return quiet ? { level: "ok", html: "Front and rear sensors agree." } : null;
  }

  function ecuCheck(get) {
    const t = totalTrim(get);
    if (!Number.isFinite(t) || Math.abs(t) < 10 || !running(get)) return null;
    const lean = t > 0;
    return {
      level: Math.abs(t) >= 25 ? "danger" : "warn",
      html: `The ECU is ${lean ? "adding" : "removing"} <b>${Math.abs(t).toFixed(1)}%</b> fuel to hold the target mixture. ` +
        `Held for long enough this sets ${lean ? "P0171 (system too lean)" : "P0172 (system too rich)"}.`,
    };
  }

  // ---------------------------------------------------------------- the views

  function dial(api, name, px, inner) {
    const wrap = h("div", "gv-dial");
    const cv = document.createElement("canvas");
    cv.style.width = `${px}px`;
    wrap.append(cv);
    api.addDial(name, cv, wrap, inner && api.has(CH.load) ? { inner: CH.load } : {});
    return wrap;
  }

  function buildSystems(root, api) {
    const has = api.has, items = [];
    const use = (el) => { items.push(el); return el; };
    root.className = "gv gv-systems";

    // engine: revs with load inside, speed and timing beside it
    if (has(CH.rpm)) {
      const s = section("Engine", "engine", "speed and load in one dial");
      const body = h("div", "gv-eng");
      body.append(dial(api, CH.rpm, 300, true));
      const col = h("div", "gv-col");
      if (has(CH.speed)) col.append(use(readout("Vehicle speed", CH.speed, { dec: 0, unit: "km/h" })));
      if (has(CH.ign)) col.append(use(readout("Ignition timing", CH.ign, { dec: 0, unit: "° BTDC" })));
      if (has(CH.load)) col.append(use(readout("Engine load", CH.load, { dec: 0, unit: "%" })));
      body.append(col);
      s.append(body);
      if (has(CH.load)) s.append(h("div", "gv-legend", '<span><i class="o"></i>Outer ring: engine speed</span><span><i class="i"></i>Inner ring: engine load</span>'));
      root.append(s);
    }

    // fuel mixture: how the ECU is correcting, then what the sensors see
    const trims = [CH.stft, CH.ltft].filter(has);
    if (trims.length || has(CH.tft) || has(CH.afr) || has(CH.o2)) {
      const s = section("Fuel mixture", "fuel", "");
      const note = s._note;
      items.push({ _update: (get) => {
        const t = totalTrim(get);
        note.textContent = !Number.isFinite(t) ? "" : t >= 10 ? `running lean · ${sgn(t, 1)}%` : t <= -10 ? `running rich · ${sgn(t, 1)}%` : "mixture in range";
        note.dataset.z = Number.isFinite(t) && Math.abs(t) >= 10 ? "warn" : "";
      } });
      if (trims.length || has(CH.tft)) {
        s.append(h("div", "gv-sub", "How the ECU is correcting"));
        const row = h("div", "gv-sum");
        const parts = [];
        if (has(CH.stft)) parts.push(use(centreBar("Short-term · correction #1", CH.stft, 35)));
        if (has(CH.ltft)) parts.push(use(centreBar("Long-term · learning #1", CH.ltft, 25)));
        if (has(CH.tft)) parts.push(use(centreBar("Total fuel trim", CH.tft, 35)));
        parts.forEach((p, k) => {
          if (k) row.append(h("span", "op", k === parts.length - 1 && has(CH.tft) ? "=" : "+"));
          row.append(p);
        });
        s.append(row);
      }
      if (has(CH.afr) || has(CH.o2)) {
        s.append(h("div", "gv-sub", "What the oxygen sensors see"));
        const row = h("div", "gv-pair");
        if (has(CH.afr)) row.append(use(afrTarget("Front A/F · sensor 1", CH.afr)));
        if (has(CH.o2)) row.append(use(vbar("Rear O2 · sensor 2", CH.o2, 0, 1.1, ["rich", "lean"], { dec: 2, unit: "V",
          zone: (d) => (d.value > 0.8 ? "danger" : "") })));
        s.append(row);
        if (has(CH.afr) && has(CH.o2)) s.append(use(flag((get) => sensorCheck(get))));
      }
      root.append(s);
    }

    // air intake, in the order the air goes
    if ([CH.tps, CH.maf, CH.iat, CH.acc].some(has)) {
      const s = section("Air intake", "air", "follows the air into the engine");
      const row1 = h("div", "gv-flow");
      if (has(CH.tps)) row1.append(use(hbar("Throttle", CH.tps, 0, 100, { dec: 1, unit: "%" })));
      if (has(CH.tps) && has(CH.maf)) row1.append(h("span", "op arrow", svg("arrow")));
      if (has(CH.maf)) row1.append(use(hbar("Mass air flow", CH.maf, 0, 160, { dec: 1, unit: "g/s" })));
      const row2 = h("div", "gv-flow");
      if (has(CH.iat)) row2.append(use(readout("Intake air temp", CH.iat, { dec: 0, unit: "°C" })));
      if (has(CH.acc)) row2.append(use(hbar("Accelerator", CH.acc, 0, 100, { dec: 1, unit: "%" })));
      s.append(row1, row2);
      root.append(s);
    }

    // temperature and electrical
    if (has(CH.cool) || has(CH.batt) || has(CH.alt)) {
      const s = section("Temperature & electrical", "temp");
      const body = h("div", "gv-eng");
      if (has(CH.cool)) body.append(dial(api, CH.cool, 200));
      const col = h("div", "gv-col");
      if (has(CH.batt)) col.append(use(battery("Battery", CH.batt)));
      if (has(CH.alt)) col.append(use(readout("Alternator duty", CH.alt, { dec: 0, unit: "%" })));
      body.append(col);
      s.append(body);
      root.append(s);
    }
    return items;
  }

  function buildCluster(root, api) {
    const has = api.has, items = [];
    const use = (el) => { items.push(el); return el; };
    root.className = "gv gv-cluster";
    const top = h("section", "gv-sec gv-cl-top");
    if (has(CH.rpm)) top.append(dial(api, CH.rpm, 340));
    const mid = h("div", "gv-cl-mid");
    const lamps = h("div", "gv-lamps");
    const lamp = (icon) => { const l = h("div", "gv-lamp", `<span class="ic">${svg(icon, 20)}</span><span class="t"></span>`); lamps.append(l); return l; };
    const lMil = lamp("mil"), lCold = has(CH.cool) ? lamp("temp") : null, lBatt = has(CH.batt) ? lamp("batt") : null,
      lMix = has(CH.tft) || has(CH.stft) ? lamp("fuel") : null;
    const setLamp = (l, on, text, kind) => { if (!l) return; l.dataset.on = on ? kind : ""; l.querySelector(".t").textContent = text; };
    items.push({ _update: (get, extra) => {
      const mil = extra.mil;
      setLamp(lMil, mil === true, "Check engine", "warn");
      lMil.title = mil == null ? "Read the fault codes to check the light" : mil ? "Check-engine light is on" : "Check-engine light is off";
      const c = get(CH.cool);
      if (lCold) setLamp(lCold, ok(c) && zoneAt(c.spec, c.value) === "cold", ok(c) && zoneAt(c.spec, c.value) === "cold" ? "Cold engine" : "Engine warm", "cold");
      const b = get(CH.batt), bz = ok(b) ? zoneAt(b.spec, b.value) : null;
      if (lBatt) setLamp(lBatt, !!bz && bz !== "ok", bz && bz !== "ok" ? "Check charging" : "Charging OK", "danger");
      const t = totalTrim(get);
      if (lMix) setLamp(lMix, Math.abs(t) >= 10, t >= 10 ? "Running lean" : t <= -10 ? "Running rich" : "Mixture OK", "warn");
    } });
    mid.append(lamps);
    if (has(CH.speed)) {
      const sp = h("div", "gv-speed", `<b class="num"></b><span>km/h</span>`);
      items.push({ _update: (get) => { const d = get(CH.speed); sp.querySelector("b").textContent = ok(d) ? d.value.toFixed(0) : "–"; } });
      mid.append(sp);
    }
    if (has(CH.tft)) mid.append(use(centreBar("Total fuel trim", CH.tft, 35)));
    const sess = h("div", "gv-sess num", "<span></span><span></span>");
    items.push({ _update: (get, extra) => {
      sess.children[0].textContent = extra.elapsed;
      sess.children[1].textContent = extra.rows;
    } });
    mid.append(sess);
    top.append(mid);
    if (has(CH.cool)) top.append(dial(api, CH.cool, 260));
    root.append(top);

    const grid = h("div", "gv-cl-grid");
    const tile = (el) => { const s = h("section", "gv-sec gv-mini"); s.append(use(el)); grid.append(s); };
    if (has(CH.afr)) tile(afrTarget("Air/fuel ratio", CH.afr));
    if (has(CH.batt)) tile(battery("Battery", CH.batt));
    if (has(CH.tps)) tile(vbar("Throttle", CH.tps, 0, 100, ["100", "0"], { dec: 1, unit: "%" }));
    if (has(CH.o2)) tile(vbar("Rear O2", CH.o2, 0, 1.1, ["rich", "lean"], { dec: 2, unit: "V", zone: (d) => (d.value > 0.8 ? "danger" : "") }));
    if (grid.children.length) root.append(grid);
    return items;
  }

  function buildSignal(root, api) {
    const has = api.has, items = [];
    const use = (el) => { items.push(el); return el; };
    root.className = "gv gv-signal";
    const path = h("div", "gv-path");
    const steps = [];
    const step = (n, title, icon, level) => {
      const s = section(title, icon);
      s.classList.add("gv-step");
      s.querySelector(".gv-sec-head").prepend(h("span", "n num", String(n)));
      if (level) items.push({ _update: (get) => { s.dataset.z = level(get) || ""; } });
      steps.push(s);
      return s;
    };

    const air = [CH.iat, CH.tps, CH.maf, CH.mrp, CH.map].some(has) ? step(1, "Air in", "air") : null;
    if (air) {
      if (has(CH.iat)) air.append(use(readout("Intake air temp", CH.iat, { dec: 0, unit: "°C" })));
      if (has(CH.tps)) air.append(use(hbar("Throttle", CH.tps, 0, 100, { dec: 1, unit: "%" })));
      if (has(CH.maf)) air.append(use(hbar("Mass air flow", CH.maf, 0, 160, { dec: 1, unit: "g/s" })));
      const man = has(CH.mrp) ? CH.mrp : has(CH.map) ? CH.map : null;
      if (man) air.append(use(readout("Manifold", man, { dec: 1 })));
    }
    const eng = [CH.rpm, CH.ign, CH.inj].some(has)
      ? step(2, "Engine", "engine", (get) => { const r = get(CH.rpm); return ok(r) ? zoneAt(r.spec, r.value) : ""; }) : null;
    if (eng) {
      if (has(CH.rpm)) eng.append(dial(api, CH.rpm, 220, true));
      if (has(CH.ign)) eng.append(use(readout("Ignition timing", CH.ign, { dec: 0, unit: "° BTDC" })));
      if (has(CH.inj)) eng.append(use(readout("Injector pulse", CH.inj, { dec: 2, unit: "ms" })));
    }
    const ex = has(CH.afr) || has(CH.o2)
      ? step(3, "Exhaust", "exhaust", (get) => sensorCheck(get, false)?.level) : null;
    if (ex) {
      if (has(CH.afr)) ex.append(use(afrTarget("Front A/F sensor", CH.afr)));
      if (has(CH.o2)) ex.append(use(hbar("Rear O2 sensor", CH.o2, 0, 1.1, { dec: 2, unit: "V", zone: (d) => (d.value > 0.8 ? "danger" : "") })));
      if (has(CH.afr) && has(CH.o2)) ex.append(use(flag((get) => sensorCheck(get, false))));
    }
    const ecu = [CH.stft, CH.ltft, CH.tft].some(has) ? step(4, "ECU correction", "ecu", (get) => ecuCheck(get)?.level) : null;
    if (ecu) {
      if (has(CH.stft)) ecu.append(use(centreBar("Short-term · correction #1", CH.stft, 35)));
      if (has(CH.ltft)) ecu.append(use(centreBar("Long-term · learning #1", CH.ltft, 25)));
      if (has(CH.tft)) ecu.append(use(centreBar("Total fuel trim", CH.tft, 35)));
      ecu.append(use(flag(ecuCheck)));
    }
    steps.forEach((s, k) => {
      if (k) path.append(h("span", "gv-arrow", svg("arrow", 20)));
      path.append(s);
    });
    root.append(path);
    root.append(h("p", "gv-foot", "<b>Read left to right.</b> Each step turns amber or red when its readings stop agreeing with the step before it."));
    return items;
  }

  const BUILD = { systems: buildSystems, cluster: buildCluster, signal: buildSignal };

  /** Build view into root. api: { has(name), addDial(name, canvas, el, opts) }. Returns { update(get, extra) }. */
  function build(view, root, api) {
    root.replaceChildren();
    const items = (BUILD[view] || buildSystems)(root, api);
    if (!root.children.length) root.append(h("p", "g-hint", "None of the channels this view uses are in this log."));
    return { update: (get, extra) => { for (const it of items) it._update(get, extra); } };
  }

  return { VIEWS, build, svg };
})();
