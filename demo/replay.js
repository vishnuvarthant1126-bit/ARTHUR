// ARTHUR replay demo (GitHub Pages).
//
// The page is ARTHUR's real interface (app.js, unchanged). This script is loaded BEFORE it
// and stands in for the server: the WebSocket and the API calls are answered from
// recording.json - every event ARTHUR really sent during a recorded session (status lamps,
// plan steps, tool calls, streamed words), played back with shortened pauses.
// No AI runs here. ARTHUR itself runs locally: https://github.com/vishnuvarthant1126-bit/ARTHUR
"use strict";

(() => {
  const realFetch = window.fetch.bind(window);
  const recording = realFetch("recording.json").then((r) => r.json());
  const MAX_PAUSE_MS = 1200; // long waits (model loading, web searches) are shortened
  const MAX_TOKEN_PAUSE_MS = 45;

  const json = (body, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  const unavailable = (what) =>
    json({ detail: `${what} isn't available in the replay demo - it needs ARTHUR running on your own PC.` }, 503);

  // ---------- the API, answered from the recording ----------
  window.fetch = async (input, options = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const path = url.pathname.replace(/^.*?\/(health|status|memories|documents|reminders|voice|attachments|vision)/, "/$1");
    const method = (options.method || "GET").toUpperCase();
    const rec = await recording;
    if (path === "/health") return json(rec.health);
    if (path === "/status") return json(rec.status);
    if (path.startsWith("/memories") && method === "GET") return json({ count: 0, memories: [] });
    if (path.startsWith("/documents") && method === "GET") return json({ count: 0, documents: [] });
    if (path.startsWith("/reminders") && method === "GET") return json({ upcoming: [], recent: [] });
    if (path === "/voice/voices") return json({ default: "en_GB-alan-medium", voices: [] });
    if (path.startsWith("/voice")) return unavailable("Voice");
    if (path.startsWith("/attachments") || path.startsWith("/vision")) return unavailable("Attaching files");
    if (method !== "GET") return unavailable("Changing things");
    return realFetch(input, options); // the page's own files
  };

  // ---------- the WebSocket, replayed ----------
  const normalise = (text) => text.trim().toLowerCase().replace(/[^a-z0-9% ]/g, "");

  class ReplaySocket {
    constructor() {
      this.readyState = 0;
      this.timers = [];
      this.next = 0; // demo questions are offered in order
      setTimeout(() => {
        this.readyState = 1;
        this.onopen?.();
        this.emit({ type: "session", session_id: "replay-demo-session", history: [] });
      }, 150);
    }

    emit(event) {
      this.onmessage?.({ data: JSON.stringify(event) });
    }

    async send(raw) {
      const message = JSON.parse(raw);
      if (message.type === "stop") return this.stop();
      if (message.type === "clear") { this.stop(); this.next = 0; return this.emit({ type: "cleared" }); }
      if (message.type !== "chat") return;
      const rec = await recording;
      const wanted = normalise(message.message);
      let index = rec.turns.findIndex((t) => normalise(t.question) === wanted);
      if (index < 0) return this.explain(rec);
      this.next = index + 1;
      this.play(rec.turns[index].events);
    }

    play(events) {
      let at = 0;
      let previous = 0;
      for (const { t, event } of events) {
        const gap = t - previous;
        previous = t;
        at += Math.min(gap, event.type === "token" ? MAX_TOKEN_PAUSE_MS : MAX_PAUSE_MS);
        this.timers.push(setTimeout(() => this.emit(event), at));
      }
      this.timers.push(setTimeout(() => this.offerNext(), at + 400));
    }

    stop() {
      this.timers.forEach(clearTimeout);
      this.timers = [];
      this.emit({ type: "done", latency_ms: 0, stopped: true, model: "qwen3:8b" });
    }

    explain(rec) {
      const list = rec.turns
        .filter((t) => t.question.length > 3) // not the bare "yes"
        .map((t) => `- *${t.question}*`)
        .join("\n");
      const text =
        "This is a **replay of a real recorded session** - no AI is running on this page, " +
        "so I can only show the recorded questions:\n\n" + list +
        "\n\nTo talk to ARTHUR freely, run it on your own PC (see the README on GitHub).";
      const words = text.split(/(?<= )/);
      this.play([
        { t: 0, event: { type: "status", state: "thinking" } },
        ...words.map((word, i) => ({ t: 200 + i * 25, event: { type: "token", content: word } })),
        // "done" strictly after the last word, or a quick next question would mix in
        { t: 300 + words.length * 25, event: { type: "done", latency_ms: 0, stopped: false, model: "replay" } },
      ]);
    }

    offerNext() {
      recording.then((rec) => window.replayOffer?.(rec.turns[this.next]?.question));
    }

    close() { this.stop(); }
  }

  window.WebSocket = ReplaySocket;

  // The suggestion chips always offer the next recorded question.
  window.replayOffer = (question) => {
    const next = document.getElementById("replay-next");
    if (!next) return;
    next.hidden = !question;
    if (question) next.querySelector("span").textContent = question;
  };
  document.addEventListener("DOMContentLoaded", () => {
    const next = document.getElementById("replay-next");
    next?.addEventListener("click", () => {
      const question = next.querySelector("span").textContent;
      next.hidden = true;
      window.send?.(question);
    });
    recording.then((rec) => window.replayOffer(rec.turns[0].question));
  });
})();
