"use strict";
// Service worker: the single place that knows whether a tab is being streamed. The capture itself lives in the offscreen document
// (offscreen.js), which keeps running when the popup closes or this worker sleeps; this worker keeps the state in
// chrome.storage.session so it survives being stopped. Only one stream runs at a time (FR15). Starting and stopping run one at a
// time, in the order they were asked for.

const STATE_KEY = "stream";

async function getState() {
  const { [STATE_KEY]: stream } = await chrome.storage.session.get(STATE_KEY);
  return stream || { streaming: false };
}

function setState(value) {
  return chrome.storage.session.set({ [STATE_KEY]: value });
}

let queue = Promise.resolve();
function serial(fn) {
  const run = queue.then(fn, fn);
  queue = run.catch(() => { /* the caller sees the error; the queue carries on */ });
  return run;
}

async function ensureOffscreen() {
  if (await chrome.offscreen.hasDocument()) return;
  await chrome.offscreen.createDocument({
    url: "offscreen.html", reasons: ["USER_MEDIA"], justification: "Capture one tab's audio so it can be streamed to a Sonos room",
  });
}

async function closeOffscreen() {
  if (await chrome.offscreen.hasDocument()) await chrome.offscreen.closeDocument().catch(() => { /* already closing */ });
}

async function ended(reason, detail) {
  const before = await getState();
  if (!before.streaming) return;                                 // already recorded (the offscreen report and our own stop both arrive)
  await setState({ streaming: false, last: { reason, detail, tabId: before.tabId, room: before.room, at: Date.now() } });
  await closeOffscreen();
}

async function stopNow(reason) {
  const state = await getState();
  if (!state.streaming) return;
  await chrome.runtime.sendMessage({ target: "offscreen", type: "capture-stop", reason }).catch(() => { /* no document: nothing to stop */ });
  await ended(reason);
}

async function startNow({ tabId, room }) {
  if (typeof tabId !== "number" || typeof room !== "string") return { error: "Pick a room and a tab first." };
  await stopNow("replaced");                                     // starting a second stream stops the first, so only one runs
  let streamId;
  try {
    streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tabId });
  } catch (e) {
    return { error: `Chrome wouldn't let this tab be captured: ${e.message}` };
  }
  // Recorded before the service answers (which takes seconds on a real speaker), so closing the tab or pressing Stop meanwhile is seen.
  await setState({ streaming: true, starting: true, tabId, room, startedAt: Date.now() });
  await ensureOffscreen();
  const r = await chrome.runtime.sendMessage({ target: "offscreen", type: "capture-start", streamId, room }).catch((e) => ({ error: e.message }));
  const now = await getState();
  if (!r || r.error) {
    // Why it didn't start can arrive here before (or after) the offscreen document's own capture-ended report: record it from this
    // reply when the stream wasn't recorded as ended yet, so the reason is never lost.
    if (now.streaming && now.starting) {
      if (r && r.reason) await ended(r.reason); else await setState({ streaming: false });
    }
    await closeOffscreen();
    return { error: (r && r.error) || "The capture didn't start.", reason: r && r.reason };
  }
  const fresh = await getState();                                // read again: the service may have ended it while we were here
  if (!fresh.streaming) return { error: "The stream ended while it started.", reason: fresh.last && fresh.last.reason };
  await setState({ streaming: true, tabId, room, id: r.id, url: r.url, startedAt: fresh.startedAt });
  return { ok: true };
}

chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
  if (!msg) return false;
  if (msg.target === "background" && msg.type === "capture-ended") {          // the offscreen document: the service ended it, or the tab went
    ended(msg.reason, msg.detail);
    return false;
  }
  if (msg.target) return false;                                                // meant for the offscreen document
  if (msg.type === "status") { getState().then(respond); return true; }
  if (msg.type === "start") { serial(() => startNow(msg)).then(respond, (e) => respond({ error: e.message })); return true; }
  if (msg.type === "stop") {
    // A Stop pressed while a start is still waiting for the service must not wait behind it: tell the capture now, then record it in order.
    getState()
      .then((st) => (st.streaming && st.starting ? chrome.runtime.sendMessage({ target: "offscreen", type: "capture-stop", reason: "stop" }).catch(() => {}) : null))
      .then(() => serial(() => stopNow("stop")))
      .then(() => respond({ ok: true }), (e) => respond({ error: e.message }));
    return true;
  }
  return false;
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const state = await getState();
  if (state.streaming && state.tabId === tabId) await serial(() => stopNow("tab-closed"));
});
