"""Self-contained status page served at http://localhost:<status_port>."""

STATUS_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ES-9 Bridge</title>
<style>
  :root {
    color-scheme: dark;
    --page:           #0d0d0d;
    --surface:        #1a1a19;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --muted:          #898781;
    --grid:           #2c2c2a;
    --baseline:       #383835;
    --border:         rgba(255,255,255,0.10);
    --series-1:       #3987e5;
    --series-1-dim:   #184f95;
    --good:           #0ca30c;
    --warning:        #fab219;
    --serious:        #ec835a;
    --critical:       #d03b3b;
  }
  :root[data-theme="light"] {
    color-scheme: light;
    --page:           #f9f9f7;
    --surface:        #fcfcfb;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --muted:          #898781;
    --grid:           #e1e0d9;
    --baseline:       #c3c2b7;
    --border:         rgba(11,11,11,0.10);
    --series-1:       #2a78d6;
    --series-1-dim:   #9ec5f4;
  }

  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px;
    background: var(--page); color: var(--text-primary);
    font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 1040px; margin: 0 auto; }

  header { display: flex; align-items: flex-start; gap: 16px; margin-bottom: 20px; }
  h1 { font-size: 19px; margin: 0 0 4px; font-weight: 600; letter-spacing: -0.01em; }
  .sub { color: var(--text-secondary); font-size: 13px; }
  .spacer { flex: 1; }

  button {
    font: inherit; font-size: 12px; padding: 6px 12px; cursor: pointer;
    background: var(--surface); color: var(--text-secondary);
    border: 1px solid var(--border); border-radius: 6px;
  }
  button:hover { color: var(--text-primary); }

  .pill {
    display: inline-flex; align-items: center; gap: 7px;
    padding: 5px 12px; border-radius: 999px; font-size: 12px; font-weight: 600;
    border: 1px solid var(--border); background: var(--surface);
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }

  .tiles {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 2px; margin-bottom: 20px;
  }
  .tile {
    background: var(--surface); border: 1px solid var(--border);
    padding: 12px 14px; border-radius: 8px;
  }
  .tile .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
  .tile .value { font-size: 24px; font-weight: 600; margin-top: 3px; letter-spacing: -0.02em; }
  .tile .note  { font-size: 11px; color: var(--text-secondary); margin-top: 2px; }

  section { background: var(--surface); border: 1px solid var(--border);
            border-radius: 8px; padding: 16px 18px; margin-bottom: 20px; }
  h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em;
       color: var(--muted); margin: 0 0 14px; font-weight: 600; }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; font-size: 11px; color: var(--muted); font-weight: 600;
       padding: 0 8px 8px 0; text-transform: uppercase; letter-spacing: .04em; }
  td { padding: 7px 8px 7px 0; border-top: 1px solid var(--grid);
       font-variant-numeric: tabular-nums; }
  td.name { font-variant-numeric: normal; font-weight: 500; }
  td.num { text-align: right; color: var(--text-secondary); }
  .idx { color: var(--muted); font-size: 12px; margin-right: 7px; }

  input, select {
    font: inherit; font-size: 12px;
    background: var(--page); color: var(--text-primary);
    border: 1px solid var(--border); border-radius: 4px;
    padding: 3px 6px;
  }
  input:focus, select:focus { outline: 1px solid var(--series-1); border-color: var(--series-1); }
  input.name-in { width: 92px; font-weight: 500; }
  input.num-in { width: 58px; text-align: right; font-variant-numeric: tabular-nums; }
  select.mode-in { width: 86px; }
  tr.cfg { display: none; }
  tr.live.editing + tr.cfg { display: table-row; }
  tr.cfg td { border-top: none; padding: 2px 0 14px 0; }
  tr.live td { padding-top: 10px; padding-bottom: 10px; }
  tr.live.editing td { padding-bottom: 4px; }
  .view-name { font-weight: 500; }
  .mode-badge {
    color: var(--muted); font-size: 11px; margin-left: 8px;
    text-transform: uppercase; letter-spacing: .04em;
  }
  tr.live.editing .view-name,
  tr.live.editing .mode-badge { display: none; }
  tr.live:not(.editing) .name-in { display: none; }
  td.editcell { text-align: right; width: 64px; padding-right: 0; }
  .editbtn { padding: 4px 10px; min-width: 52px; }
  .knobs {
    display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center;
    color: var(--text-secondary); font-size: 12px;
  }
  .knobs label { display: inline-flex; align-items: center; gap: 5px; cursor: help; }
  .knobs .k {
    color: var(--muted); font-size: 11px; text-transform: uppercase;
    letter-spacing: .04em; border-bottom: 1px dotted var(--muted);
  }
  .save-flash { color: var(--good); font-size: 11px; min-width: 48px; }

  /* meter: min-max span with a mean tick, a zero reference and a threshold marker */
  .meter { position: relative; height: 18px; width: 100%; min-width: 180px; overflow: hidden; }
  .track { position: absolute; inset: 6px 0; background: var(--grid); border-radius: 3px; }
  /* marks live inset so a value pinned at 0% or 100% never clips */
  .marks { position: absolute; inset: 0 3px; }
  .zero  { position: absolute; top: 4px; height: 10px; width: 1px; background: var(--baseline); }
  .span  { position: absolute; left: 0; top: 6px; height: 6px; width: 100%;
           background: var(--series-1-dim); border-radius: 3px;
           transform-origin: 0 50%; will-change: transform; }
  .mean-slot { position: absolute; inset: 0; will-change: transform; pointer-events: none; }
  .mean  { position: absolute; left: 0; top: 2px; height: 14px; width: 3px; margin-left: -1.5px;
           background: var(--series-1); border-radius: 2px;
           box-shadow: 0 0 0 2px var(--surface); }
  .thr   { position: absolute; top: 0; height: 18px; width: 1px; background: var(--muted); opacity: .55; }
  .thr::after { content: ""; position: absolute; top: 0; left: -1.5px;
                width: 4px; height: 4px; border-radius: 50%; background: var(--muted); }

  .gate { display: inline-flex; align-items: center; gap: 6px; font-size: 12px;
          color: var(--text-secondary); font-variant-numeric: normal; }

  .log { font-size: 12px; }
  .log td { font-variant-numeric: tabular-nums; }
  .log .reason { color: var(--text-secondary); font-variant-numeric: normal; }
  .empty { color: var(--muted); font-size: 13px; padding: 6px 0; }

  .sim-banner {
    background: var(--surface); border: 1px solid var(--warning);
    border-radius: 8px; padding: 10px 14px; margin-bottom: 16px;
    font-size: 13px; display: flex; align-items: center; gap: 9px;
  }
  .foot { color: var(--muted); font-size: 12px; margin-top: 22px; }
  code { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 12px; }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div>
      <h1>ES-9 &rarr; OSC Bridge</h1>
      <div class="sub" id="devline">connecting&hellip;</div>
    </div>
    <div class="spacer"></div>
    <span class="pill" id="health"><span class="dot"></span><span id="healthtext">&mdash;</span></span>
    <button id="themebtn" title="Toggle light/dark">&#9681;</button>
    <button id="resetbtn" title="Force a stream reopen">Reopen stream</button>
  </header>

  <div id="simbanner"></div>

  <div class="tiles" id="tiles"></div>

  <section>
    <h2>Inputs</h2>
    <table>
      <thead><tr>
        <th style="width:160px">Channel</th>
        <th style="width:38%">Level</th>
        <th style="width:74px" class="num">Volts</th>
        <th style="width:74px" class="num">Out</th>
        <th style="width:96px">Gate</th>
        <th style="width:70px" class="num">Trigs</th>
        <th></th>
      </tr></thead>
      <tbody id="chans"></tbody>
    </table>
  </section>

  <section>
    <h2>Recovery log</h2>
    <div id="log"></div>
  </section>

  <div class="foot" id="foot"></div>
</div>

<script>
const $ = (id) => document.getElementById(id);

let theme = "dark";
$("themebtn").onclick = () => {
  theme = theme === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", theme);
};
$("resetbtn").onclick = () => fetch("/api/reset");

const MODES = ["lfo", "clock", "env", "audio"];
let presets = {};
let builtN = 0;
const dirty = new Set();
const timers = {};

function statusColor(kind) {
  return getComputedStyle(document.documentElement).getPropertyValue("--" + kind).trim();
}

function tile(id, label, value, note) {
  return `<div class="tile" id="tile-${id}"><div class="label">${label}</div>
          <div class="value">${value}</div>
          <div class="note">${note || "&nbsp;"}</div></div>`;
}

function setTile(id, value, note) {
  const t = $("tile-" + id);
  if (!t) return;
  t.querySelector(".value").textContent = value;
  if (note !== undefined) t.querySelector(".note").innerHTML = note || "&nbsp;";
}

function fmtUptime(s) {
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"
  }[c]));
}

function rowHtml(c) {
  const opts = MODES.map(m =>
    `<option value="${m}"${m === c.mode ? " selected" : ""}>${m}</option>`).join("");
  const tip = {
    mode: "Preset for this jack. Fills the knobs below; you can still tweak them.",
    in: "Voltage range on the jack. Mapped onto Out. Match the module (e.g. \u00b15 V LFO, 0\u20138 V envelope).",
    out: "OSC value range after mapping. 0\u20131 is what TouchDesigner usually wants.",
    smooth: "One-pole lag at the OSC rate. 0 = none, 0.9 = heavy. Leave at 0 for clocks and triggers.",
    thr: "Gate opens above this voltage. Detected at audio rate, so short pulses survive.",
    hyst: "Dead band around the threshold. Stops a slow edge from chattering open/closed.",
    lock: "Minimum milliseconds between triggers. Caps the event rate if a signal sits on the threshold.",
    events: "Send /gate and /trig for this channel. Turn off for audio so it does not spam triggers.",
    minmax: "Also emit /es9/N/minmax as [min, max] volts each OSC frame.",
    clamp: "Clip the OSC value to the Out range instead of allowing overshoot.",
    on: "Include this channel in OSC output.",
    name: "Label only. OSC address stays /es9/" + c.n + ".",
  };
  return `<tr class="live" data-ch="${c.n}">
      <td class="name">
        <span class="idx">${c.n}</span>
        <span class="view-name">${esc(c.name)}</span>
        <span class="mode-badge">${esc(c.mode)}</span>
        <input class="name-in" data-k="name" value="${esc(c.name)}" spellcheck="false"
               title="${esc(tip.name)}">
        <span class="cal-dot" hidden> \u25cf</span>
      </td>
      <td><div class="meter">
            <div class="track"></div>
            <div class="marks">
              <div class="zero" hidden></div>
              <div class="span"></div>
              <div class="mean-slot"><div class="mean"></div></div>
              <div class="thr"></div>
            </div>
          </div></td>
      <td class="num volts">0</td>
      <td class="num outv">0</td>
      <td><span class="gate"><span class="dot"></span><span class="gtxt">closed</span></span></td>
      <td class="num trigs">0</td>
      <td class="editcell"><button class="editbtn" type="button">Edit</button></td>
    </tr>
    <tr class="cfg" data-ch="${c.n}">
      <td colspan="7"><div class="knobs">
        <label title="${esc(tip.mode)}"><span class="k">mode</span>
          <select class="mode-in" data-k="mode">${opts}</select></label>
        <label title="${esc(tip.in)}"><span class="k">in</span>
          <input class="num-in" data-k="in_lo" type="number" step="0.1"> &ndash;
          <input class="num-in" data-k="in_hi" type="number" step="0.1"> V</label>
        <label title="${esc(tip.out)}"><span class="k">out</span>
          <input class="num-in" data-k="out_lo" type="number" step="0.1"> &ndash;
          <input class="num-in" data-k="out_hi" type="number" step="0.1"></label>
        <label title="${esc(tip.smooth)}"><span class="k">smooth</span>
          <input class="num-in" data-k="smooth" type="number" min="0" max="0.99" step="0.05"></label>
        <label title="${esc(tip.thr)}"><span class="k">thr</span>
          <input class="num-in" data-k="gate_threshold" type="number" step="0.1"></label>
        <label title="${esc(tip.hyst)}"><span class="k">hyst</span>
          <input class="num-in" data-k="gate_hysteresis" type="number" step="0.1"></label>
        <label title="${esc(tip.lock)}"><span class="k">lock ms</span>
          <input class="num-in" data-k="retrigger_lockout_ms" type="number" min="0" step="0.5"></label>
        <label title="${esc(tip.events)}"><input type="checkbox" data-k="events"> events</label>
        <label title="${esc(tip.minmax)}"><input type="checkbox" data-k="send_minmax"> minmax</label>
        <label title="${esc(tip.clamp)}"><input type="checkbox" data-k="clamp"> clamp</label>
        <label title="${esc(tip.on)}"><input type="checkbox" data-k="enabled"> on</label>
        <span class="save-flash" data-k="flash"></span>
      </div></td>
    </tr>`;
}

function fillForm(n, c) {
  const live = document.querySelector(`tr.live[data-ch="${n}"]`);
  const cfg = document.querySelector(`tr.cfg[data-ch="${n}"]`);
  if (!live || !cfg) return;
  const set = (root, k, v) => {
    const el = root.querySelector(`[data-k="${k}"]`);
    if (!el || el === document.activeElement || dirty.has(n + ":" + k)) return;
    if (el.type === "checkbox") el.checked = !!v;
    else el.value = v;
  };
  set(live, "name", c.name);
  set(cfg, "mode", c.mode);
  if (!live.classList.contains("editing") && !dirty.has(n + ":name")) {
    live.querySelector(".view-name").textContent = c.name;
  }
  live.querySelector(".mode-badge").textContent = c.mode;
  set(cfg, "in_lo", c.in_range[0]);
  set(cfg, "in_hi", c.in_range[1]);
  set(cfg, "out_lo", c.out_range[0]);
  set(cfg, "out_hi", c.out_range[1]);
  set(cfg, "smooth", c.smooth);
  set(cfg, "gate_threshold", c.gate_threshold);
  set(cfg, "gate_hysteresis", c.gate_hysteresis);
  set(cfg, "retrigger_lockout_ms", c.retrigger_lockout_ms);
  set(cfg, "events", c.events);
  set(cfg, "send_minmax", c.send_minmax);
  set(cfg, "clamp", c.clamp);
  set(cfg, "enabled", c.enabled);
}

function readChannel(n) {
  const live = document.querySelector(`tr.live[data-ch="${n}"]`);
  const cfg = document.querySelector(`tr.cfg[data-ch="${n}"]`);
  const v = (root, k) => root.querySelector(`[data-k="${k}"]`);
  const num = (root, k) => parseFloat(v(root, k).value);
  return {
    name: v(live, "name").value.trim() || ("ch" + n),
    mode: v(cfg, "mode").value,
    in_range: [num(cfg, "in_lo"), num(cfg, "in_hi")],
    out_range: [num(cfg, "out_lo"), num(cfg, "out_hi")],
    smooth: num(cfg, "smooth"),
    gate_threshold: num(cfg, "gate_threshold"),
    gate_hysteresis: num(cfg, "gate_hysteresis"),
    retrigger_lockout_ms: num(cfg, "retrigger_lockout_ms"),
    events: v(cfg, "events").checked,
    send_minmax: v(cfg, "send_minmax").checked,
    clamp: v(cfg, "clamp").checked,
    enabled: v(cfg, "enabled").checked,
  };
}

function applyPresetToForm(n, mode) {
  const p = presets[mode];
  if (!p) return;
  const cfg = document.querySelector(`tr.cfg[data-ch="${n}"]`);
  const live = document.querySelector(`tr.live[data-ch="${n}"]`);
  cfg.querySelector("[data-k=mode]").value = mode;
  live.querySelector(".mode-badge").textContent = mode;
  cfg.querySelector("[data-k=in_lo]").value = p.in_range[0];
  cfg.querySelector("[data-k=in_hi]").value = p.in_range[1];
  cfg.querySelector("[data-k=out_lo]").value = p.out_range[0];
  cfg.querySelector("[data-k=out_hi]").value = p.out_range[1];
  cfg.querySelector("[data-k=smooth]").value = p.smooth;
  cfg.querySelector("[data-k=gate_threshold]").value = p.gate_threshold;
  cfg.querySelector("[data-k=gate_hysteresis]").value = p.gate_hysteresis;
  cfg.querySelector("[data-k=retrigger_lockout_ms]").value = p.retrigger_lockout_ms;
  cfg.querySelector("[data-k=events]").checked = p.events;
  cfg.querySelector("[data-k=send_minmax]").checked = p.send_minmax;
  cfg.querySelector("[data-k=clamp]").checked = p.clamp;
}

async function saveChannel(n, extra) {
  const body = Object.assign(readChannel(n), extra || {});
  const flash = document.querySelector(`tr.cfg[data-ch="${n}"] [data-k=flash]`);
  try {
    const r = await fetch("/api/channel/" + n, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.status);
    ["name","mode","in_lo","in_hi","out_lo","out_hi","smooth",
     "gate_threshold","gate_hysteresis","retrigger_lockout_ms",
     "events","send_minmax","clamp","enabled"].forEach(k => dirty.delete(n + ":" + k));
    fillForm(n, j);
    if (pageState) {
      const i = pageState.channels.findIndex(c => c.n === n);
      if (i >= 0) Object.assign(pageState.channels[i], j);
    }
    if (flash) {
      flash.textContent = "saved";
      setTimeout(() => { if (flash.textContent === "saved") flash.textContent = ""; }, 900);
    }
  } catch (e) {
    if (flash) flash.textContent = "save failed";
  }
}

function scheduleSave(n, key) {
  dirty.add(n + ":" + key);
  clearTimeout(timers[n]);
  timers[n] = setTimeout(() => saveChannel(n), 350);
}

function bindRows() {
  $("chans").querySelectorAll("tr.live, tr.cfg").forEach(tr => {
    const n = +tr.dataset.ch;
    tr.querySelectorAll("input, select").forEach(el => {
      const k = el.dataset.k;
      el.addEventListener("input", () => {
        if (k === "mode") {
          applyPresetToForm(n, el.value);
          ["mode","in_lo","in_hi","out_lo","out_hi","smooth",
           "gate_threshold","gate_hysteresis","retrigger_lockout_ms",
           "events","send_minmax","clamp"].forEach(x => dirty.add(n + ":" + x));
          saveChannel(n, {apply_preset: true, mode: el.value});
          return;
        }
        scheduleSave(n, k);
      });
      el.addEventListener("change", () => {
        if (k === "mode") return;
        scheduleSave(n, k);
      });
    });
  });
  $("chans").querySelectorAll("tr.live .editbtn").forEach(btn => {
    btn.addEventListener("click", () => {
      const live = btn.closest("tr.live");
      const on = !live.classList.contains("editing");
      live.classList.toggle("editing", on);
      btn.textContent = on ? "Done" : "Edit";
      if (on) {
        const inp = live.querySelector("[data-k=name]");
        if (inp) inp.focus();
      } else {
        live.querySelector(".view-name").textContent =
          live.querySelector("[data-k=name]").value.trim()
          || ("ch" + live.dataset.ch);
      }
    });
  });
}

const rowCache = {};

function cacheRow(n) {
  const tr = document.querySelector(`tr.live[data-ch="${n}"]`);
  if (!tr) return null;
  rowCache[n] = {
    tr,
    zero: tr.querySelector(".zero"),
    spanEl: tr.querySelector(".span"),
    meanSlot: tr.querySelector(".mean-slot"),
    thr: tr.querySelector(".thr"),
    volts: tr.querySelector(".volts"),
    outv: tr.querySelector(".outv"),
    gateDot: tr.querySelector(".gate .dot"),
    gtxt: tr.querySelector(".gtxt"),
    trigs: tr.querySelector(".trigs"),
    cal: tr.querySelector(".cal-dot"),
  };
  return rowCache[n];
}

function oscVolts(c) {
  const [lo, hi] = c.in_range;
  const [olo, ohi] = c.out_range;
  const t = (c.out - olo) / ((ohi - olo) || 1e-9);
  return lo + t * (hi - lo);
}

function updateMeter(c) {
  const row = rowCache[c.n] || cacheRow(c.n);
  if (!row) return;
  const [lo, hi] = c.in_range;
  const span = (hi - lo) || 1e-9;
  const pct = (v) => Math.max(0, Math.min(100, ((v - lo) / span) * 100));
  let a = pct(c.min_v), b = pct(c.max_v);
  const m = pct(c.needle);
  if (c.value === "rms") {
    a = pct(0); b = pct(c.volts);
  }
  const left = Math.min(a, b);
  const width = Math.max(1.5, Math.abs(b - a));
  row.zero.hidden = !(lo < 0 && hi > 0);
  if (!row.zero.hidden) row.zero.style.left = pct(0) + "%";
  row.spanEl.style.transform = `translateX(${left}%) scaleX(${width / 100})`;
  row.meanSlot.style.transform = `translateX(${m}%)`;
  row.thr.hidden = !c.events;
  if (c.events) row.thr.style.left = pct(c.threshold) + "%";
}

function updateLabels(c) {
  const row = rowCache[c.n] || cacheRow(c.n);
  if (!row) return;
  row.volts.textContent = c.volts.toFixed(3);
  row.outv.textContent = c.out.toFixed(3);
  const gateOn = c.gate === 1;
  row.gateDot.style.background = gateOn ? statusColor("good") : "var(--baseline)";
  row.gtxt.textContent = c.events ? (gateOn ? "open" : "closed") : "off";
  row.trigs.textContent = c.trigs.toLocaleString();
  row.cal.hidden = !c.calibrating;
}

function ensureRows(d) {
  if (builtN === d.channels.length) return;
  $("chans").innerHTML = d.channels.map(rowHtml).join("");
  bindRows();
  d.channels.forEach(c => {
    fillForm(c.n, c);
    cacheRow(c.n);
  });
  builtN = d.channels.length;
}

function setHealth(ok, label) {
  const kind = ok ? "good" : "critical";
  $("health").querySelector(".dot").style.background = statusColor(kind);
  $("healthtext").textContent = label || (ok ? "Streaming" : "Stalled");
}

function renderChrome(d) {
  if (d.presets) presets = d.presets;
  const dev = d.device || {};
  $("devline").innerHTML =
    `${dev.device || "\u2014"} &middot; ${dev.hostapi || "\u2014"} &middot; ` +
    `<strong>${dev.samplerate ? (+dev.samplerate).toLocaleString() + " Hz" : "\u2014"}</strong> &middot; ` +
    `block ${dev.blocksize ?? "\u2014"} &middot; ${dev.latency_ms ?? "\u2014"} ms in`;

  setHealth(d.healthy);

  let banners = "";
  if (d.simulate) {
    banners += `<div class="sim-banner"><span class="dot" style="background:${statusColor("warning")}"></span>
      <span><strong>Simulate mode</strong> &mdash; synthetic signals, the ES-9 is not being read.</span></div>`;
  } else if (dev.bitexact === false) {
    banners += `<div class="sim-banner" style="border-color:${statusColor("critical")}">
      <span class="dot" style="background:${statusColor("critical")}"></span>
      <span><strong>Not bit-exact</strong> &mdash; ${dev.hostapi} routes through the Windows
      audio engine, which resamples and mixes. These values are not your true CV.
      Run <code>--check</code> to find an exclusive path.</span></div>`;
  }
  $("simbanner").innerHTML = banners;

  if (!$("tile-uptime")) {
    $("tiles").innerHTML = [
      tile("uptime", "Uptime", fmtUptime(d.uptime_s), `since start`),
      tile("recoveries", "Recoveries", d.recovery_count,
           d.recovery_count === 0 ? "no stalls detected" : "auto-reopened"),
      tile("age", "Callback age", d.callback_age_ms.toFixed(0) + " ms", "time since last audio block"),
      tile("xruns", "Xruns", d.xruns, d.last_status ? d.last_status : "driver over/underflows"),
      tile("chatter", "Chatter held", (d.suppressed ?? 0).toLocaleString(), "edges inside retrigger lockout"),
      tile("osc", "OSC frames", d.frames_sent.toLocaleString(), `${d.osc_rate} Hz \u00d7 ${d.destinations.length} dest`),
    ].join("");
  } else {
    setTile("uptime", fmtUptime(d.uptime_s));
    setTile("recoveries", d.recovery_count,
            d.recovery_count === 0 ? "no stalls detected" : "auto-reopened");
    setTile("osc", d.frames_sent.toLocaleString(),
            `${d.osc_rate} Hz \u00d7 ${d.destinations.length} dest`);
  }

  ensureRows(d);
  d.channels.forEach(c => fillForm(c.n, c));

  const rec = d.recoveries || [];
  $("log").innerHTML = rec.length === 0
    ? `<div class="empty">No stalls yet. Every automatic reopen will be timestamped here
       &mdash; the pattern over a few sessions tells you whether this is clock drift
       (regular intervals), USB (clustered), or something else.</div>`
    : `<table class="log"><thead><tr>
         <th style="width:36px">#</th><th style="width:160px">Time</th>
         <th style="width:96px" class="num">At uptime</th>
         <th style="width:88px" class="num">Recovered</th><th>Detected by</th>
       </tr></thead><tbody>` +
       rec.map(r => `<tr>
         <td>${r.n}</td><td>${r.time}</td>
         <td class="num">${fmtUptime(r.uptime_s)}</td>
         <td class="num">${r.ok ? (r.recover_ms + " ms") : "\u2014"}</td>
         <td class="reason">${r.ok ? "" : "<strong>reopen failed:</strong> "}${r.error || r.reason}</td>
       </tr>`).join("") + `</tbody></table>`;

  $("foot").innerHTML =
    `Sending <code>${d.address_prefix}/1</code>&hellip;<code>${d.address_prefix}/${d.channels.length}</code> ` +
    `to ${d.destinations.map(x => `<code>${x}</code>`).join(", ")} at ${d.osc_rate} Hz. ` +
    `Meters follow the same ${d.osc_rate} Hz stream (display only). ` +
    `Full scale ${d.full_scale_volts} V. ` +
    `Watchdog ${d.watchdog_enabled ? "on" : "OFF"}${d.watchdog_enabled && !d.staleness_enabled ? " (staleness check disabled)" : ""}. ` +
    `${d.messages_sent.toLocaleString()} messages sent. ` +
    `Channel edits save to the config file.`;
}

function applyLive(d, cfgChannels) {
  d.channels.forEach(live => {
    const cfg = cfgChannels.find(c => c.n === live.n);
    if (!cfg) return;
    const merged = Object.assign({}, cfg, live);
    const needle = oscVolts(merged);
    let t = targets[live.n];
    if (!t) {
      targets[live.n] = {
        volts: live.volts, min_v: live.min_v, max_v: live.max_v,
        out: live.out, needle, gate: live.gate, trigs: live.trigs,
        calibrating: live.calibrating,
      };
    } else {
      t.volts = live.volts; t.min_v = live.min_v; t.max_v = live.max_v;
      t.out = live.out; t.needle = needle; t.gate = live.gate;
      t.trigs = live.trigs; t.calibrating = live.calibrating;
    }
  });
  pendingTiles = d;
}

function shownOrSeed(n, t) {
  let s = shown[n];
  if (!s) {
    s = shown[n] = {
      volts: t.volts, min_v: t.min_v, max_v: t.max_v,
      out: t.out, needle: t.needle,
    };
  }
  return s;
}

function follow(cur, dst, k) {
  return cur + (dst - cur) * k;
}

let oscRate = 60;
let pageState = null;
let pendingLive = null;
let pendingTiles = null;
let liveInFlight = false;
let stateInFlight = false;
let lastLiveAt = 0;
let lastStateAt = 0;
let lastLabelAt = 0;
let lastPaint = 0;
const targets = {};
const shown = {};
const STATE_MS = 1000;
const LABEL_MS = 100;

function onDisconnect() {
  $("healthtext").textContent = "No bridge";
  $("health").querySelector(".dot").style.background = statusColor("critical");
}

async function fetchJson(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

function paint(now) {
  requestAnimationFrame(paint);
  const liveMs = 1000 / Math.max(30, Math.min(oscRate || 60, 60));
  if (!liveInFlight && now - lastLiveAt >= liveMs) {
    lastLiveAt = now;
    liveInFlight = true;
    fetchJson("/api/live").then(d => {
      oscRate = d.osc_rate || oscRate;
      pendingLive = d;
    }).catch(onDisconnect).finally(() => { liveInFlight = false; });
  }
  if (!stateInFlight && (pageState === null || now - lastStateAt >= STATE_MS)) {
    lastStateAt = now;
    stateInFlight = true;
    fetchJson("/api/state").then(d => {
      oscRate = d.osc_rate || oscRate;
      pageState = d;
      renderChrome(d);
    }).catch(onDisconnect).finally(() => { stateInFlight = false; });
  }
  if (pendingLive && pageState) {
    const d = pendingLive;
    pendingLive = null;
    applyLive(d, pageState.channels);
  }

  const dt = lastPaint ? Math.min(0.05, (now - lastPaint) / 1000) : 0.016;
  lastPaint = now;
  // Needle tracks OSC (already smoothed) with a short display lag so HTTP
  // jitter does not stair-step. Span uses slower ballistics; extrema chatter.
  const needleK = 1 - Math.exp(-dt * oscRate * 3);
  const spanK = 1 - Math.exp(-dt * 14);
  if (pageState) {
    pageState.channels.forEach(c => {
      const t = targets[c.n];
      if (!t) return;
      const s = shownOrSeed(c.n, t);
      s.needle = follow(s.needle, t.needle, needleK);
      s.volts = follow(s.volts, t.volts, needleK);
      s.out = follow(s.out, t.out, needleK);
      s.min_v = follow(s.min_v, t.min_v, spanK);
      s.max_v = follow(s.max_v, t.max_v, spanK);
      updateMeter(Object.assign({}, c, s, {
        needle: s.needle, min_v: s.min_v, max_v: s.max_v, volts: s.volts,
      }));
    });
  }

  if (now - lastLabelAt >= LABEL_MS && pageState) {
    lastLabelAt = now;
    if (pendingTiles) {
      const d = pendingTiles;
      setHealth(d.healthy);
      setTile("age", d.callback_age_ms.toFixed(0) + " ms");
      setTile("xruns", d.xruns, d.last_status ? d.last_status : "driver over/underflows");
      setTile("chatter", (d.suppressed ?? 0).toLocaleString());
      setTile("osc", d.frames_sent.toLocaleString());
    }
    pageState.channels.forEach(c => {
      const t = targets[c.n];
      const s = shown[c.n];
      if (!t || !s) return;
      updateLabels(Object.assign({}, c, t, s));
    });
  }
}
requestAnimationFrame(paint);
</script>
</body>
</html>
"""
