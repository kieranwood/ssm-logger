// CAN bus: the raw traffic between the car's modules, read listen-only from the OBD port - live
// (alongside the logging, if the adapter allows a second channel) or from a capture made with
// `probe.py --listen` (its CSV, and the _ssm.csv beside it). Four views: a monitor of every message,
// a signal finder that compares picked bits with a logged channel, a map of which module sends what,
// and the signals found so far, exported as a DBC file. Nothing here transmits on the bus.
"use strict";

const CanBus = (() => {
  const KEEP_S = 120;                       // live: seconds of frames kept per message
  const SIG_KEY = "ssm-logger.canSignals", NODE_KEY = "ssm-logger.canNodes";
  const MODULES = [
    ["ecm", "Engine control"], ["tcm", "Transmission"], ["abs", "ABS / stability"], ["steer", "Steering angle"],
    ["biu", "Body integration"], ["meter", "Instrument cluster"], ["other", "Other module"],
  ];
  const HOW = { confirmed: "Confirmed by you", likely: "Likely", unknown: "Not sure" };
  const hx = (n, w = 3) => n.toString(16).toUpperCase().padStart(w, "0");
  const idTxt = (id) => (id > 0x7ff ? `0x${hx(id, 8)}` : `0x${hx(id)}`);
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const text = (tag, cls, t) => { const e = el(tag, cls); e.textContent = t; return e; };
  const load = (k, d) => { try { return JSON.parse(localStorage.getItem(k) || "") ?? d; } catch { return d; } };
  const store = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } };

  const st = {
    view: "monitor", tab: "table", source: null, srcName: "", demo: false, paused: false, listening: false,
    msgs: new Map(),                        // id -> {id, t: [], d: [], flips: Uint32Array(64), changed: Set, flashAt: [8]}
    refs: null,                             // file captures: [{name, units, t, y}] from the _ssm.csv
    finder: { id: null, start: 0, len: 16, order: "be", ref: null },
    net: { sel: "unassigned" },
    signals: load(SIG_KEY, []), nodes: load(NODE_KEY, {}),
    showDbc: true, loadHist: [], raf: 0, lastDraw: 0, best: null, bestKey: "",
  };

  // ---------------------------------------------------------------- frames
  function addFrame(t, id, data) {
    let m = st.msgs.get(id);
    if (!m) {
      m = { id, t: [], d: [], flips: new Uint32Array(64), changed: new Set(), flashAt: new Array(8).fill(-1e9) };
      st.msgs.set(id, m);
    }
    const prev = m.d[m.d.length - 1];
    if (prev) {
      for (let i = 0; i < 8; i++) {
        const x = (prev[i] ?? 0) ^ (data[i] ?? 0);
        if (!x) continue;
        m.changed.add(i);
        m.flashAt[i] = t;
        for (let b = 0; b < 8; b++) if ((x >> (7 - b)) & 1) m.flips[i * 8 + b]++;
      }
    }
    m.t.push(t);
    m.d.push(data);
    if (st.source === "live" && m.t.length > 50 && t - m.t[0] > KEEP_S + 10) {   // trim in chunks
      const cut = m.t.findIndex((x) => x >= t - KEEP_S);
      m.t.splice(0, cut);
      m.d.splice(0, cut);
    }
  }

  function reset(source, name) {
    st.msgs.clear();
    st.refs = null;
    st.source = source;
    st.srcName = name || "";
    st.loadHist = [];
    st.best = null;
    st.bestKey = "";
  }

  const sorted = () => [...st.msgs.values()].sort((a, b) => a.id - b.id);
  const tNow = () => {
    let x = -Infinity;
    for (const m of st.msgs.values()) x = Math.max(x, m.t[m.t.length - 1]);
    return x;
  };
  function rateOf(m, now) {
    if (st.source === "file") return m.t.length > 1 ? (m.t.length - 1) / (m.t[m.t.length - 1] - m.t[0] || 1) : 0;
    let n = 0;
    for (let i = m.t.length - 1; i >= 0 && m.t[i] > now - 1; i--) n++;
    return n;
  }
  /** Bus load estimate: an 8-byte standard frame is about 125 bits with stuffing, at 500 kbit/s. */
  const loadOf = (fps) => (fps * 125) / 500000 * 100;

  // ---------------------------------------------------------------- reference channels
  function refs() {
    if (st.source === "file") return st.refs || [];
    const rec = state.rec;
    if (!rec || !rec.live) return [];
    return rec.channels.map((c, i) => ({ name: c.name, units: c.units || "", switch: !!c.switch, t: rec.t, y: rec.y[i] }));
  }

  function bsearch(arr, x) {             // last index with arr[i] <= x, or -1
    let lo = 0, hi = arr.length - 1, ans = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (arr[mid] <= x) { ans = mid; lo = mid + 1; } else hi = mid - 1; }
    return ans;
  }

  /** Bits start..start+len-1, numbered as the signal finder shows them: big-endian counts from each
   *  byte's top bit (bit 7 of byte 0 is 0), little-endian from each byte's bottom bit. */
  function value(d, start, len, order) {
    let v = 0;
    for (let k = 0; k < len; k++) {
      const i = start + k;
      if (i > 63) return null;
      const bit = order === "be" ? ((d[i >> 3] ?? 0) >> (7 - (i & 7))) & 1 : ((d[i >> 3] ?? 0) >> (i & 7)) & 1;
      v = order === "be" ? v * 2 + bit : v + bit * 2 ** k;
    }
    return v;
  }

  function stats(xs, ys) {
    const n = xs.length;
    if (n < 3) return { r: 0, a: 0, b: 0 };
    let mx = 0, my = 0;
    for (let i = 0; i < n; i++) { mx += xs[i]; my += ys[i]; }
    mx /= n; my /= n;
    let sxy = 0, sxx = 0, syy = 0;
    for (let i = 0; i < n; i++) { const dx = xs[i] - mx, dy = ys[i] - my; sxy += dx * dy; sxx += dx * dx; syy += dy * dy; }
    const r = sxx && syy ? sxy / Math.sqrt(sxx * syy) : 0, a = sxx ? sxy / sxx : 0;
    return { r, a, b: my - a * mx };
  }

  /** The reference channel and the times to compare at: the last 15 s live, the whole capture from a file. */
  function refWindow(ref) {
    if (!ref) return null;
    const t = ref.t, n = t.length;
    if (n < 3) return null;
    // start once every message has been seen, so each field has a value at every compared moment
    let seen = -Infinity;
    for (const m of st.msgs.values()) if (m.id < 0x7df || m.id > 0x7ef) seen = Math.max(seen, m.t[0]);
    let from = Math.max(0, bsearch(t, seen) + 1);
    if (st.source === "live") from = Math.max(from, bsearch(t, t[n - 1] - 15));
    if (n - from < 10) return null;
    const step = Math.max(1, Math.floor((n - from) / 300));
    const ts = [], ys = [];
    for (let i = from; i < n; i += step) if (Number.isFinite(ref.y[i])) { ts.push(t[i]); ys.push(ref.y[i]); }
    return { ts, ys };
  }

  function seriesAt(m, ts, start, len, order) {
    const out = [];
    for (const t of ts) {
      const i = bsearch(m.t, t);
      if (i < 0) return null;
      const v = value(m.d[i], start, len, order);
      if (v === null) return null;
      out.push(v);
    }
    return out;
  }

  // ---------------------------------------------------------------- window
  function open() {
    $("tools-menu").hidden = true;
    if (!st.source && state.session && state.rec?.live) startListening();
    $("can-dialog").showModal();
    draw(true);
    loop();
  }

  function loop() {
    cancelAnimationFrame(st.raf);
    const tick = (now) => {
      if (!$("can-dialog").open) return;
      // the monitor refreshes 5 times a second; the other views once a second, and not while a field
      // or menu in them is in use (a redraw would wipe what's being typed)
      const busy = $("can-body").contains(document.activeElement) && /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
      const every = st.view === "monitor" ? 200 : 1000;
      if (!st.paused && !busy && now - st.lastDraw > every) draw();
      st.raf = requestAnimationFrame(tick);
    };
    st.raf = requestAnimationFrame(tick);
  }

  async function startListening() {
    const s = state.session;
    if (!s) return;
    reset("live", "");
    st.demo = !!s.port.getInfo?.().demo;
    st.listening = true;
    draw(true);
    try {
      await s.startCan((t, id, data, err) => {
        if (err) { setStatus(`CAN listening stopped: ${err}`, true); return; }
        if (!st.paused) addFrame(t, id, data);
      });
    } catch (e) {
      st.listening = false;
      st.error = /channel in use|device in use/i.test(e.message)
        ? "Your OpenPort won't open a second channel while it's logging, so it can't listen live. Record a capture with " +
          "probe.py --listen (with the logger closed) and open it here."
        : `Couldn't listen: ${e.message}`;
      s.canCb = null;
    }
    draw(true);
  }

  function stopListening() {
    state.session?.stopCan();
    st.listening = false;
    draw(true);
  }

  // ---- captures: the CAN CSV from probe.py --listen, and optionally its _ssm.csv (same clock)
  async function openFiles(files) {
    if (st.listening) stopListening();
    const texts = await Promise.all([...files].map(async (f) => [f.name, await f.text()]));
    const can = texts.find(([, t]) => /^time_s,device_us,id,len,data/m.test(t.slice(0, 200)));
    if (!can) { setStatus("That isn't a CAN capture: pick the can_….csv that probe.py --listen saves (and its _ssm.csv).", true); return; }
    reset("file", can[0]);
    st.demo = false;
    const lines = can[1].split(/\r?\n/);
    for (const line of lines.slice(1)) {
      const c = line.split(",");
      if (c.length < 5) continue;
      const data = c[4].trim() ? c[4].trim().split(/\s+/).map((h) => parseInt(h, 16)) : [];
      addFrame(Number(c[0]), parseInt(c[2], 16), data);
    }
    const ssm = texts.find(([n, t]) => n !== can[0] && /^time_s,/m.test(t) && !/device_us/.test(t.slice(0, 100)));
    if (ssm) {
      const rows = ssm[1].split(/\r?\n/).filter(Boolean);
      const head = rows[0].split(",");
      const cols = head.map(() => []);
      for (const r of rows.slice(1)) r.split(",").forEach((v, i) => cols[i]?.push(v === "" ? NaN : Number(v)));
      st.refs = head.slice(1).map((name, i) => ({ name, units: "", switch: /switch/i.test(name), t: cols[0], y: cols[i + 1] }));
    }
    setStatus(`Opened ${can[0]}: ${st.msgs.size} messages${ssm ? `, with ${st.refs.length} logged values from ${ssm[0]}` : ""}.`);
    draw(true);
  }

  function saveCapture() {
    const all = [];
    for (const m of st.msgs.values()) m.t.forEach((t, i) => all.push([t, m.id, m.d[i]]));
    all.sort((a, b) => a[0] - b[0]);
    if (!all.length) return;
    const t0 = all[0][0];
    const name = `can_${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "").replace(/^(\d{8})/, "$1_")}`;
    const csv = ["time_s,device_us,id,len,data", ...all.map(([t, id, d]) =>
      `${(t - t0).toFixed(4)},,${hx(id, 0)},${d.length},${[...d].map((b) => hx(b, 2)).join(" ")}`)].join("\n");
    download(new Blob([csv + "\n"], { type: "text/csv" }), `${name}.csv`);
    const rs = refs();
    if (rs.length) {
      const t = rs[0].t, from = Math.max(0, bsearch(t, t0) + 1);
      const rows = [["time_s", ...rs.map((r) => r.name)].join(",")];
      for (let i = from; i < t.length; i++) rows.push([(t[i] - t0).toFixed(4), ...rs.map((r) => (Number.isFinite(r.y[i]) ? r.y[i] : ""))].join(","));
      download(new Blob([rows.join("\n") + "\n"], { type: "text/csv" }), `${name}_ssm.csv`);
    }
  }

  // ---------------------------------------------------------------- drawing
  function draw(full = false) {
    st.lastDraw = performance.now();
    const now = tNow();
    let fps = 0;
    for (const m of st.msgs.values()) fps += rateOf(m, now);
    const ld = loadOf(fps);
    if (st.source === "live" && !st.paused && Number.isFinite(now)) { st.loadHist.push(ld); if (st.loadHist.length > 120) st.loadHist.shift(); }
    const titles = { monitor: "CAN bus", finder: "Signal finder", network: "CAN network", signals: "CAN signals" };
    const leads = {
      monitor: "Every message on the bus. Bytes light up as they change, so moving values stand out from static ones. Pick a message to work out what it carries in the signal finder.",
      finder: "Pick bits out of a message and compare them with a channel the app logs. When the shapes match, you've found the signal and its scaling.",
      network: "The modules on the bus and the messages each one sends. CAN frames don't say who sent them, so senders are worked out, not read: assign each message to the module you think sends it.",
      signals: "Every signal you've identified. Export them as a DBC file, the standard format other CAN tools read, or import one to label your bus.",
    };
    $("can-title").textContent = titles[st.view];
    $("can-lead").textContent = leads[st.view];
    $("can-bus-pill").hidden = !st.msgs.size;
    $("can-bus-pill").textContent = `500 kbit/s · ~${ld.toFixed(0)}% load · ${Math.round(fps)} frames/s`;
    $("can-src-pill").hidden = !st.source;
    $("can-src-pill").textContent = st.source === "file" ? `Capture: ${st.srcName}` : st.demo ? "Demo traffic" : "Live";
    const live = !!state.session && !!state.rec?.live;
    $("can-listen").hidden = !live;
    $("can-listen").textContent = st.listening ? "Stop listening" : "Listen";
    $("can-pause").hidden = st.source !== "live";
    $("can-pause").textContent = st.paused ? "Resume" : "Pause";
    $("can-save").hidden = st.source !== "live" || !st.msgs.size;
    let total = 0;
    for (const m of st.msgs.values()) total += m.t.length;
    $("can-count").textContent = st.msgs.size ? `${st.msgs.size} IDs · ${total.toLocaleString("en")} frames${st.source === "live" ? ` (last ${KEEP_S} s kept)` : ""}` : "";
    for (const b of $("can-nav").children) b.setAttribute("aria-pressed", String(b.dataset.view === st.view));
    const body = $("can-body");
    if (!st.msgs.size && st.view !== "signals") {
      body.replaceChildren(empty());
      return;
    }
    if (st.view === "monitor") drawMonitor(body, now, full);
    else if (st.view === "finder") drawFinder(body, now, full);
    else if (st.view === "network") drawNetwork(body, now, full);
    else drawSignals(body, now, full);
  }

  function empty() {
    const box = el("div", "can-empty");
    if (st.error) box.append(text("p", "can-err", st.error));
    box.append(text("h3", "", st.listening ? "Listening… no frames yet" : "No CAN traffic yet"));
    const p = el("p");
    p.innerHTML = state.session && state.rec?.live
      ? "Choose <b>Listen</b> to watch the bus while logging (if your OpenPort allows a second channel), or open a capture."
      : "Connect to the car and choose <b>Listen</b>, or open a capture made with <code>probe.py --listen 30 --with-ssm</code>: " +
        "pick its <code>can_….csv</code> and the <code>_ssm.csv</code> beside it, so the signal finder has logged values to compare with.";
    box.append(p);
    return box;
  }

  const labelsFor = (id) => st.signals.filter((g) => g.id === id).map((g) => g.name);
  const nodeOf = (id) => st.nodes[hx(id, 0)] || null;
  const moduleName = (k) => (MODULES.find(([x]) => x === k) || [null, "Not assigned"])[1];

  // ---- monitor
  function drawMonitor(body, now, full) {
    if (full || !body.querySelector(".can-monitor")) {
      body.replaceChildren();
      const wrap = el("div", "can-monitor");
      const tabs = el("div", "seg-group can-tabs");
      for (const [k, label] of [["table", "Live table"], ["bits", "Bit activity"], ["time", "Traffic timeline"]]) {
        const b = text("button", "seg", label);
        b.type = "button";
        b.setAttribute("aria-pressed", String(st.tab === k));
        b.addEventListener("click", () => { st.tab = k; draw(true); });
        tabs.append(b);
      }
      wrap.append(tabs, el("section", "can-panel can-mon-body"));
      body.append(wrap);
    }
    const panel = body.querySelector(".can-mon-body");
    const msgs = sorted();
    if (st.tab === "table") {
      const rows = [el("div", "can-row can-head", "<span>ID</span><span>Label</span><span>Len</span><span>Rate</span><span>Data bytes 0–7</span><span>Changing</span>")];
      for (const m of msgs) {
        const d = m.d[m.d.length - 1] || [];
        const row = el("button", "can-row");
        row.type = "button";
        const labs = labelsFor(m.id), node = nodeOf(m.id);
        const label = labs.length ? labs.join(", ") : node ? moduleName(node.node) : "Unknown";
        row.innerHTML = `<span class="num id"></span><span class="lab${labs.length ? "" : " dim"}"></span><span class="num">${d.length}</span>` +
          `<span class="num">${Math.round(rateOf(m, now))} Hz</span><span class="bytes"></span>` +
          `<span class="act"><span class="bar"><i style="width:${(m.changed.size / 8) * 100}%"></i></span><span class="num">${m.changed.size}/8</span></span>`;
        row.querySelector(".id").textContent = idTxt(m.id);
        row.querySelector(".lab").textContent = label;
        const bytes = row.querySelector(".bytes");
        for (let i = 0; i < 8; i++) {
          const b = el("span", "num byte");
          if (i < d.length) {
            b.textContent = hx(d[i], 2);
            if (now - m.flashAt[i] < 0.35 && st.source === "live") b.classList.add("flash");
            if (!d[i]) b.classList.add("zero");
          } else b.classList.add("none");
          bytes.append(b);
        }
        row.addEventListener("click", () => { st.finder.id = m.id; st.finder.start = 0; st.view = "finder"; draw(true); });
        rows.push(row);
      }
      panel.replaceChildren(...rows);
    } else if (st.tab === "bits") {
      let max = 1;
      for (const m of msgs) for (const v of m.flips) max = Math.max(max, v);
      const heat = (v) => (!v ? "h0" : v / max > 0.66 ? "h4" : v / max > 0.33 ? "h3" : v / max > 0.08 ? "h2" : "h1");
      const out = [text("p", "can-note", "How often each of the 64 bits has flipped. Busy low bits are usually counters or fast signals; bits that never change are padding or flags.")];
      const head = el("div", "can-bits-row head");
      head.append(el("span"));
      for (let i = 0; i < 8; i++) head.append(text("span", "num", `byte ${i}`));
      out.push(head);
      for (const m of msgs) {
        const row = el("div", "can-bits-row");
        row.append(text("span", "num id", idTxt(m.id)));
        for (let i = 0; i < 8; i++) {
          const cell = el("span", "can-bits");
          for (let b = 0; b < 8; b++) cell.append(el("i", heat(m.flips[i * 8 + b])));
          row.append(cell);
        }
        out.push(row);
      }
      out.push(text("p", "can-note num", "Within each byte: bit 7 on the left, bit 0 on the right."));
      panel.replaceChildren(...out);
    } else {
      const out = [text("p", "can-note", st.source === "file"
        ? "One second of traffic from the middle of the capture, one lane per message. Even spacing is a steady periodic message; gaps or bunching point to a busy or faulty module."
        : "The last second of traffic, one lane per message. Even spacing is a steady periodic message; gaps or bunching point to a busy or faulty module.")];
      let t1 = now;
      if (st.source === "file") {
        let a = Infinity;
        for (const m of msgs) a = Math.min(a, m.t[0]);
        t1 = (a + now) / 2 + 0.5;
      }
      for (const m of msgs) {
        let dpath = "";
        for (let i = m.t.length - 1; i >= 0 && m.t[i] > t1 - 1; i--) {
          if (m.t[i] > t1) continue;
          dpath += `M${((1 - (t1 - m.t[i])) * 1000).toFixed(1)} 4V20`;
        }
        const lane = el("div", "can-lane");
        lane.innerHTML = `<span class="num id"></span><svg viewBox="0 0 1000 24" preserveAspectRatio="none" aria-hidden="true"><path d="${dpath}"></path></svg><span class="num">${Math.round(rateOf(m, now))} Hz</span>`;
        lane.querySelector(".id").textContent = idTxt(m.id);
        out.push(lane);
      }
      if (st.loadHist.length > 1) {
        const pts = st.loadHist.map((v, i) => `${((i / (st.loadHist.length - 1)) * 1000).toFixed(1)},${(60 - Math.min(100, v) * 0.6).toFixed(1)}`).join(" ");
        const ld = el("div", "can-lane load");
        ld.innerHTML = `<span class="can-lbl">Bus load</span><svg viewBox="0 0 1000 60" preserveAspectRatio="none" aria-hidden="true"><polyline points="${pts}"></polyline><line x1="0" x2="1000" y1="18" y2="18"></line></svg><span class="num">~${st.loadHist[st.loadHist.length - 1].toFixed(0)}%</span>`;
        out.push(ld, text("p", "can-note", "Estimated from the frame rate (about 125 bits per frame at 500 kbit/s). Dashed line: 70%, where a CAN bus starts to struggle."));
      }
      panel.replaceChildren(...out);
    }
  }

  // ---- signal finder
  function drawFinder(body, now, full) {
    const f = st.finder, msgs = sorted();
    if (f.id == null || !st.msgs.has(f.id)) f.id = msgs[0]?.id;
    const m = st.msgs.get(f.id);
    const rs = refs();
    if (!f.ref || !rs.some((r) => r.name === f.ref)) f.ref = (rs.find((r) => /Engine Speed/.test(r.name)) || rs[0])?.name || null;
    const ref = rs.find((r) => r.name === f.ref);
    body.replaceChildren();
    const wrap = el("div", "can-finder");
    // message list
    const list = el("aside", "can-panel can-list");
    list.append(text("div", "can-lbl", "Messages"));
    for (const x of msgs) {
      const b = el("button", "can-li");
      b.type = "button";
      b.setAttribute("aria-pressed", String(x.id === f.id));
      b.innerHTML = `<span class="num id"></span><span class="sub"></span>`;
      b.querySelector(".id").textContent = idTxt(x.id);
      b.querySelector(".sub").textContent = labelsFor(x.id).join(", ") || `${Math.round(rateOf(x, now))} Hz · ${x.changed.size} changing`;
      b.addEventListener("click", () => { f.id = x.id; f.start = 0; draw(true); });
      list.append(b);
    }
    const main = el("div", "can-main");
    // bit picker
    const pick = el("section", "can-panel");
    const head = el("div", "can-sec-head");
    const h = el("h3");
    h.append(text("span", "num id", idTxt(f.id)), document.createTextNode(" · choose the bits"));
    head.append(h, seg([[1, "1"], [4, "4"], [8, "8"], [12, "12"], [16, "16"]], f.len, (v) => { f.len = v; draw(true); }, "Length in bits"),
      seg([["be", "Big-endian"], ["le", "Little-endian"]], f.order, (v) => { f.order = v; draw(true); }, "Byte order"));
    pick.append(head);
    const d = m.d[m.d.length - 1] || [];
    const grid = el("div", "can-bitgrid");
    grid.append(el("span"));
    for (let p = 7; p >= 0; p--) grid.append(text("span", "num hdr", `bit ${p}`));
    for (let by = 0; by < 8; by++) {
      grid.append(text("span", "num hdr", `byte ${by}`));
      for (let p = 7; p >= 0; p--) {
        const ib = f.order === "be" ? by * 8 + (7 - p) : by * 8 + p;
        const sel = ib >= f.start && ib < f.start + f.len, bit = ((d[by] ?? 0) >> p) & 1;
        const b = text("button", `bit${sel ? " sel" : ""}${ib === f.start ? " first" : ""}${bit ? " one" : ""}`, String(bit));
        b.type = "button";
        b.setAttribute("aria-label", `Byte ${by} bit ${p}, value ${bit}`);
        b.setAttribute("aria-pressed", String(sel));
        b.addEventListener("click", () => { f.start = ib; draw(true); });
        grid.append(b);
      }
    }
    pick.append(grid);
    const raw = value(d, f.start, f.len, f.order);
    const info = el("div", "can-info num");
    info.innerHTML = `<span>Click a bit to start the selection there.</span><span>raw value <b></b></span><span></span>`;
    info.children[1].querySelector("b").textContent = raw === null ? "—" : `${raw} (0x${hx(raw, 2)})`;
    info.children[2].textContent = `bits ${f.start}–${Math.min(63, f.start + f.len - 1)}${f.start + f.len > 64 ? " (runs off the end)" : ""}`;
    pick.append(info);
    main.append(pick);
    // compare
    const cmp = el("section", "can-panel");
    const ch = el("div", "can-sec-head");
    ch.append(text("h3", "", "Compare with"));
    if (rs.length) {
      const sel = el("select", "field");
      sel.setAttribute("aria-label", "Logged channel to compare with");
      for (const r of rs) sel.append(new Option(r.units ? `${r.name} (${r.units})` : r.name, r.name));
      sel.value = f.ref;
      sel.addEventListener("change", () => { f.ref = sel.value; draw(true); });
      ch.append(sel);
    }
    cmp.append(ch);
    const win = refWindow(ref);
    if (!win) {
      cmp.append(text("p", "can-note", st.source === "file"
        ? "This capture has no logged values beside it. Open the _ssm.csv that probe.py --with-ssm saves together with the capture to compare."
        : "Collecting: the comparison starts once every message has been heard and a few logged samples have come in."));
    } else {
      const ys = win.ys, xs = seriesAt(m, win.ts, f.start, f.len, f.order);
      const s = xs ? stats(xs, ys) : { r: 0, a: 0, b: 0 };
      const ar = Math.abs(s.r), strong = ar >= 0.95, maybe = ar >= 0.7;
      let lo = Math.min(...ys), hi = Math.max(...ys);
      const pad = (hi - lo) * 0.15 || 1; lo -= pad; hi += pad;
      const X = (i) => ((i / (ys.length - 1)) * 1000).toFixed(1), Y = (v) => (200 - ((v - lo) / (hi - lo)) * 200).toFixed(1);
      const refPts = ys.map((v, i) => `${X(i)},${Y(v)}`).join(" ");
      const candPts = xs ? xs.map((v, i) => `${X(i)},${Y(Math.max(lo, Math.min(hi, s.a * v + s.b)))}`).join(" ") : "";
      const row = el("div", "can-cmp");
      const plot = el("div", "can-plot");
      plot.innerHTML = `<svg viewBox="0 0 1000 200" preserveAspectRatio="none" aria-hidden="true"><polyline class="ref" points="${refPts}"></polyline><polyline class="cand ${strong ? "ok" : maybe ? "warn" : ""}" points="${candPts}"></polyline></svg>` +
        `<div class="can-legend"><span><i class="ref"></i></span><span><i class="cand ${strong ? "ok" : maybe ? "warn" : ""}"></i>selected bits, scaled</span><span>${st.source === "file" ? "whole capture" : "last 15 seconds"}</span></div>`;
      plot.querySelector(".can-legend span").append(`${f.ref} (logged)`);
      const card = el("div", `can-match ${strong ? "ok" : maybe ? "warn" : ""}`);
      const unit = ref.units || "";
      card.innerHTML = `<span class="can-lbl"></span><b class="num"></b><span class="note">correlation, 1.00 = identical shape</span>` +
        `<div class="num kv"><span>factor</span><span></span><span>offset</span><span></span><span>now</span><span></span></div>`;
      card.querySelector(".can-lbl").textContent = strong ? "Strong match" : maybe ? "Possible match" : "No match";
      card.querySelector("b").textContent = `${s.r < 0 ? "−" : ""}${ar.toFixed(2)}`;
      const kv = card.querySelectorAll(".kv span");
      kv[1].textContent = s.a ? s.a.toPrecision(3) : "—";
      kv[3].textContent = s.b ? s.b.toFixed(2) : "0";
      kv[5].textContent = raw === null ? "—" : `${(s.a * raw + s.b).toFixed(2)} ${unit}`;
      const nameIn = el("input", "field");
      nameIn.type = "text";
      nameIn.value = f.ref.replace(/[^A-Za-z0-9]+(.)?/g, (_, c) => (c ? c.toUpperCase() : "")).replace(/^./, (c) => c.toUpperCase());
      nameIn.setAttribute("aria-label", "Signal name");
      const save = text("button", `btn ${strong ? "primary" : ""}`, "Save signal");
      save.type = "button";
      save.disabled = !maybe;
      save.title = maybe ? "" : "These bits don't follow the logged channel, so there's nothing to save yet";
      save.addEventListener("click", () => {
        const name = (nameIn.value.trim() || `${f.ref}`).replace(/\s+/g, "_");
        st.signals = st.signals.filter((g) => g.name !== name);
        st.signals.push({ name, id: f.id, st: f.start, len: f.len, e: f.order, f: Number(s.a.toPrecision(4)), o: Number(s.b.toFixed(3)),
                          u: unit, min: Number(lo.toFixed(1)), max: Number(hi.toFixed(1)), src: "found", r: Number(ar.toFixed(3)) });
        store(SIG_KEY, st.signals);
        setStatus(`Saved the signal ${name} (${idTxt(f.id)} bits ${f.start}–${f.start + f.len - 1}). See it under Signals.`);
        draw(true);
      });
      card.append(nameIn, save);
      row.append(plot, card);
      cmp.append(row);
      // best matches across the bus (recomputed at most once a second)
      const key = `${f.ref}|${st.source}`;
      if (!st.best || st.bestKey !== key || performance.now() - st.bestAt > 1000) {
        st.best = bestMatches(ref, win);
        st.bestKey = key;
        st.bestAt = performance.now();
      }
      const best = el("section", "can-panel");
      const bh = el("div", "can-sec-head");
      bh.append(text("h3", "", "Best matches on the bus"), text("span", "can-note", `every 8- and 16-bit field${ref.switch ? " and bit" : ""} of every message, ranked against ${f.ref}`));
      best.append(bh);
      for (const c of st.best) {
        const b = el("button", "can-best");
        b.type = "button";
        const col = c.r >= 0.95 ? "ok" : c.r >= 0.7 ? "warn" : "";
        b.innerHTML = `<span class="num id"></span><span class="num"></span><span class="bar"><i class="${col}" style="width:${(c.r * 100).toFixed(0)}%"></i></span><span class="num r ${col}">${c.r.toFixed(2)}</span>`;
        b.querySelector(".id").textContent = idTxt(c.id);
        b.children[1].textContent = c.field;
        b.addEventListener("click", () => { Object.assign(f, { id: c.id, start: c.start, len: c.len, order: c.order }); draw(true); });
        best.append(b);
      }
      main.append(cmp, best);
      wrap.append(list, main);
      body.append(wrap);
      return;
    }
    main.append(cmp);
    wrap.append(list, main);
    body.append(wrap);
  }

  function bestMatches(ref, win) {
    const out = [];
    for (const m of st.msgs.values()) {
      if (m.id >= 0x7df && m.id <= 0x7ef) continue;          // diagnostic requests and replies
      const fields = [];
      for (let by = 0; by < 8; by++) fields.push([by * 8, 8, "be", `byte ${by}`]);
      for (let by = 0; by < 7; by++) {
        fields.push([by * 8, 16, "be", `bytes ${by}–${by + 1} BE`]);
        fields.push([by * 8, 16, "le", `bytes ${by}–${by + 1} LE`]);
      }
      if (ref.switch) for (let i = 0; i < 64; i++) fields.push([i, 1, "be", `byte ${i >> 3} bit ${7 - (i & 7)}`]);
      for (const [start, len, order, field] of fields) {
        if (!m.changed.has(start >> 3) && !m.changed.has((start + len - 1) >> 3)) continue;
        const xs = seriesAt(m, win.ts, start, len, order);
        if (!xs) continue;
        const r = Math.abs(stats(xs, win.ys).r);
        if (Number.isFinite(r) && r > 0) out.push({ id: m.id, start, len, order, field, r });
      }
    }
    return out.sort((a, b) => b.r - a.r).slice(0, 5);
  }

  function seg(opts, cur, pick, label) {
    const g = el("div", "seg-group");
    g.setAttribute("role", "group");
    g.setAttribute("aria-label", label);
    for (const [k, t] of opts) {
      const b = text("button", "seg", t);
      b.type = "button";
      b.setAttribute("aria-pressed", String(k === cur));
      b.addEventListener("click", () => pick(k));
      g.append(b);
    }
    return g;
  }

  // ---- network
  function drawNetwork(body, now) {
    const msgs = sorted().filter((m) => m.id < 0x7df || m.id > 0x7ef);
    const groups = new Map([...MODULES.map(([k]) => [k, []]), ["unassigned", []]]);
    for (const m of msgs) groups.get(nodeOf(m.id)?.node || "unassigned").push(m);
    let tot = 0;
    for (const m of msgs) tot += rateOf(m, now);
    body.replaceChildren();
    const wrap = el("div", "can-net");
    const map = el("section", "can-panel can-map");
    const shown = [...groups].filter(([k, ms]) => ms.length || k === st.net.sel);
    const top = shown.slice(0, Math.ceil(shown.length / 2)), bottom = shown.slice(Math.ceil(shown.length / 2));
    const lane = (arr, cls) => {
      const row = el("div", `can-nodes ${cls}`);
      for (const [k, ms] of arr) {
        const fps = ms.reduce((a, m) => a + rateOf(m, now), 0);
        const how = k === "unassigned" ? "unknown" : (ms.every((m) => nodeOf(m.id)?.how === "confirmed") ? "confirmed" : "likely");
        const b = el("button", `can-node ${how}`);
        b.type = "button";
        b.setAttribute("aria-pressed", String(st.net.sel === k));
        b.innerHTML = `<b></b><span class="num">${ms.length} message${ms.length === 1 ? "" : "s"} · ${Math.round(fps)}/s</span>` +
          `<span class="bar"><i style="width:${tot ? (fps / tot) * 100 : 0}%"></i></span><small></small>`;
        b.querySelector("b").textContent = k === "unassigned" ? "Not assigned" : moduleName(k);
        b.querySelector("small").textContent = k === "unassigned" ? "Not assigned yet" : HOW[how];
        b.addEventListener("click", () => { st.net.sel = k; draw(true); });
        row.append(b);
      }
      return row;
    };
    const bus = el("div", "can-busline", `<span class="num">CAN-H</span><i class="h"></i><i class="l"></i><span class="num">CAN-L</span>`);
    map.append(lane(top, "top"), bus, lane(bottom, "bottom"));
    // detail
    const side = el("aside", "can-side");
    const card = el("div", "td-card");
    const selMsgs = groups.get(st.net.sel) || [];
    const fps = selMsgs.reduce((a, m) => a + rateOf(m, now), 0);
    card.append(text("h3", "", st.net.sel === "unassigned" ? "Not assigned" : moduleName(st.net.sel)));
    const kpi = el("div", "can-kpis num");
    kpi.innerHTML = `<div><b>${selMsgs.length}</b><span>messages</span></div><div><b>${Math.round(fps)}</b><span>frames/s</span></div><div><b>${tot ? Math.round((fps / tot) * 100) : 0}%</b><span>of traffic</span></div>`;
    card.append(kpi);
    for (const m of selMsgs) {
      const row = el("div", "can-assign");
      const node = nodeOf(m.id);
      row.innerHTML = `<span class="num id"></span><span class="lab"></span>`;
      row.querySelector(".id").textContent = idTxt(m.id);
      row.querySelector(".lab").textContent = labelsFor(m.id).join(", ") || `${Math.round(rateOf(m, now))} Hz`;
      const mod = el("select", "field");
      mod.setAttribute("aria-label", `Module that sends ${idTxt(m.id)}`);
      mod.append(new Option("Not assigned", ""));
      for (const [k, name] of MODULES) mod.append(new Option(name, k));
      mod.value = node?.node || "";
      mod.addEventListener("change", () => {
        if (mod.value) st.nodes[hx(m.id, 0)] = { node: mod.value, how: node?.how || "likely" };
        else delete st.nodes[hx(m.id, 0)];
        store(NODE_KEY, st.nodes);
        draw(true);
      });
      row.append(mod);
      if (node) {
        const how = el("select", "field");
        how.setAttribute("aria-label", `How sure you are that ${moduleName(node.node)} sends ${idTxt(m.id)}`);
        for (const [k, t] of Object.entries(HOW)) how.append(new Option(t, k));
        how.value = node.how;
        how.addEventListener("change", () => { st.nodes[hx(m.id, 0)].how = how.value; store(NODE_KEY, st.nodes); draw(true); });
        row.append(how);
      }
      card.append(row);
    }
    card.append(text("p", "can-tip", st.net.sel === "unassigned"
      ? "Assign each message to the module you think sends it. Clues: what its signals are (wheel speeds come from the ABS unit, engine speed from the engine computer) and which messages change together."
      : "CAN frames don't carry the sender, so this is your judgement. Mark it confirmed once a signal in it could only have come from this module."));
    const how = el("div", "td-card can-howkey");
    how.innerHTML = "<h3>How senders are shown</h3><span><i class='confirmed'></i>Confirmed by you</span><span><i class='likely'></i>Likely: your best guess</span><span><i class='unknown'></i>Not assigned yet</span>";
    side.append(card, how);
    wrap.append(map, side);
    body.append(wrap);
  }

  // ---- signals and DBC
  function overlaps() {
    const out = {};
    const bits = (g) => { const s = new Set(); for (let k = 0; k < g.len; k++) s.add(g.st + k); return s; };
    st.signals.forEach((a, i) => st.signals.forEach((b, j) => {
      if (i >= j || a.id !== b.id || a.e !== b.e) return;
      const A = bits(a);
      for (let k = 0; k < b.len; k++) if (A.has(b.st + k)) { out[a.name] = b.name; out[b.name] = a.name; break; }
    }));
    return out;
  }

  const dbcStart = (g) => (g.e === "be" ? (g.st >> 3) * 8 + (7 - (g.st & 7)) : g.st);
  const nodeName = (id) => { const n = nodeOf(id); return n ? moduleName(n.node).replace(/[^A-Za-z0-9]+/g, "_").replace(/_$/, "") : "Vector__XXX"; };

  function dbcText() {
    const inc = st.signals.filter((g) => g.inc !== false);
    const msgs = new Map();
    for (const g of inc) (msgs.get(g.id) || msgs.set(g.id, []).get(g.id)).push(g);
    const nodes = [...new Set([...msgs.keys()].map(nodeName).filter((n) => n !== "Vector__XXX"))];
    const L = ['VERSION ""', "", "NS_ :", "", "BS_:", "", `BU_: ${nodes.join(" ")}`, ""];
    for (const id of [...msgs.keys()].sort((a, b) => a - b)) {
      const dbcId = id > 0x7ff ? (id | 0x80000000) >>> 0 : id;
      L.push(`BO_ ${dbcId} MSG_${hx(id, 0)}: 8 ${nodeName(id)}`);
      for (const g of msgs.get(id)) {
        L.push(` SG_ ${g.name} : ${dbcStart(g)}|${g.len}@${g.e === "be" ? 0 : 1}${g.signed ? "-" : "+"} (${g.f},${g.o}) [${g.min ?? 0}|${g.max ?? 0}] "${g.u || ""}" Vector__XXX`);
      }
      L.push("");
    }
    L.push(`CM_ "Exported from SSM Log Viewer. Signals found by comparing CAN traffic with logged SSM values; not official Subaru definitions.";`);
    return L.join("\n") + "\n";
  }

  /** Read BO_/SG_ lines of a DBC file into signals (marked imported). */
  function parseDbc(txt) {
    const out = [];
    let id = null;
    for (const raw of txt.split(/\r?\n/)) {
      const line = raw.trim();
      let m = line.match(/^BO_\s+(\d+)\s+\w+\s*:\s*\d+\s+(\w+)/);
      if (m) {
        let n = Number(m[1]);
        id = n & 0x80000000 ? n & 0x1fffffff : n;
        if (m[2] && m[2] !== "Vector__XXX") {
          const mod = MODULES.find(([, name]) => name.replace(/[^A-Za-z0-9]+/g, "_").replace(/_$/, "") === m[2]);
          if (mod && !st.nodes[hx(id, 0)]) st.nodes[hx(id, 0)] = { node: mod[0], how: "likely" };
        }
        continue;
      }
      m = line.match(/^SG_\s+(\w+)\s*(?:[Mm]\d*\s*)?:\s*(\d+)\|(\d+)@([01])([+-])\s*\(([^,]+),([^)]+)\)\s*\[([^|]*)\|([^\]]*)\]\s*"([^"]*)"/);
      if (m && id != null) {
        const [, name, start, len, order, sign, f, o, min, max, u] = m;
        const e = order === "0" ? "be" : "le", s = Number(start);
        out.push({ name, id, st: e === "be" ? (s >> 3) * 8 + (7 - (s & 7)) : s, len: Number(len), e, signed: sign === "-",
                   f: Number(f), o: Number(o), min: Number(min), max: Number(max), u, src: "imported" });
      }
    }
    return out;
  }

  function decoded(g) {
    const m = st.msgs.get(g.id);
    const d = m?.d[m.d.length - 1];
    if (!d) return "—";
    let v = value(d, g.st, g.len, g.e);
    if (v === null) return "—";
    if (g.signed && v >= 2 ** (g.len - 1)) v -= 2 ** g.len;
    const x = v * g.f + g.o;
    return `${Math.abs(x) >= 100 ? x.toFixed(0) : x.toFixed(2)} ${g.u || ""}`.trim();
  }

  function drawSignals(body) {
    const conf = overlaps();
    body.replaceChildren();
    const bar = el("div", "can-sigbar");
    const imp = el("label", "btn", "Import DBC…");
    const file = el("input");
    file.type = "file";
    file.accept = ".dbc,text/plain";
    file.hidden = true;
    imp.append(file);
    file.addEventListener("change", async () => {
      const f = file.files[0];
      if (!f) return;
      const got = parseDbc(await f.text());
      const names = new Set(st.signals.map((g) => g.name));
      const added = got.filter((g) => !names.has(g.name));
      st.signals.push(...added);
      store(SIG_KEY, st.signals);
      store(NODE_KEY, st.nodes);
      st.importMsg = `Imported ${f.name}: ${added.length} signal${added.length === 1 ? "" : "s"} added` +
        (got.length - added.length ? `, ${got.length - added.length} skipped (same name as one of yours)` : "") + ".";
      draw(true);
    });
    const exp = text("button", `btn ${st.showDbc ? "primary" : ""}`, "Export DBC");
    exp.type = "button";
    exp.setAttribute("aria-pressed", String(st.showDbc));
    exp.addEventListener("click", () => { st.showDbc = !st.showDbc; draw(true); });
    const inc = st.signals.filter((g) => g.inc !== false).length;
    bar.append(imp, exp, el("span", "spacer"), text("span", "num can-count", `${st.signals.length} signals · ${inc} in export`));
    body.append(bar);
    if (st.importMsg) body.append(text("div", "can-banner", st.importMsg));
    const wrap = el("div", "can-sigs");
    const table = el("section", "can-panel can-sigtable");
    table.append(el("div", "can-sig head", "<span></span><span>Name</span><span>Message</span><span>Start</span><span>Bits</span><span>Order</span><span>Factor</span><span>Offset</span><span>Unit</span><span>Now</span><span>Source</span><span></span>"));
    if (!st.signals.length) {
      table.append(text("p", "can-note pad", "No signals yet. Find one in the signal finder and choose Save signal, or import a DBC file."));
    }
    for (const g of st.signals) {
      const row = el("div", `can-sig${conf[g.name] ? " conflict" : ""}`);
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = g.inc !== false;
      cb.setAttribute("aria-label", `Include ${g.name} in the export`);
      cb.addEventListener("change", () => { g.inc = cb.checked; store(SIG_KEY, st.signals); draw(true); });
      const cbw = el("label", "chk");
      cbw.append(cb);
      const name = el("span", "nm");
      name.append(text("span", "num", g.name));
      if (conf[g.name]) name.append(text("small", "", `Overlaps ${conf[g.name]}`));
      const src = { found: "Signal finder", imported: "Imported", manual: "Added by hand" }[g.src] || g.src;
      const cells = [idTxt(g.id), `byte ${g.st >> 3}.${g.e === "be" ? 7 - (g.st & 7) : g.st & 7}`, String(g.len), g.e === "be" ? "Big" : "Little",
                     String(g.f), String(g.o), g.u || "—", decoded(g)];
      const del = text("button", "btn icon-btn ghost small", "✕");
      del.type = "button";
      del.setAttribute("aria-label", `Delete ${g.name}`);
      del.addEventListener("click", () => { st.signals = st.signals.filter((x) => x !== g); store(SIG_KEY, st.signals); draw(true); });
      row.append(cbw, name, ...cells.map((c, i) => text("span", `num${i === 0 ? " id" : ""}`, c)), text("span", `chip ${g.src}`, src), del);
      table.append(row);
    }
    wrap.append(table);
    if (st.showDbc) {
      const side = el("aside", "can-panel can-dbc");
      const hd = el("div", "can-sec-head");
      const copy = text("button", "btn small", "Copy");
      copy.type = "button";
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(dbcText()); copy.textContent = "Copied"; } catch { copy.textContent = "Copy failed"; }
        setTimeout(() => { copy.textContent = "Copy"; }, 1500);
      });
      const save = text("button", "btn small primary", "Save file");
      save.type = "button";
      save.addEventListener("click", () => download(new Blob([dbcText()], { type: "text/plain" }), "subaru-can.dbc"));
      hd.append(text("h3", "", "subaru-can.dbc"), copy, save);
      side.append(hd, text("pre", "num", dbcText()),
        text("p", "can-note", "Message senders come from the network map. Start bits use DBC numbering, which differs between byte orders, so the file reads correctly in other tools."));
      wrap.append(side);
    }
    body.append(wrap);
  }

  // ---------------------------------------------------------------- wiring
  for (const b of $("can-nav").children) b.addEventListener("click", () => { st.view = b.dataset.view; draw(true); });
  $("can-x").addEventListener("click", () => $("can-dialog").close());
  $("can-dialog").addEventListener("close", () => cancelAnimationFrame(st.raf));
  $("can-listen").addEventListener("click", () => (st.listening ? stopListening() : startListening()));
  $("can-pause").addEventListener("click", () => { st.paused = !st.paused; draw(true); });
  $("can-save").addEventListener("click", saveCapture);
  $("can-file").addEventListener("change", (e) => { openFiles(e.target.files); e.target.value = ""; });

  return { open, value, parseDbc, dbcText, _st: st };
})();
