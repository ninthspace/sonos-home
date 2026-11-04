"use strict";
// The offscreen document holds the capture: tab audio in, 48 kHz 16-bit stereo PCM out over a WebSocket to the service (FR15).
// The popup can close and the service worker can sleep; this page keeps streaming until it is told to stop, the tab goes, or the
// service says the stream ended.

const SERVICE_WS = "ws://localhost:8737";
const STARTED_WAIT_MS = 10000;
const MAX_BUFFERED = 1_000_000;        // bytes the socket may hold back (about 5 s of audio) before blocks are dropped instead of piling up

let current = null;        // { stream, ctx, ws, source, stopping }
let starting = false;
let pendingStop = null;    // a Stop that arrived before the capture existed (while getUserMedia was still answering)

function report(message) {
  chrome.runtime.sendMessage({ target: "background", ...message }).catch(() => { /* no receiver */ });
}

function cleanup() {
  const c = current;
  current = null;
  if (!c) return;
  try { c.stream.getTracks().forEach((t) => t.stop()); } catch { /* already stopped */ }
  try { if (c.ws && c.ws.readyState <= 1) c.ws.close(); } catch { /* already closed */ }
  try { if (c.ctx) c.ctx.close(); } catch { /* already closed */ }
}

function finish(reason, detail) {
  if (!current) return;
  cleanup();
  report({ type: "capture-ended", reason, detail });
}

function end(reason) {
  const c = current;
  if (!c) {
    if (starting) pendingStop = reason;                       // begin() picks it up as soon as there is a capture to stop
    return;
  }
  c.stopping = reason;
  const wire = reason === "tab-closed" ? "tab-closed" : "stop";        // the service knows two reasons; replaced/others are a stop to it
  try { if (c.ws && c.ws.readyState === WebSocket.OPEN) c.ws.send(JSON.stringify({ end: wire })); } catch { /* closing anyway */ }
  finish(reason);
}

async function begin({ streamId, room }) {
  if (current || starting) throw new Error("A stream is already starting or running");
  starting = true;
  pendingStop = null;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } },
      video: false,
    });
    const c = (current = { stream, ctx: null, ws: null, source: null, stopping: false });
    stream.getAudioTracks().forEach((t) => { t.onended = () => end("tab-closed"); });        // from the very start: the tab can go while the service is answering
    if (pendingStop) {                                                                          // Stop was pressed before there was anything to stop
      const reason = pendingStop;
      pendingStop = null;
      end(reason);
      throw Object.assign(new Error("The stream was stopped while it started."), { reason });
    }
    try {
      c.ctx = new AudioContext({ sampleRate: 48000 });
      await c.ctx.audioWorklet.addModule("pcm-worklet.js");
      c.source = c.ctx.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(c.ctx, "pcm-blocks", { numberOfInputs: 1, numberOfOutputs: 0, channelCount: 2, channelCountMode: "explicit" });
      const ws = (c.ws = new WebSocket(`${SERVICE_WS}/live/ingest?room=${encodeURIComponent(room)}`));
      ws.binaryType = "arraybuffer";
      const started = await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error("The service didn't answer in time")), STARTED_WAIT_MS);
        ws.onmessage = (ev) => {
          let msg = null;
          try { msg = JSON.parse(ev.data); } catch { return; }
          clearTimeout(timer);
          if (msg.started) resolve(msg); else reject(new Error(msg.detail || `The service ended the stream: ${msg.ended || "unknown"}`));
        };
        ws.onerror = () => { clearTimeout(timer); reject(new Error("Couldn't reach the service on this Mac")); };
        ws.onclose = (ev) => {
          clearTimeout(timer);
          reject(Object.assign(new Error(ev.code === 4404 ? "That room isn't available any more." : c.stopping ? "The stream was stopped while it started." : "The service closed the connection"),
                               { reason: c.stopping || undefined }));
        };
      });
      if (current !== c) throw Object.assign(new Error("The stream was stopped while it started."), { reason: c.stopping || "stop" });       // the tab went, or Stop was pressed, meanwhile
      if (stream.getAudioTracks().every((t) => t.readyState === "ended")) throw Object.assign(new Error("The tab was closed while the stream started."), { reason: "tab-closed" });
      node.port.onmessage = (ev) => { if (ws.readyState === WebSocket.OPEN && ws.bufferedAmount < MAX_BUFFERED) ws.send(ev.data); };
      c.source.connect(node);
      ws.onmessage = (ev) => {
        let msg = null;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.ended) finish(msg.ended, msg.detail);
      };
      ws.onclose = () => finish(c.stopping ? c.stopping : "service-error");
      return { id: started.id, url: started.url };
    } catch (e) {
      if (!e.reason && c.stopping) e.reason = c.stopping;       // closing a connecting socket also fires `error`: the cause is still the stop
      cleanup();
      throw e;
    }
  } finally {
    starting = false;
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
  if (!msg || msg.target !== "offscreen") return false;
  if (msg.type === "capture-start") {
    begin(msg).then((r) => respond({ ok: true, ...r }), (e) => respond({ error: e.message, reason: e.reason }));
    return true;
  }
  if (msg.type === "capture-stop") {
    end(msg.reason || "stop");
    respond({ ok: true });
  }
  return false;
});
