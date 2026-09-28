// ARTHUR web client: one WebSocket to /ws, streamed replies, auto-reconnect.
"use strict";

const $ = (id) => document.getElementById(id);
const els = {
  log: $("log"), empty: $("empty"), form: $("composer"), input: $("input"),
  send: $("send"), clear: $("clear"), status: $("status"), statusText: $("status-text"), model: $("model"),
  memoryToggle: $("memory-toggle"), memoryPanel: $("memory-panel"), memoryClose: $("memory-close"),
  memoryList: $("memory-list"), memoryEmpty: $("memory-empty"),
  docsToggle: $("docs-toggle"), docsPanel: $("docs-panel"), docsClose: $("docs-close"),
  docsInput: $("docs-input"), docsStatus: $("docs-status"), docsList: $("docs-list"), docsEmpty: $("docs-empty"),
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
      if (event.state === "executing") setStatus("executing", `Using ${event.tool}…`);
      else setStatus("thinking", "Thinking…");
      break;
    case "tool":
      if (state.reply) showTool(event);
      break;
    case "plan":
      if (state.reply) showPlan(event);
      break;
    case "step":
      if (state.reply) updateStep(event);
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
  const plan = document.createElement("ol");
  plan.className = "plan";
  plan.hidden = true;
  const tools = document.createElement("div");
  tools.className = "tools";
  body.insertBefore(plan, bubble); // plan and tool activity show above the answer
  body.insertBefore(tools, bubble);
  const meta = document.createElement("div");
  meta.className = "meta";
  body.append(meta);
  state.reply = { bubble, meta, tools, chips: {}, plan, steps: {}, text: "" };
  state.busy = true;
  refreshComposer();
}

function finishReply(info) {
  const reply = state.reply;
  if (!els.memoryPanel.hidden) loadMemories(); // a reply may have saved/forgotten something
  if (reply) {
    reply.bubble.classList.remove("cursor");
    if (!reply.text) reply.bubble.textContent = info.stopped ? "(stopped)" : "(no response)";
    const seconds = (info.latency_ms / 1000).toFixed(1);
    reply.meta.textContent = `${info.model} · ${seconds}s${info.stopped ? " · stopped" : ""}`;
  }
  endReply();
}

// A multi-step plan: one line per step, ticked off as the steps finish.
function showPlan(event) {
  const reply = state.reply;
  reply.plan.replaceChildren();
  for (const step of event.steps) {
    const item = document.createElement("li");
    item.dataset.status = "pending";
    const task = document.createElement("span");
    task.className = "task";
    task.textContent = step.task; // textContent: model text can never inject HTML
    const detail = document.createElement("span");
    detail.className = "detail";
    item.append(task, detail);
    reply.plan.append(item);
    reply.steps[step.id] = item;
  }
  reply.plan.hidden = false;
  reply.stepCount = event.steps.length;
  scrollToBottom();
}

function updateStep(event) {
  const item = state.reply.steps[event.id];
  if (!item) return;
  item.dataset.status = event.status;
  const retry = event.attempt > 1 ? ` (retry ${event.attempt - 1})` : "";
  item.querySelector(".detail").textContent =
    event.status === "running" ? retry : event.detail ? ` — ${event.detail}` : "";
  if (event.status === "running") setStatus("executing", `Step ${event.id}/${state.reply.stepCount}…`);
  scrollToBottom();
}

// One chip per tool call: "⚙ calculator  482 * 29" → "✓ calculator  result: 13978"
function showTool(event) {
  const reply = state.reply;
  let chip = reply.chips[event.call_id];
  if (!chip) {
    chip = document.createElement("div");
    chip.className = "tool-chip running";
    const icon = document.createElement("span");
    icon.className = "icon";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = event.name;
    const detail = document.createElement("span");
    detail.className = "detail";
    chip.append(icon, name, detail);
    reply.tools.append(chip);
    reply.chips[event.call_id] = chip;
  }
  const detail = chip.querySelector(".detail");
  if (event.phase === "start") {
    detail.textContent = Object.values(event.arguments || {}).join(", ");
  } else {
    chip.className = `tool-chip ${event.status === "ok" ? "ok" : event.status === "needs_confirmation" ? "waiting" : "failed"}`;
    detail.textContent = event.summary; // textContent: tool output can never inject HTML
    chip.title = `${event.name} · ${event.status} · ${event.duration_ms} ms`;
  }
  scrollToBottom();
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

// ---------- memory panel ----------
function toggleMemoryPanel(open = els.memoryPanel.hidden) {
  if (open) toggleDocsPanel(false); // one panel at a time
  els.memoryPanel.hidden = !open;
  els.memoryToggle.setAttribute("aria-expanded", String(open));
  if (open) loadMemories();
}

// ---------- documents panel ----------
function toggleDocsPanel(open = els.docsPanel.hidden) {
  if (open) toggleMemoryPanel(false);
  els.docsPanel.hidden = !open;
  els.docsToggle.setAttribute("aria-expanded", String(open));
  if (open) loadDocuments();
}

function docsStatus(text, isError = false) {
  els.docsStatus.textContent = text;
  els.docsStatus.classList.toggle("error", isError);
}

async function loadDocuments() {
  try {
    const res = await fetch("/documents");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const { documents } = await res.json();
    els.docsList.replaceChildren(
      ...documents.map((d) => {
        const item = document.createElement("li");
        const text = document.createElement("div");
        const name = document.createElement("div");
        name.className = "fact";
        name.textContent = d.filename;
        const tag = document.createElement("span");
        tag.className = "tag";
        const pages = d.file_type === "pdf" ? `${d.pages} page${d.pages === 1 ? "" : "s"} · ` : "";
        tag.textContent = `${d.file_type} · ${pages}${d.chunks} chunks`;
        text.append(name, tag);
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "✕";
        remove.title = "Delete document";
        remove.setAttribute("aria-label", `Delete ${d.filename}`);
        remove.addEventListener("click", () => deleteDocument(d));
        item.append(text, remove);
        return item;
      })
    );
    els.docsEmpty.hidden = documents.length > 0;
  } catch {
    docsStatus("Couldn't load documents.", true);
  }
}

async function uploadDocuments(files) {
  for (const file of files) {
    docsStatus(`Reading and indexing ${file.name}…`);
    const form = new FormData();
    form.append("file", file);
    try {
      const res = await fetch("/documents", { method: "POST", body: form });
      const body = await res.json();
      if (!res.ok) {
        docsStatus(`${file.name}: ${body.detail || body.error?.message || res.status}`, true);
        return;
      }
      const d = body.document;
      docsStatus(body.created ? `Added ${d.filename} (${d.chunks} chunks).` : `${d.filename} was already added.`);
    } catch {
      docsStatus(`Upload of ${file.name} failed.`, true);
      return;
    }
  }
  els.docsInput.value = "";
  loadDocuments();
}

async function deleteDocument(doc) {
  // Deleting is a level-2 action: always confirm first.
  if (!confirm(`Delete this document?\n\n${doc.filename}`)) return;
  await fetch(`/documents/${encodeURIComponent(doc.id)}`, { method: "DELETE" });
  docsStatus(`Deleted ${doc.filename}.`);
  loadDocuments();
}

async function loadMemories() {
  try {
    const res = await fetch("/memories");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderMemories((await res.json()).memories);
  } catch {
    els.memoryList.replaceChildren();
    els.memoryEmpty.hidden = false;
    els.memoryEmpty.textContent = "Couldn't load memories.";
  }
}

function renderMemories(memories) {
  els.memoryList.replaceChildren(
    ...memories.map((m) => {
      const item = document.createElement("li");
      const text = document.createElement("div");
      const fact = document.createElement("div");
      fact.className = "fact";
      fact.textContent = m.content; // textContent: stored text can never inject HTML
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = m.category;
      text.append(fact, tag);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "✕";
      remove.title = "Forget this";
      remove.setAttribute("aria-label", `Forget: ${m.content}`);
      remove.addEventListener("click", () => deleteMemory(m));
      item.append(text, remove);
      return item;
    })
  );
  els.memoryEmpty.hidden = memories.length > 0;
  els.memoryEmpty.textContent = "Nothing yet. Try: “Remember that my main project is called ARTHUR.”";
}

async function deleteMemory(memory) {
  // Deleting is a level-2 action: always confirm first.
  if (!confirm(`Forget this?\n\n${memory.content}`)) return;
  await fetch(`/memories/${encodeURIComponent(memory.id)}`, { method: "DELETE" });
  loadMemories();
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

// Small models sometimes write maths as LaTeX ($482 \times 29$) even when asked not to.
// Show it as plain text instead: strip the $ markers and turn common commands into symbols.
function plainMath(text) {
  const symbols = { times: "×", div: "÷", cdot: "·", pm: "±", le: "≤", ge: "≥", approx: "≈", neq: "≠" };
  return text
    .replace(/\$\$?([^$\n]{1,200})\$\$?/g, "$1")
    .replace(/\\(times|div|cdot|pm|le|ge|approx|neq)\b/g, (_, cmd) => symbols[cmd]);
}

function renderMarkdown(text) {
  // Split on ``` fences: odd parts are code blocks (left exactly as written).
  return escapeHtml(text).split("```").map((part, i) =>
    i % 2 ? `<pre><code>${part.replace(/^[\w+-]*\n/, "")}</code></pre>` : renderBlocks(plainMath(part))
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
els.memoryToggle.addEventListener("click", () => toggleMemoryPanel());
els.memoryClose.addEventListener("click", () => toggleMemoryPanel(false));
els.docsToggle.addEventListener("click", () => toggleDocsPanel());
els.docsClose.addEventListener("click", () => toggleDocsPanel(false));
els.docsInput.addEventListener("change", () => uploadDocuments([...els.docsInput.files]));
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  toggleMemoryPanel(false);
  toggleDocsPanel(false);
});
document.querySelectorAll(".chip").forEach((chip) =>
  chip.addEventListener("click", () => send(chip.textContent))
);

connect();
els.input.focus();
