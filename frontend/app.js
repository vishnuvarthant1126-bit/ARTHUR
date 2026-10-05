// ARTHUR web client: one WebSocket to /ws, streamed replies, auto-reconnect.
"use strict";

const $ = (id) => document.getElementById(id);
const els = {
  log: $("log"), empty: $("empty"), form: $("composer"), input: $("input"),
  send: $("send"), clear: $("clear"), status: $("status"), statusText: $("status-text"), model: $("model"),
  memoryToggle: $("memory-toggle"), memoryPanel: $("memory-panel"), memoryClose: $("memory-close"),
  memoryList: $("memory-list"), memoryEmpty: $("memory-empty"),
  mic: $("mic"), wake: $("wake"),
  voiceToggle: $("voice-toggle"), voicePanel: $("voice-panel"), voiceClose: $("voice-close"),
  speakMode: $("speak-mode"), voiceSelect: $("voice-select"), voiceSpeed: $("voice-speed"),
  voiceSpeedValue: $("voice-speed-value"), voiceVolume: $("voice-volume"),
  voiceVolumeValue: $("voice-volume-value"), voiceTest: $("voice-test"),
  docsToggle: $("docs-toggle"), docsPanel: $("docs-panel"), docsClose: $("docs-close"),
  docsInput: $("docs-input"), docsStatus: $("docs-status"), docsList: $("docs-list"), docsEmpty: $("docs-empty"),
  remindersToggle: $("reminders-toggle"), remindersPanel: $("reminders-panel"), remindersClose: $("reminders-close"),
  reminderForm: $("reminder-form"), reminderText: $("reminder-text"), reminderWhen: $("reminder-when"),
  remindersStatus: $("reminders-status"), remindersList: $("reminders-list"), remindersEmpty: $("reminders-empty"),
  hud: $("hud"), hudToggle: $("hud-toggle"), hudClose: $("hud-close"), hudNow: $("hud-now"),
  hudMic: $("hud-mic"), hudModel: $("hud-model"), hudTaskTitle: $("hud-task-title"),
  hudTaskGoal: $("hud-task-goal"), hudTask: $("hud-task"), hudTaskResult: $("hud-task-result"),
  hudSystem: $("hud-system"), hudOverall: $("hud-overall"), hudUptime: $("hud-uptime"),
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
  renderLamps(kind, label);
}

function refreshComposer() {
  const stoppable = state.busy || isSpeaking();
  els.send.disabled = !stoppable && (!state.connected || !els.input.value.trim());
  els.send.classList.toggle("stop", stoppable);
  els.send.setAttribute("aria-label", stoppable ? "Stop" : "Send");
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
    loadSystem();
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
    els.hudModel.textContent = health.llm.model;
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
      hudTool(event);
      break;
    case "plan":
      if (state.reply) showPlan(event);
      hudPlan(event);
      break;
    case "step":
      if (state.reply) updateStep(event);
      hudStep(event);
      break;
    case "token":
      if (!state.reply) return;
      if (!state.reply.text) setStatus("streaming", "Responding…");
      state.reply.text += event.content;
      speakStreamed(event.content);
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
    case "reminder":
      showReminder(event);
      break;
  }
}

// ---------- conversation ----------
function send(text, { fromVoice = false } = {}) {
  if (!state.connected || state.busy || !text) return;
  state.lastInputWasVoice = fromVoice; // decides whether the reply is spoken ("When I talk")
  stopSpeaking(); // a new question interrupts the previous answer
  els.empty.hidden = true;
  addMessage("user", text);
  hudStartTask(text);
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
  beginSpokenReply();
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
  if (!info.stopped) speakStreamed("", { final: true }); // say the last, unfinished sentence
  if (!els.memoryPanel.hidden) loadMemories(); // a reply may have saved/forgotten something
  if (!els.remindersPanel.hidden) loadReminders();
  if (reply) {
    reply.bubble.classList.remove("cursor");
    if (!reply.text) reply.bubble.textContent = info.stopped ? "(stopped)" : "(no response)";
    const seconds = (info.latency_ms / 1000).toFixed(1);
    reply.meta.textContent = `${info.model} · ${seconds}s${info.stopped ? " · stopped" : ""}`;
  }
  hudFinishTask(info);
  loadSystem(); // counts (memories, reminders...) may have changed
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
  const detail = (event.detail || "").replace(/\*\*|__/g, ""); // drop Markdown bold markers
  item.querySelector(".detail").textContent =
    event.status === "running" ? retry : detail ? ` — ${detail}` : "";
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
  hudFailTask(message);
  state.reply?.bubble.closest(".msg").remove();
  addError(message);
  endReply();
}

function endReply() {
  state.reply = null;
  state.busy = false;
  if (isSpeaking()) setStatus("speaking", "Speaking…");
  else idleStatus();
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

// ---------- voice input (speech-to-text) ----------
// Click 🎤 to start recording, click again to stop. The audio is sent to ARTHUR on this
// computer (/voice/transcribe), the text appears in the box and is sent like a typed message.
const voice = { recorder: null, chunks: [], stream: null, timer: null, busy: false };
const MAX_RECORDING_MS = 60_000;

function micError(error) {
  const messages = {
    NotAllowedError: "Microphone access was blocked. If you see ARTHUR inside another app (e.g. the Claude app), open http://127.0.0.1:8000 in Chrome or Edge instead. In the browser: click the icon left of the address, set Microphone to Allow, then reload.",
    NotFoundError: "No microphone was found. Plug one in or check Windows sound settings.",
    NotReadableError: "The microphone is busy in another app (e.g. a video call).",
    SecurityError: "The browser blocked the microphone on this page.",
  };
  addError(messages[error.name] || `Microphone problem: ${error.message}`);
}

async function toggleRecording() {
  if (voice.recorder) return stopRecording();
  stopSpeaking(); // "barge in": starting to talk silences ARTHUR
  if (voice.busy || state.busy) return;
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
    addError("Voice input isn't supported in this browser. Try Chrome, Edge or Firefox.");
    return;
  }
  try {
    // echoCancellation/noiseSuppression: the browser cleans up background noise for us.
    voice.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    });
  } catch (error) {
    micError(error);
    return;
  }
  const mimeType = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/webm"]
    .find((t) => MediaRecorder.isTypeSupported(t)) || "";
  voice.chunks = [];
  voice.recorder = new MediaRecorder(voice.stream, mimeType ? { mimeType } : {});
  voice.recorder.ondataavailable = (e) => { if (e.data.size) voice.chunks.push(e.data); };
  voice.recorder.onstop = () => transcribeRecording(voice.recorder?.mimeType || mimeType);
  voice.recorder.start();
  voice.timer = setTimeout(stopRecording, MAX_RECORDING_MS); // never record forever
  els.mic.classList.add("recording");
  els.mic.setAttribute("aria-label", "Stop recording");
  setStatus("listening", "Listening…");
}

function stopRecording() {
  clearTimeout(voice.timer);
  if (voice.recorder?.state === "recording") voice.recorder.stop();
  voice.stream?.getTracks().forEach((t) => t.stop()); // turns the browser's mic indicator off
  els.mic.classList.remove("recording");
  els.mic.setAttribute("aria-label", "Start voice input");
}

async function transcribeRecording(mimeType) {
  voice.recorder = null;
  const blob = new Blob(voice.chunks, { type: mimeType || "audio/webm" });
  voice.chunks = [];
  if (blob.size === 0) { idleStatus(); return; }
  voice.busy = true;
  els.mic.classList.add("working");
  setStatus("thinking", "Transcribing…");
  try {
    const form = new FormData();
    form.append("audio", blob, `recording.${mimeType.includes("ogg") ? "ogg" : "webm"}`);
    const res = await fetch("/voice/transcribe", { method: "POST", body: form });
    const body = await res.json();
    if (!res.ok) {
      addError(body.detail || body.error?.message || `Transcription failed (${res.status}).`);
      return;
    }
    send(body.text, { fromVoice: true });
  } catch {
    addError("Couldn't reach ARTHUR to transcribe the recording.");
  } finally {
    voice.busy = false;
    els.mic.classList.remove("working");
    if (!state.busy) idleStatus();
  }
}

// ---------- hands-free: "Hey Arthur" (wake word) ----------
// While enabled, the mic stays open. The browser measures loudness ~125 times a second and
// cuts out clips of speech (from the moment sound starts until 0.7 s of silence). Only
// those clips go to ARTHUR on this computer, which checks whether they start with
// "Hey Arthur". Nothing is stored. While ARTHUR is thinking or speaking, clips are
// ignored, so it can't wake itself up with its own voice.
const WAKE = {
  prerollMs: 300, // keep a little audio from just before speech started ("Hey" is short)
  endSilenceMs: 700, // this much quiet ends a clip
  minSpeechMs: 350, // shorter bursts (clicks, coughs) are ignored
  maxWakeClipMs: 10_000,
  maxCommandClipMs: 15_000,
  commandTimeoutMs: 7_000, // after "Yes?", wait this long for the command
  minThreshold: 0.015, // loudness (RMS) below this is always "quiet"
  noiseFactor: 3.5, // speech must be this many times louder than the room's background noise
};
const TAP_WORKLET = `
class ArthurTap extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0][0];
    if (channel) this.port.postMessage(channel.slice(0));
    return true;
  }
}
registerProcessor("arthur-tap", ArthurTap);`;

const wake = {
  enabled: false, ctx: null, stream: null, node: null,
  phase: "wake", // "wake": waiting for the phrase · "command": listening after "Yes?"
  noise: 0.005, inSpeech: false, preroll: [], clip: [], speechMs: 0, silenceMs: 0,
  checking: false, commandTimer: null,
};

async function toggleWakeMode() {
  if (wake.enabled) return stopWakeMode();
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
    addError("Hands-free mode isn't supported in this browser. Try Chrome or Edge.");
    return;
  }
  try {
    wake.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
  } catch (error) {
    micError(error);
    return;
  }
  // Ask for 16 kHz (what Whisper uses); the browser resamples the mic for us.
  try { wake.ctx = new AudioContext({ sampleRate: 16000 }); } catch { wake.ctx = new AudioContext(); }
  await wake.ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([TAP_WORKLET], { type: "text/javascript" })));
  const source = wake.ctx.createMediaStreamSource(wake.stream);
  wake.node = new AudioWorkletNode(wake.ctx, "arthur-tap");
  const mute = wake.ctx.createGain();
  mute.gain.value = 0; // connected so the audio graph runs, but silent: no echo
  source.connect(wake.node).connect(mute).connect(wake.ctx.destination);
  wake.node.port.onmessage = (e) => onAudioBlock(e.data);
  wake.enabled = true;
  wake.phase = "wake";
  els.wake.setAttribute("aria-pressed", "true");
  showWakeStatus();
}

function stopWakeMode() {
  wake.enabled = false;
  clearTimeout(wake.commandTimer);
  wake.node?.disconnect();
  wake.stream?.getTracks().forEach((t) => t.stop()); // mic indicator off
  wake.ctx?.close();
  Object.assign(wake, { ctx: null, stream: null, node: null, inSpeech: false, clip: [], preroll: [] });
  els.wake.setAttribute("aria-pressed", "false");
  renderLamps(els.status.dataset.state, els.statusText.textContent);
  if (!state.busy && !isSpeaking() && state.connected) setStatus("online", "Online");
}

// What the status light shows when ARTHUR isn't busy: waiting for "Hey Arthur", or Online.
function idleStatus() {
  if (!state.connected) return;
  if (wake.enabled) showWakeStatus();
  else setStatus("online", "Online");
}

function showWakeStatus() {
  if (!wake.enabled || state.busy || isSpeaking() || voice.recorder) return;
  if (wake.phase === "command") setStatus("listening", "Listening…");
  else setStatus("waiting", "Say “Hey Arthur”");
}

// Paused while ARTHUR answers or speaks, while push-to-talk records, or while a clip is checked.
function wakePaused() {
  return state.busy || isSpeaking() || voice.recorder || voice.busy || wake.checking;
}

function onAudioBlock(samples) {
  if (!wake.enabled) return;
  const blockMs = (samples.length / wake.ctx.sampleRate) * 1000;
  if (wakePaused()) {
    wake.inSpeech = false;
    wake.clip = [];
    return;
  }
  let sum = 0;
  for (const s of samples) sum += s * s;
  const rms = Math.sqrt(sum / samples.length);
  const threshold = Math.max(WAKE.minThreshold, wake.noise * WAKE.noiseFactor);

  if (!wake.inSpeech) {
    wake.noise = wake.noise * 0.995 + rms * 0.005; // slowly learn the room's background noise
    wake.preroll.push(samples);
    while (wake.preroll.length * blockMs > WAKE.prerollMs) wake.preroll.shift();
    if (rms > threshold) {
      Object.assign(wake, { inSpeech: true, clip: [...wake.preroll], speechMs: 0, silenceMs: 0 });
      clearTimeout(wake.commandTimer); // the user started talking
    }
    return;
  }
  wake.clip.push(samples);
  if (rms > threshold * 0.6) { wake.speechMs += blockMs; wake.silenceMs = 0; } else { wake.silenceMs += blockMs; }
  const clipMs = wake.clip.length * blockMs;
  const maxMs = wake.phase === "command" ? WAKE.maxCommandClipMs : WAKE.maxWakeClipMs;
  if (wake.silenceMs >= WAKE.endSilenceMs || clipMs >= maxMs) {
    const clip = wake.clip;
    Object.assign(wake, { inSpeech: false, clip: [], preroll: [] });
    if (wake.speechMs >= WAKE.minSpeechMs) handleClip(encodeWav(clip, wake.ctx.sampleRate));
  }
}

async function handleClip(wav) {
  wake.checking = true;
  const form = new FormData();
  form.append("audio", wav, "clip.wav");
  try {
    if (wake.phase === "command") {
      setStatus("thinking", "Transcribing…");
      const res = await fetch("/voice/transcribe", { method: "POST", body: form });
      const body = await res.json();
      wake.phase = "wake";
      if (res.ok) send(body.text, { fromVoice: true });
      else addError(body.detail || "Sorry, I didn't catch that.");
      return;
    }
    const res = await fetch("/voice/wake", { method: "POST", body: form });
    if (!res.ok) return;
    const result = await res.json();
    if (!result.wake) return; // not for ARTHUR: ignore silently
    if (result.command) {
      send(result.command, { fromVoice: true }); // "Hey Arthur, what's the weather?"
    } else {
      sayYes(); // just "Hey Arthur": answer, then listen for the command
    }
  } catch {
    /* ARTHUR unreachable - the connection status already shows it */
  } finally {
    wake.checking = false;
    showWakeStatus();
  }
}

function sayYes() {
  wake.phase = "command";
  stopSpeaking();
  speech.active = true;
  speakStreamed("Yes?", { final: true });
  speech.active = false;
  clearTimeout(wake.commandTimer);
  // If nothing is said after "Yes?", quietly go back to waiting for the wake word.
  wake.commandTimer = setTimeout(() => {
    if (wake.phase === "command" && !wake.inSpeech) { wake.phase = "wake"; showWakeStatus(); }
  }, WAKE.commandTimeoutMs + 1500);
}

// Float32 samples -> 16-bit PCM WAV (resampled to 16 kHz if the browser ignored our request).
function encodeWav(blocks, rate) {
  let samples = new Float32Array(blocks.reduce((n, b) => n + b.length, 0));
  let offset = 0;
  for (const b of blocks) { samples.set(b, offset); offset += b.length; }
  if (rate !== 16000) {
    const ratio = rate / 16000;
    const resampled = new Float32Array(Math.floor(samples.length / ratio));
    for (let i = 0; i < resampled.length; i++) resampled[i] = samples[Math.floor(i * ratio)];
    samples = resampled;
  }
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const text = (pos, s) => [...s].forEach((c, i) => view.setUint8(pos + i, c.charCodeAt(0)));
  text(0, "RIFF"); view.setUint32(4, 36 + samples.length * 2, true); text(8, "WAVE");
  text(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true);
  view.setUint16(34, 16, true); text(36, "data"); view.setUint32(40, samples.length * 2, true);
  samples.forEach((s, i) => view.setInt16(44 + i * 2, Math.max(-1, Math.min(1, s)) * 0x7fff, true));
  return new Blob([buffer], { type: "audio/wav" });
}

// ---------- voice output (text-to-speech) ----------
// As the reply streams in, finished sentences are sent to /voice/speak (in order, fetched
// ahead of time) and played one after another - so ARTHUR starts talking after sentence one.
const SPEECH_DEFAULTS = { mode: "voice", voice: "", speed: 1, volume: 1 };
const speech = {
  settings: loadSpeechSettings(),
  queue: [], // promises of audio URLs, in sentence order
  audio: null, // the <audio> currently playing
  pending: "", // streamed text not yet spoken (an unfinished sentence)
  inCode: false, // inside a ``` code block - never read code aloud
  active: false, // is this reply being spoken?
  generation: 0, // bumps on stop, so late audio from an old reply is dropped
  playing: false,
};

function loadSpeechSettings() {
  try {
    return { ...SPEECH_DEFAULTS, ...JSON.parse(localStorage.getItem("arthur.speech") || "{}") };
  } catch {
    return { ...SPEECH_DEFAULTS };
  }
}

function saveSpeechSettings() {
  try { localStorage.setItem("arthur.speech", JSON.stringify(speech.settings)); } catch { /* fine */ }
}

function isSpeaking() {
  return speech.playing || speech.queue.length > 0;
}

function speakStreamed(text, { final = false } = {}) {
  if (!speech.active) return;
  speech.pending += text;
  // Split off complete sentences: . ! ? : followed by a space, or a line break. The two
  // characters before the mark must be "word" characters, so "3.14", "e.g." and the
  // "p. 2" inside a citation like [handbook.pdf, p. 2] are not treated as sentence ends.
  const parts = speech.pending.split(/(?<=[^\s.]{2}[.!?:])\s+|\n+/);
  const rest = parts.pop();
  speech.pending = final ? "" : rest;
  if (final) parts.push(rest);
  for (let part of parts) {
    if (part.includes("```")) {
      const fences = part.split("```");
      part = fences.filter((_, i) => (speech.inCode ? i % 2 === 1 : i % 2 === 0)).join(" ");
      if (fences.length % 2 === 0) speech.inCode = !speech.inCode;
    } else if (speech.inCode) {
      continue;
    }
    if (part.trim()) enqueueSpeech(part.trim());
  }
}

function enqueueSpeech(sentence) {
  const generation = speech.generation;
  const { voice, speed } = speech.settings;
  const audioUrl = fetch("/voice/speak", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: sentence, voice: voice || null, speed }),
  })
    .then((res) => (res.status === 200 ? res.blob() : null))
    .then((blob) => (blob && generation === speech.generation ? URL.createObjectURL(blob) : null))
    .catch(() => null);
  speech.queue.push(audioUrl);
  if (!speech.playing) playSpeechQueue();
  refreshComposer();
}

async function playSpeechQueue() {
  speech.playing = true;
  while (speech.queue.length) {
    const generation = speech.generation;
    const url = await speech.queue.shift();
    if (!url || generation !== speech.generation) continue;
    if (!state.busy) setStatus("speaking", "Speaking…");
    else renderLamps(els.status.dataset.state, els.statusText.textContent);
    await new Promise((resolve) => {
      const audio = new Audio(url);
      speech.audio = audio;
      audio.volume = speech.settings.volume;
      audio.onended = audio.onerror = () => { URL.revokeObjectURL(url); resolve(); };
      audio.play().catch(() => {
        // Browsers only allow sound after the user has interacted with the page.
        addError("Your browser blocked ARTHUR's voice. Click anywhere on the page, then try again.");
        stopSpeaking();
        resolve();
      });
    });
  }
  speech.playing = false;
  renderLamps(els.status.dataset.state, els.statusText.textContent);
  speech.audio = null;
  if (!state.busy) idleStatus();
  refreshComposer();
}

function stopSpeaking() {
  speech.generation += 1;
  speech.queue = [];
  speech.pending = "";
  speech.inCode = false;
  speech.audio?.pause();
  speech.audio?.dispatchEvent(new Event("ended")); // resolve the waiting play step
  refreshComposer();
}

function beginSpokenReply() {
  const mode = speech.settings.mode;
  speech.active = mode === "always" || (mode === "voice" && state.lastInputWasVoice);
  speech.pending = "";
  speech.inCode = false;
}

async function loadVoices() {
  try {
    const { default: fallback, voices } = await (await fetch("/voice/voices")).json();
    els.voiceSelect.replaceChildren(
      ...voices.map((v) => new Option(`${v.name} (${v.language.replace("_", "-")}, ${v.quality})`, v.id))
    );
    els.voiceSelect.value = speech.settings.voice || fallback;
  } catch { /* the panel still works with the server's default voice */ }
}

function toggleVoicePanel(open = els.voicePanel.hidden) {
  if (open) { toggleMemoryPanel(false); toggleDocsPanel(false); toggleRemindersPanel(false); loadVoices(); }
  els.voicePanel.hidden = !open;
  els.voiceToggle.setAttribute("aria-expanded", String(open));
}

function showSpeechSettings() {
  const s = speech.settings;
  els.speakMode.value = s.mode;
  els.voiceSpeed.value = s.speed;
  els.voiceVolume.value = s.volume;
  els.voiceSpeedValue.textContent = `${Number(s.speed).toFixed(2)}×`;
  els.voiceVolumeValue.textContent = `${Math.round(s.volume * 100)}%`;
}

function updateSpeechSetting(key, value) {
  speech.settings[key] = value;
  if (key === "volume" && speech.audio) speech.audio.volume = value;
  saveSpeechSettings();
  showSpeechSettings();
}

// ---------- memory panel ----------
function toggleMemoryPanel(open = els.memoryPanel.hidden) {
  if (open) { toggleDocsPanel(false); toggleVoicePanel(false); toggleRemindersPanel(false); } // one panel at a time
  els.memoryPanel.hidden = !open;
  els.memoryToggle.setAttribute("aria-expanded", String(open));
  if (open) loadMemories();
}

// ---------- documents panel ----------
function toggleDocsPanel(open = els.docsPanel.hidden) {
  if (open) { toggleMemoryPanel(false); toggleVoicePanel(false); toggleRemindersPanel(false); }
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

// ---------- reminders ----------
// The scheduler pushes {"type": "reminder"} when one is due - possibly mid-answer.
function showReminder(event) {
  els.empty.hidden = true;
  const { row, bubble } = addMessage("assistant", "");
  row.classList.add("reminder");
  const late = event.late ? ` (was due ${event.due})` : "";
  // textContent only: reminder text is stored user/model text and must never inject HTML.
  const label = document.createElement("strong");
  label.textContent = "⏰ Reminder: ";
  bubble.append(label, document.createTextNode(event.text + late));
  // Keep an answer that is being streamed at the bottom, below the reminder.
  if (state.reply) els.log.append(state.reply.bubble.closest(".msg"));
  chime();
  if (speech.settings.mode !== "never") enqueueSpeech(`Reminder: ${event.text}.`);
  document.title = "⏰ ARTHUR";
  window.addEventListener("focus", () => { document.title = "ARTHUR"; }, { once: true });
  if (!els.remindersPanel.hidden) loadReminders();
  scrollToBottom();
}

function chime() {
  try {
    const audio = new (window.AudioContext || window.webkitAudioContext)();
    const tone = audio.createOscillator();
    const gain = audio.createGain();
    tone.frequency.value = 880;
    gain.gain.setValueAtTime(0.08 * speech.settings.volume, audio.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + 0.6);
    tone.connect(gain).connect(audio.destination);
    tone.start();
    tone.stop(audio.currentTime + 0.6);
    tone.onended = () => audio.close();
  } catch { /* no sound is fine */ }
}

function toggleRemindersPanel(open = els.remindersPanel.hidden) {
  if (open) { toggleMemoryPanel(false); toggleDocsPanel(false); toggleVoicePanel(false); }
  els.remindersPanel.hidden = !open;
  els.remindersToggle.setAttribute("aria-expanded", String(open));
  if (open) loadReminders();
}

function remindersStatus(text, isError = false) {
  els.remindersStatus.textContent = text;
  els.remindersStatus.classList.toggle("error", isError);
}

async function loadReminders() {
  try {
    const res = await fetch("/reminders");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderReminders((await res.json()).upcoming);
  } catch {
    els.remindersList.replaceChildren();
    els.remindersEmpty.hidden = false;
    els.remindersEmpty.textContent = "Couldn't load reminders.";
  }
}

function renderReminders(reminders) {
  els.remindersList.replaceChildren(
    ...reminders.map((r) => {
      const item = document.createElement("li");
      const text = document.createElement("div");
      const what = document.createElement("div");
      what.className = "fact";
      what.textContent = r.text;
      const when = document.createElement("span");
      when.className = "tag";
      when.textContent = r.due;
      text.append(what, when);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "✕";
      remove.title = "Cancel this reminder";
      remove.setAttribute("aria-label", `Cancel reminder: ${r.text}`);
      remove.addEventListener("click", () => cancelReminder(r));
      item.append(text, remove);
      return item;
    })
  );
  els.remindersEmpty.hidden = reminders.length > 0;
  els.remindersEmpty.textContent = "No upcoming reminders.";
}

async function addReminder() {
  const body = { text: els.reminderText.value.trim(), when: els.reminderWhen.value.trim() };
  if (!body.text || !body.when) return;
  try {
    const res = await fetch("/reminders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(typeof data.detail === "string" ? data.detail : data.message || "Couldn't add it.");
    }
    remindersStatus(`Set for ${data.due}.`);
    els.reminderForm.reset();
    loadReminders();
  } catch (error) {
    remindersStatus(error.message, true);
  }
}

async function cancelReminder(reminder) {
  if (!confirm(`Cancel this reminder?\n\n${reminder.text}\n${reminder.due}`)) return;
  await fetch(`/reminders/${reminder.id}`, { method: "DELETE" });
  loadReminders();
}

// ---------- tiny, safe Markdown renderer ----------
// Everything is HTML-escaped FIRST, so model output can never inject scripts.
// Only a small, fixed set of our own tags is added afterwards.
function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function renderInline(s) {
  // Links are set aside first so the * rules below can't break a URL. Only http(s) links
  // are allowed (never "javascript:"), and they open in a new tab without access to this page.
  const links = [];
  s = s.replace(/\[([^\]]{1,200})\]\((https?:\/\/[^\s)]{1,500})\)/g, (_, text, url) => {
    links.push(`<a href="${url}" target="_blank" rel="noopener noreferrer nofollow">${text}</a>`);
    return `\u0000${links.length - 1}\u0000`;
  });
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\u0000(\d+)\u0000/g, (_, i) => links[Number(i)]);
}

function renderBlocks(text) {
  const out = [];
  let list = null; // "ul" | "ol" | null
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (const line of text.split("\n")) {
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const numbered = line.match(/^\s*(\d+)[.)]\s+(.*)$/);
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    if (bullet || numbered) {
      const kind = bullet ? "ul" : "ol";
      if (list !== kind) {
        closeList();
        // Keep the model's numbering ("2.") even if a bullet list interrupted the numbered one.
        out.push(numbered ? `<ol start="${Number(numbered[1])}">` : "<ul>");
        list = kind;
      }
      out.push(`<li>${renderInline(bullet ? bullet[1] : numbered[2])}</li>`);
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

// ---------- status rail (Phase 25) ----------
// Five lamps for what ARTHUR is doing, the task in progress, and the health of every part.
// Everything is set with textContent / dataset - names and arguments come from the model
// and tools and are never treated as HTML.
const hud = { tools: {}, steps: {}, systemTimer: null };
const SYSTEM_REFRESH_MS = 20_000;

function renderLamps(kind, label) {
  const lamps = {};
  for (const li of els.hud.querySelectorAll(".lamps li")) {
    li.className = "";
    lamps[li.dataset.lamp] = li;
  }
  if (kind === "offline") lamps.online.className = "bad";
  else if (kind === "connecting") lamps.online.className = "wait";
  else lamps.online.className = "on";
  if (kind === "listening") lamps.listening.className = "on";
  else if (kind === "waiting") lamps.listening.className = "idle";
  if (kind === "thinking" || kind === "streaming") lamps.thinking.className = "on";
  if (kind === "executing") lamps.executing.className = "on";
  if (kind === "speaking" || (typeof speech !== "undefined" && isSpeaking())) lamps.speaking.className = "on";
  if (kind !== "executing" || !els.hudNow.dataset.tool) els.hudNow.textContent = label;
  if (kind !== "executing") delete els.hudNow.dataset.tool;
  els.hudMic.textContent = micState();
}

function micState() {
  if (typeof voice === "undefined" || typeof wake === "undefined") return "off";
  if (voice.recorder) return "recording";
  if (wake.enabled) return wake.phase === "command" ? "listening for your request" : "hands-free · say \u201cHey Arthur\u201d";
  return "off";
}

function hudItem(status, what, detail = "") {
  const item = document.createElement("li");
  item.dataset.status = status;
  const text = document.createElement("span");
  const name = document.createElement("span");
  name.className = "what";
  name.textContent = what;
  text.append(name, detail ? ` ${detail}` : "");
  item.append(text);
  els.hudTask.append(item);
  return item;
}

function hudStartTask(text) {
  hud.tools = {};
  hud.steps = {};
  els.hudTaskTitle.textContent = "Task · running";
  els.hudTaskGoal.textContent = text.length > 140 ? `${text.slice(0, 140)}…` : text;
  els.hudTask.replaceChildren();
  els.hudTaskResult.textContent = "";
}

function hudPlan(event) {
  els.hudTask.replaceChildren();
  hud.steps = {};
  for (const step of event.steps) hud.steps[step.id] = hudItem("pending", `${step.id}.`, step.task);
}

function hudStep(event) {
  const item = hud.steps[event.id];
  if (item) item.dataset.status = event.status;
}

function hudTool(event) {
  const args = Object.values(event.arguments || {}).join(", ").slice(0, 80);
  if (event.phase === "start") {
    hud.tools[event.call_id] = hudItem("running", event.name, args ? `· ${args}` : "");
    els.hudNow.dataset.tool = event.name;
    els.hudNow.textContent = `⚙ ${event.name}${args ? ` · ${args}` : ""}`;
    return;
  }
  const item = hud.tools[event.call_id];
  if (item) {
    item.dataset.status = event.status;
    item.title = event.summary;
  }
}

function hudFinishTask(info) {
  const tools = Object.keys(hud.tools).length;
  const seconds = (info.latency_ms / 1000).toFixed(1);
  els.hudTaskTitle.textContent = info.stopped ? "Last task · stopped" : "Last task · done";
  els.hudTaskResult.textContent =
    `${seconds} s · ${tools ? `${tools} tool call${tools === 1 ? "" : "s"}` : "no tools needed"}`;
}

function hudFailTask(message) {
  els.hudTaskTitle.textContent = "Last task · failed";
  els.hudTaskResult.textContent = message;
}

function uptimeText(seconds) {
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
  return `${Math.floor(seconds / 3600)} h ${Math.floor((seconds % 3600) / 60)} min`;
}

async function loadSystem() {
  clearTimeout(hud.systemTimer);
  hud.systemTimer = setTimeout(loadSystem, SYSTEM_REFRESH_MS);
  try {
    const res = await fetch("/status");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const status = await res.json();
    els.hudSystem.replaceChildren(...status.components.map((c) => {
      const item = document.createElement("li");
      item.dataset.state = c.state;
      const lamp = document.createElement("span");
      lamp.className = "lamp";
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = c.name;
      const detail = document.createElement("span");
      detail.className = "detail";
      detail.textContent = c.state === "off" ? `off · ${c.detail}` : c.detail;
      item.append(lamp, name, detail);
      return item;
    }));
    els.hudOverall.dataset.state = status.overall;
    els.hudOverall.textContent = status.overall === "ok" ? "All good" : "Needs attention";
    els.hudUptime.textContent = `Running for ${uptimeText(status.uptime_seconds)}`;
  } catch {
    els.hudOverall.dataset.state = "problem";
    els.hudOverall.textContent = "Unreachable";
  }
}

function toggleHud(open = !els.hud.classList.contains("open")) {
  els.hud.classList.toggle("open", open);
  els.hudToggle.setAttribute("aria-expanded", String(open));
  if (open) loadSystem();
}

// ---------- input handling ----------
function autosize() {
  els.input.style.height = "auto";
  els.input.style.height = `${els.input.scrollHeight}px`;
  refreshComposer();
}

els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (state.busy || isSpeaking()) {
    stop();
    stopSpeaking();
  } else {
    send(els.input.value.trim());
  }
});

els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    els.form.requestSubmit();
  }
});

els.input.addEventListener("input", autosize);
els.clear.addEventListener("click", clearConversation);
els.mic.addEventListener("click", toggleRecording);
els.wake.addEventListener("click", toggleWakeMode);
els.voiceToggle.addEventListener("click", () => toggleVoicePanel());
els.voiceClose.addEventListener("click", () => toggleVoicePanel(false));
els.speakMode.addEventListener("change", () => updateSpeechSetting("mode", els.speakMode.value));
els.voiceSelect.addEventListener("change", () => updateSpeechSetting("voice", els.voiceSelect.value));
els.voiceSpeed.addEventListener("input", () => updateSpeechSetting("speed", Number(els.voiceSpeed.value)));
els.voiceVolume.addEventListener("input", () => updateSpeechSetting("volume", Number(els.voiceVolume.value)));
els.voiceTest.addEventListener("click", () => {
  stopSpeaking();
  speech.active = true;
  speakStreamed("Hello, I'm Arthur. This is how I sound at this speed.", { final: true });
  speech.active = false;
});
showSpeechSettings();
els.memoryToggle.addEventListener("click", () => toggleMemoryPanel());
els.memoryClose.addEventListener("click", () => toggleMemoryPanel(false));
els.docsToggle.addEventListener("click", () => toggleDocsPanel());
els.docsClose.addEventListener("click", () => toggleDocsPanel(false));
els.docsInput.addEventListener("change", () => uploadDocuments([...els.docsInput.files]));
els.remindersToggle.addEventListener("click", () => toggleRemindersPanel());
els.remindersClose.addEventListener("click", () => toggleRemindersPanel(false));
els.hudToggle.addEventListener("click", () => toggleHud());
els.hudClose.addEventListener("click", () => toggleHud(false));
els.reminderForm.addEventListener("submit", (e) => { e.preventDefault(); addReminder(); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  toggleMemoryPanel(false);
  toggleDocsPanel(false);
  toggleVoicePanel(false);
  toggleRemindersPanel(false);
  toggleHud(false);
  stopSpeaking();
});
document.querySelectorAll(".chip").forEach((chip) =>
  chip.addEventListener("click", () => send(chip.textContent))
);

connect();
els.input.focus();
