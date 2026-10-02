// ARTHUR stats page: reads GET /metrics/summary every 5 s and redraws.
// Everything is inserted with textContent - names come from the server and are never
// treated as HTML.
"use strict";

const REFRESH_MS = 5000;
const $ = (id) => document.getElementById(id);
let paused = false;
let timer = null;

// ---------- formatting ----------
function compact(n) {
  if (n === null || n === undefined) return "–";
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e4) return `${(n / 1e3).toFixed(1)}K`;
  return n.toLocaleString("en");
}

function duration(ms) {
  if (ms === null || ms === undefined) return ["–", ""];
  if (ms < 1) return ["<1", "ms"];
  if (ms < 1000) return [String(Math.round(ms)), "ms"];
  if (ms < 60000) return [(ms / 1000).toFixed(ms < 10000 ? 1 : 0), "s"];
  return [(ms / 60000).toFixed(1), "min"];
}

function uptime(seconds) {
  if (seconds < 60) return [String(seconds), "s"];
  if (seconds < 3600) return [String(Math.floor(seconds / 60)), "min"];
  const hours = Math.floor(seconds / 3600);
  if (hours < 48) return [`${hours} h ${Math.floor((seconds % 3600) / 60)}`, "min"];
  return [String(Math.floor(hours / 24)), "days"];
}

// ---------- building blocks ----------
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function tile(label, value, unit = "", note = "", alert = false) {
  const box = el("div", alert ? "tile alert" : "tile");
  box.append(el("div", "label", label));
  const number = el("div", "value", value);
  if (unit) number.append(el("span", "unit", unit));
  box.append(number);
  if (note) box.append(el("div", "note", note));
  return box;
}

function numberCell(n) {
  return el("td", n ? "num" : "num zero", compact(n || 0));
}

function durationCell(ms) {
  const [value, unit] = duration(ms);
  return el("td", "num", unit ? `${value} ${unit}` : value);
}

function barCell(count, max, label) {
  const cell = el("td");
  const wrap = el("div", "bar-cell");
  const track = el("div", "bar-track");
  const bar = el("div", "bar");
  bar.style.width = `${max ? Math.max((count / max) * 100, 1) : 0}%`; // set via CSSOM (CSP-safe)
  bar.title = `${label}: ${count.toLocaleString("en")}`;
  track.append(bar);
  wrap.append(track, el("span", "bar-value", compact(count)));
  cell.append(wrap);
  return cell;
}

function fillTable(table, rows) {
  table.tBodies[0].replaceChildren(...rows);
  table.hidden = rows.length === 0;
}

// ---------- sections ----------
function drawOverview(s) {
  const turns = Object.entries(s.chat_turns);
  const total = turns.reduce((sum, [, n]) => sum + n, 0);
  const failed = turns.filter(([key]) => key.endsWith(":error")).reduce((sum, [, n]) => sum + n, 0);
  const blocked = Object.values(s.security_blocks).reduce((sum, n) => sum + n, 0);
  const [up, upUnit] = uptime(s.uptime_seconds);
  $("overview").replaceChildren(
    tile("Running for", up, upUnit),
    tile("Open tabs", compact(s.open_tabs)),
    tile("Messages answered", compact(total), "", failed ? `⚠ ${failed} failed` : "none failed", failed > 0),
    tile("Tool calls", compact(s.tools.reduce((sum, t) => sum + t.calls, 0))),
    tile("Refused for safety", compact(blocked)),
    tile("Reminders delivered", compact(s.reminders_delivered)),
  );
}

function drawModel(s) {
  const tiles = [];
  for (const m of s.models) {
    const [typical, typicalUnit] = duration(m.p50_ms);
    const [slow, slowUnit] = duration(m.p95_ms);
    const [first, firstUnit] = duration(m.first_token.p50_ms);
    tiles.push(
      tile("Model", m.model),
      tile("Calls", compact(m.calls), "", m.errors ? `⚠ ${m.errors} failed or stopped` : "all completed", m.errors > 0),
      tile("Typical call", typical, typicalUnit, "half were faster (p50)"),
      tile("Slow call", slow, slowUnit, "95 % were faster (p95)"),
      tile("First words", first, firstUnit, "typical, streamed answers"),
      tile("Text generated", compact(m.completion_tokens), "tokens"),
    );
    // Where the time goes (Phase 24): how much the model must read, and how long that takes.
    const [read, readUnit] = duration(m.prompt_read.p50_ms);
    const [readSlow, readSlowUnit] = duration(m.prompt_read.p95_ms);
    const [load, loadUnit] = duration(m.load.p95_ms);
    tiles.push(
      tile("Prompt size", compact(m.prompt_tokens_typical), "tokens", "instructions + tools + chat"),
      tile("Reading the prompt", read, readUnit, `typical; slow ${readSlow} ${readSlowUnit} (not cached)`),
      tile("Loading the model", load, loadUnit, "slowest cases (after a long pause)"),
      tile("Writing speed", m.tokens_per_second ? String(Math.round(m.tokens_per_second)) : "–", "tokens/s", "last answer"),
    );
  }
  const stageNames = { recall: "Memory + document lookup", plan: "Planning (multi-step requests)" };
  for (const [stage, timing] of Object.entries(s.stages || {})) {
    const [typical, unit] = duration(timing.p50_ms);
    tiles.push(tile(stageNames[stage] || stage, typical, unit, "typical, before the model starts"));
  }
  $("model").replaceChildren(...tiles);
  $("model-empty").hidden = s.models.length > 0;
}

function drawTools(s) {
  const max = Math.max(0, ...s.tools.map((t) => t.calls));
  fillTable($("tools"), s.tools.map((t) => {
    const row = el("tr");
    row.append(
      el("td", "name", t.tool),
      barCell(t.calls, max, t.tool),
      numberCell(t.by_status.ok),
      numberCell(t.by_status.error),
      numberCell(t.by_status.needs_confirmation),
      numberCell(t.by_status.denied),
      durationCell(t.p50_ms),
      durationCell(t.p95_ms),
    );
    return row;
  }));
  $("tools-empty").hidden = s.tools.length > 0;
}

const REASONS = {
  wrong_host: ["Wrong address", "not addressed to localhost"],
  cross_origin: ["Other websites", "tried to change something"],
  rate_limited: ["Too many requests", "over the per-minute limit"],
  egress: ["Blocked connections", "browser → private address"],
};

function drawSafety(s) {
  $("safety").replaceChildren(
    ...Object.entries(REASONS).map(([key, [label, note]]) =>
      tile(label, compact(s.security_blocks[key] || 0), "", note)),
  );
}

function drawRoutes(s) {
  const max = Math.max(0, ...s.routes.map((r) => r.requests));
  fillTable($("routes"), s.routes.map((r) => {
    const row = el("tr");
    row.append(
      el("td", "name", r.route === "static" ? "(page files)" : r.route),
      barCell(r.requests, max, r.route),
      numberCell(r.errors),
      durationCell(r.p50_ms),
      durationCell(r.p95_ms),
    );
    return row;
  }));
}

// ---------- refresh loop ----------
async function refresh() {
  try {
    const res = await fetch("/metrics/summary");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const summary = await res.json();
    drawOverview(summary);
    drawModel(summary);
    drawTools(summary);
    drawSafety(summary);
    drawRoutes(summary);
    $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    $("updated").textContent = `Couldn't load stats (${error.message}). Is ARTHUR running?`;
  }
}

function schedule() {
  clearInterval(timer);
  if (!paused) timer = setInterval(refresh, REFRESH_MS);
}

$("pause").addEventListener("click", () => {
  paused = !paused;
  $("pause").setAttribute("aria-pressed", String(paused));
  $("pause").textContent = paused ? "Resume" : "Pause";
  schedule();
  if (!paused) refresh();
});

refresh();
schedule();
