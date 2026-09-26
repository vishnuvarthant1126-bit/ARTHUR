// ARTHUR web client: one WebSocket to /ws, streamed replies, auto-reconnect.
"use strict";

const $ = (id) => document.getElementById(id);
const els = {
  log: $("log"), empty: $("empty"), form: $("composer"), input: $("input"),
  send: $("send"), clear: $("clear"), status: $("status"), statusText: $("status-text"), model: $("model"),
};

const state = {
  ws: null,
  connected: false,
  busy: false,       // an answer is in progress
  reply: null,       // { bubble, meta, text } for the answer being streamed
  retries: 0,
  sessionId: loadSessionId(),
};

// The session id links this browser to its conversation on the server.
// localStorage can be unavailable (private mode), so every access is guarded.
function loadSessionId() {
  try { return localStorage.getItem("arthur.sessionId"); } catch { return null; }
}

function saveSessionId(id) {
  state.sessionId = id;
  try { localStorage.setItem("arthur.sessionId", id); } catch { /* memory-only is fine */ }
}

// ---------- status ----------
function setStatus(kind, label) {
  els.status.dataset.state = kind;
  els.statusText.textContent = label;
  document.body.dataset.state = kind;
}

function refreshComposer() {
  els.send.disabled = !state.connected || (!state.busy && !els.input.value.trim());
  els.send.classList.toggle("stop", state.busy);
  els.send.setAttribute("aria-label", state.busy ? "Stop" : "Send");
}

// ---------- connection ----------
function connect() {
  setStatus("connecting", "Connecting…");
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const query = state.sessionId ? `?session_id=${encodeURIComponent(state.sessionId)}` : "";
  const ws = new WebSocket(`${scheme}://${location.host}/ws${query}`);
  state.ws = ws;

  ws.onopen = () => {
    state.connected = true;
    state.retries = 0;
    setStatus("online", "Online");
    refreshComposer();
    loadHealth();
  };

  ws.onmessage = (event) => handleEvent(JSON.parse(event.data));

  ws.onclose = () => {
    state.connected = false;
    if (state.busy) failReply("Connection to ARTHUR was lost.");
    // Exponential backoff: 1s, 2s, 4s ... max 15s, so we don't hammer a stopped server.
    const delay = Math.min(1000 * 2 ** state.retries, 15000);
    state.retries += 1;
    setStatus("offline", "Offline");
    refreshComposer();
    setTimeout(connect, delay);
  };
}

async function loadHealth() {
  try {
    const res = await fetch("/health");
    const health = await res.json();
    els.model.textContent = `${health.llm.model} · ${health.llm.provider}`;
    if (!health.llm.reachable) setStatus("offline", "Model offline");
  } catch { /* the WebSocket status already tells the story */ }
}

// ---------- server events ----------
function handleEvent(event) {
  switch (event.type) {
    case "session":
      saveSessionId(event.session_id);
      renderHistory(event.history);
      break;
    case "cleared":
      clearScreen();
      break;
    case "status":
      setStatus("thinking", "Thinking…");
      break;
    case "token":
      if (!state.reply) return;
      if (!state.reply.text) setStatus("streaming", "Responding…");
      state.reply.text += event.content;
      state.reply.bubble.innerHTML = renderMarkdown(state.reply.text);
      state.reply.bubble.classList.add("cursor");
      scrollToBottom();
      break;
    case "done":
      finishReply(event);
      break;
    case "error":
      if (state.busy) failReply(event.message);
      else addError(event.message);
      break;
  }
}

// ---------- conversation ----------
function send(text) {
  if (!state.connected || state.busy || !text) return;
  els.empty.hidden = true;
  addMessage("user", text);
  startReply();
  state.ws.send(JSON.stringify({ type: "chat", message: text }));
  els.input.value = "";
  autosize();
}

function stop() {
  if (state.busy && state.connected) state.ws.send(JSON.stringify({ type: "stop" }));
}

function addMessage(role, text) {
  const row = document.createElement("div");
  row.className = `msg ${role}`;
  if (role !== "user") row.innerHTML = '<div class="core" aria-hidden="true"><span></span></div>';
  const body = document.createElement("div");
  body.className = "msg-body";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text; // textContent: user text can never inject HTML
  body.append(bubble);
  row.append(body);
  els.log.append(row);
  scrollToBottom();
  return { row, body, bubble };
}

function startReply() {
  const { body, bubble } = addMessage("arthur", "");
  bubble.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
  const meta = document.createElement("div");
  meta.className = "meta";
  body.append(meta);
  state.reply = { bubble, meta, text: "" };
  state.busy = true;
  refreshComposer();
}

function finishReply(info) {
  const reply = state.reply;
  if (reply) {
    reply.bubble.classList.remove("cursor");
    if (!reply.text) reply.bubble.textContent = info.stopped ? "(stopped)" : "(no response)";
    const seconds = (info.latency_ms / 1000).toFixed(1);
    reply.meta.textContent = `${info.model} · ${seconds}s${info.stopped ? " · stopped" : ""}`;
  }
  endReply();
}

function failReply(message) {
  state.reply?.bubble.closest(".msg").remove();
  addError(message);
  endReply();
}

function endReply() {
  state.reply = null;
  state.busy = false;
  if (state.connected) setStatus("online", "Online");
  refreshComposer();
  els.input.focus();
}

function addError(message) {
  addMessage("error", `⚠ ${message}`);
}

// The server is the source of truth: after a reload (or reconnect) we redraw
// exactly what ARTHUR remembers. After a server restart that is nothing.
function renderHistory(history) {
  clearScreen();
  for (const message of history) {
    const { bubble } = addMessage(message.role === "user" ? "user" : "arthur", message.content);
    if (message.role !== "user") bubble.innerHTML = renderMarkdown(message.content);
  }
  els.empty.hidden = history.length > 0;
}

function clearConversation() {
  if (state.connected) state.ws.send(JSON.stringify({ type: "clear" })); // server replies "cleared"
  else clearScreen();
}

function clearScreen() {
  els.log.querySelectorAll(".msg").forEach((m) => m.remove());
  els.empty.hidden = false;
  els.input.focus();
}

function scrollToBottom() {
  els.log.scrollTop = els.log.scrollHeight;
}

// ---------- tiny, safe Markdown renderer ----------
// Everything is HTML-escaped FIRST, so model output can never inject scripts.
// Only a small, fixed set of our own tags is added afterwards.
function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function renderInline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
}

function renderBlocks(text) {
  const out = [];
  let list = null; // "ul" | "ol" | null
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (const line of text.split("\n")) {
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    if (bullet || numbered) {
      const kind = bullet ? "ul" : "ol";
      if (list !== kind) { closeList(); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${renderInline((bullet || numbered)[1])}</li>`);
    } else if (heading) {
      closeList();
      out.push(`<p><strong>${renderInline(heading[1])}</strong></p>`);
    } else if (line.trim() === "") {
      closeList();
    } else {
      closeList();
      out.push(`<p>${renderInline(line)}</p>`);
    }
  }
  closeList();
  return out.join("");
}

function renderMarkdown(text) {
  // Split on ``` fences: odd parts are code blocks.
  return escapeHtml(text).split("```").map((part, i) =>
    i % 2 ? `<pre><code>${part.replace(/^[\w+-]*\n/, "")}</code></pre>` : renderBlocks(part)
  ).join("");
}

// ---------- input handling ----------
function autosize() {
  els.input.style.height = "auto";
  els.input.style.height = `${els.input.scrollHeight}px`;
  refreshComposer();
}

els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (state.busy) stop();
  else send(els.input.value.trim());
});

els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    els.form.requestSubmit();
  }
});

els.input.addEventListener("input", autosize);
els.clear.addEventListener("click", clearConversation);
document.querySelectorAll(".chip").forEach((chip) =>
  chip.addEventListener("click", () => send(chip.textContent))
);

connect();
els.input.focus();
