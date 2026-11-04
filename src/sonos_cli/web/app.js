"use strict";
// Sonos at home: browse the Mac's music folders, play them on a room, control the room. No build step.

const $ = (id) => document.getElementById(id);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window: carry on without */ } },
};

let lastAnnounced = "";
let loadSeq = 0;
let loading = 0;
const state = {
  rooms: [], roomUid: store.get("room"), roots: [], path: "", parent: null,
  entries: [], nextOffset: null, filter: "", now: null, unreachable: false, draggingVolume: false, folderError: false,
  anchor: null, holdUntil: 0, holdUid: null, frame: 0, meta: null,
  jump: null, jumpNote: "", jumpNoteTimer: 0, balance: {},
  folders: { editable: false, list: [], configPath: "" }, folderBusy: false, fp: { path: "", parent: null },
  seek: { dragging: false, preview: null, uid: null, pointerId: null, seq: 0, keyTarget: null, keyTimer: 0, holdUntil: 0, holdUid: null, noteTimer: 0 },
  view: "music", playlists: [], plOpen: null, plDetail: null, plConfirmDelete: false, plArmedAt: 0, plFocus: null,
};

// ---- talking to the service ---------------------------------------------------------------------------

async function api(path, body, opts = {}) {
  // Reads give up after 3.5 s (the service is unreachable). Writes can legitimately take long (a hi-res file is
  // converted first, an append waits for the previous queueing), so they get two minutes. `opts.timeout` lengthens a read
  // whose service side has its own, longer limit (the folder picker's 5 s).
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), body === undefined ? (opts.timeout || 3500) : 120000);
  try {
    const res = await fetch(path, body === undefined ? { signal: ctl.signal } : {
      method: "POST", signal: ctl.signal,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    setReachable(true);
    let data = null;
    try { data = await res.json(); } catch { /* not JSON */ }
    if (!res.ok) throw Object.assign(new Error((data && data.error) || `HTTP ${res.status}`), { status: res.status });
    return data;
  } catch (e) {
    if (e.status === undefined) {
      if (e.name === "AbortError" && body !== undefined) throw new Error("The service is taking too long to answer; the request may still finish.");
      if (opts.slow) throw e;                              // a slow speaker is not a dead service
      setReachable(false);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

function setReachable(ok) {
  if (state.unreachable === !ok) return;
  state.unreachable = !ok;
  $("banner").hidden = ok;
  if (!ok) { state.now = null; state.anchor = null; renderPlayer(); }      // nothing to extrapolate from once the service is gone
  renderRooms();
}

// ---- rooms --------------------------------------------------------------------------------------------

async function pollRooms() {
  try {
    state.rooms = await api("/api/rooms");
    if (!state.rooms.some((r) => r.uid === state.roomUid) && state.rooms.length) {
      selectRoom(state.rooms[0].uid);                                   // the selected room vanished: switch as a click would (queue, jump and seek state cleared)
    }
    renderRooms();
    if (state.view === "rooms") loadBalances(false);                      // a room that has just appeared gets its value
  } catch { /* the banner says so */ }
}

const STRIP_COLOURS = ["#3d7bff", "#ff4fa3", "#2fe0a0", "#ff8a3d", "#a78bff", "#3ddcff"];

const roomEls = new Map();         // uid -> { li, btn, name, word, led, fader, level }: a strip is built once and updated in place, so a poll can't take a fader out of a hand
const faders = {};                 // uid -> the fader's send state
function faderState(uid) { return faders[uid] || (faders[uid] = { dragging: false, inflight: false, next: null, last: 0, timer: 0, holdUntil: 0 }); }

// A button released anywhere (off the fader, outside the window, after a context menu) must not leave a fader marked as held.
for (const type of ["pointerup", "pointercancel"]) window.addEventListener(type, () => {
  for (const f of Object.values(faders)) if (f.dragging) { f.dragging = false; f.holdUntil = performance.now() + 1000; }
}, true);
window.addEventListener("blur", () => { for (const f of Object.values(faders)) f.dragging = false; });

function selectRoom(uid) {
  resetSeek();
  resetJump();
  state.roomUid = uid;
  store.set("room", uid);
  state.queue = null; lastQueueCurrent = null;
  renderQueue(); renderRooms(); pollState(); pollQueue();
}

// A strip's fader sets its group's volume (FR9). Requests: one in flight per room, at least 250 ms between starts, the newest value
// sent when the last one returns, and the final value on release.
function sendRoomVolume(uid, level) {
  const f = faderState(uid);
  f.next = level;
  pumpRoomVolume(uid);
}

async function pumpRoomVolume(uid) {
  const f = faderState(uid);
  if (f.inflight || f.next === null) return;
  const wait = 250 - (performance.now() - f.last);
  if (wait > 0) { if (!f.timer) f.timer = setTimeout(() => { f.timer = 0; pumpRoomVolume(uid); }, wait); return; }
  const level = f.next;
  f.next = null; f.inflight = true; f.last = performance.now();
  try { await api(`/api/rooms/${encodeURIComponent(uid)}/volume`, { level }); }
  catch (e) { note(e.message, true); }
  f.inflight = false;
  if (f.next !== null) { pumpRoomVolume(uid); return; }
  f.holdUntil = performance.now() + 1000;                  // a poll still carrying the old volume can't pull the fader back
  pollRooms(); pollState();
}

function buildStrip(r) {
  const uid = r.uid;
  const li = document.createElement("li");
  li.className = "room";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "strip";
  btn.dataset.uid = uid;
  const cap = document.createElement("span");
  cap.className = "cap";
  const name = document.createElement("span");
  name.className = "rname";
  const st = document.createElement("span");
  st.className = "state";
  const led = document.createElement("span");
  led.className = "led";
  const word = document.createElement("span");
  st.append(led, word);
  btn.append(cap, name, st);
  const chan = document.createElement("div");
  chan.className = "chan";
  const meter = document.createElement("span");
  meter.className = "meter";
  meter.setAttribute("aria-hidden", "true");
  const fader = document.createElement("input");
  fader.type = "range"; fader.min = "0"; fader.max = "100"; fader.step = "1"; fader.className = "strip-fader";
  chan.append(meter, fader);
  const level = document.createElement("span");
  level.className = "vol";
  level.setAttribute("aria-hidden", "true");
  li.append(btn, chan, level);
  li.addEventListener("click", (ev) => { if (!ev.target.closest(".strip-fader")) selectRoom(uid); });          // anywhere on the strip but the fader selects the room
  fader.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;                                                 // a right-click or ctrl-click is not a drag
    try { fader.setPointerCapture(ev.pointerId); } catch { /* a synthetic pointer */ }
    faderState(uid).dragging = true;
  });
  const released = () => { const f = faderState(uid); if (!f.dragging) return; f.dragging = false; f.holdUntil = performance.now() + 1000; };
  fader.addEventListener("pointerup", released);
  fader.addEventListener("pointercancel", released);
  fader.addEventListener("lostpointercapture", released);
  fader.addEventListener("input", () => {
    const f = faderState(uid), v = Number(fader.value);
    f.holdUntil = performance.now() + 1000;
    level.textContent = String(v);
    sendRoomVolume(uid, v);
  });
  return { li, btn, name, word, led, fader, level };
}

function updateStrip(el, r, order) {
  const { li, btn, name, word, fader, level } = el;
  const members = r.members.filter((m) => m.visible).length;
  const hasVol = typeof r.volume === "number";
  btn.style.setProperty("--cap", STRIP_COLOURS[order.indexOf(r.uid) % STRIP_COLOURS.length]);
  li.style.setProperty("--vol", String(hasVol ? r.volume : 0));
  name.textContent = r.name;
  if (members > 1) {
    const plus = document.createElement("span");
    plus.className = "plus";
    plus.textContent = `+${members - 1}`;
    name.append(plus);
  }
  word.textContent = state.unreachable ? "Unknown" : !r.online ? "Offline" : r.state === "PLAYING" ? "Playing" : r.state === "PAUSED_PLAYBACK" ? "Paused" : "Stopped";
  btn.setAttribute("aria-current", String(r.uid === state.roomUid));
  btn.setAttribute("aria-label", `${r.name}${members > 1 ? ` and ${members - 1} more` : ""}, ${word.textContent.toLowerCase()}${hasVol ? `, volume ${r.volume}` : ""}`);
  li.classList.toggle("sel", r.uid === state.roomUid);
  li.classList.toggle("playing", r.state === "PLAYING" && r.online && !state.unreachable);
  li.classList.toggle("paused", r.state === "PAUSED_PLAYBACK" && r.online && !state.unreachable);
  li.classList.toggle("offline", !r.online);
  li.classList.toggle("novol", !hasVol);
  const usable = r.online && hasVol && !state.unreachable;
  if (fader.disabled === usable) fader.disabled = !usable;
  fader.setAttribute("aria-disabled", String(!usable));
  fader.setAttribute("aria-label", `${r.name} volume`);
  const f = faderState(r.uid);
  if (!f.dragging && !f.inflight && f.next === null && performance.now() >= f.holdUntil && hasVol) {
    if (fader.value !== String(r.volume)) fader.value = String(r.volume);
    level.textContent = String(r.volume);
  } else if (!hasVol) {
    level.textContent = "–";
  }
}

function renderRooms() {
  const ul = $("room-list");
  if (!state.rooms.length) {
    const li = document.createElement("li");
    li.textContent = state.unreachable ? "Rooms unknown while the service is unreachable." : "No rooms found yet. Searching the network…";
    li.className = "none";
    ul.replaceChildren(li);
    roomEls.clear();
  } else {
    const none = ul.querySelector("li.none");
    if (none) none.remove();
    const order = state.rooms.map((r) => r.uid).sort();                 // a room keeps its colour when others are renamed
    const seen = new Set();
    let prev = null;
    for (const r of state.rooms) {
      let el = roomEls.get(r.uid);
      if (!el) { el = buildStrip(r); roomEls.set(r.uid, el); }
      updateStrip(el, r, order);
      const want = prev ? prev.nextSibling : ul.firstChild;
      if (el.li !== want) ul.insertBefore(el.li, want);                  // moved only when out of place (a moved strip would lose focus)
      prev = el.li;
      seen.add(r.uid);
    }
    for (const [uid, el] of roomEls) if (!seen.has(uid)) { el.li.remove(); roomEls.delete(uid); delete faders[uid]; }
  }
  updatePlayLabel();                                       // the Play buttons in the playlist editor and Web page view name the room
  if (state.view === "rooms") renderRoomsPanel(false);
}

// ---- now playing --------------------------------------------------------------------------------------

async function pollState() {
  if (!state.roomUid) return;
  const asked = state.roomUid;
  let now = null;
  try {
    now = await api(`/api/rooms/${encodeURIComponent(asked)}/state`);
  } catch { now = null; }
  if (asked !== state.roomUid) return;                    // the room was switched while this was in flight
  const prevIndex = state.now && state.now.track ? state.now.track.index : null;
  state.now = now;
  reanchor(now);
  renderPlayer();
  markCurrent();
  const idx = now && now.track ? now.track.index : null;
  if (idx !== prevIndex) pollQueue();                      // the track changed: the highlighted row moves
}

function queueItem(index) {
  // Only when the service says the room is playing that queue item (`current` is null for a radio or line-in source that merely
  // reports track 1), so a stale queue never names audio that isn't from it.
  const q = state.queue;
  if (!q || index === null || index === undefined || q.current !== index) return null;
  return q.items.find((it) => it.index === index) || null;
}
function queueTitle(index) { const item = queueItem(index); return item ? item.title : ""; }

function seconds(t) {
  const m = /^(\d+):(\d\d):(\d\d)$/.exec(t || "");
  return m ? Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]) : 0;
}

function clock(t) {
  const m = /^(\d+):(\d\d):(\d\d)$/.exec(t || "");
  if (!m) return "0:00";
  const h = Number(m[1]), min = Number(m[2]), sec = m[3];
  return h ? `${h}:${m[2]}:${sec}` : `${min}:${sec}`;
}

function clockSecs(total) {
  const t = Math.max(0, Math.floor(total)), h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), sec = String(t % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

// ---- the position between polls -----------------------------------------------------------------------
// A speaker is read every 2 s at best, and the reading the service hands out can already be a poll old. So the bar and the time are
// drawn from the reading's age (the service's `now` minus its `updated`, both on the Mac's clock) plus the time since the answer
// arrived, and a reading moves them only when it is a new one: the same cached reading repeated can't pull them back.

function shownPosition() {
  const a = state.anchor;
  if (!a || a.uid !== state.roomUid) return null;                                 // a reading of another room (just switched from) is not drawn
  if (state.seek.preview !== null && state.seek.uid === state.roomUid) return Math.min(a.total, Math.max(0, state.seek.preview));      // where the pointer or the keys have put it
  const playing = a.state === "PLAYING" && a.reachable && !state.unreachable;
  return Math.min(a.total, Math.max(0, a.position + (playing ? a.age + (performance.now() - a.at) / 1000 : 0)));
}

function reanchor(n) {
  const total = seconds(n && n.duration);
  const prev = state.anchor, shown = shownPosition();
  if (!n || !(total > 0)) { state.anchor = null; return; }                       // no duration (a stream): nothing to draw
  const index = n.track ? n.track.index : null, reachable = n.reachable !== false;
  if (prev && prev.uid === state.roomUid && prev.updated === n.updated && prev.state === n.state && prev.index === index &&
      prev.total === total && prev.reachable === reachable) return;               // the monitor handed out the same reading again
  if (performance.now() < state.holdUntil && state.holdUid === state.roomUid && n.state === "PLAYING") return;       // a reading from before a Pause that just succeeded
  const age = typeof n.now === "number" && typeof n.updated === "number" ? Math.max(0, n.now - n.updated) : 0;
  if (performance.now() < state.seek.holdUntil && state.seek.holdUid === state.roomUid && shown !== null && Math.abs(seconds(n.position) + age - shown) > 3) return;    // a poll in flight from before a seek
  let position = seconds(n.position);
  const playing = n.state === "PLAYING" && reachable;
  let at = performance.now();
  if (playing && prev && prev.uid === state.roomUid && prev.index === index && prev.total === total && prev.state === "PLAYING") {
    const fresh = position + age;
    if (shown !== null && fresh < shown && shown - fresh <= 1.5) { position = shown; at = performance.now(); state.anchor = { uid: state.roomUid, updated: n.updated, state: n.state, index, total, position, age: 0, at, reachable }; return; }
  }
  state.anchor = { uid: state.roomUid, updated: n.updated, state: n.state, index, total, position, age, at, reachable };
}

let drawnSecond = -1, drawnWidth = "";
function drawProgress(force) {
  const a = state.anchor, pos = shownPosition();
  const pct = pos === null ? 0 : (pos / a.total) * 100;
  const width = `${pct.toFixed(3)}%`;
  if (width !== drawnWidth) { drawnWidth = width; $("progress-fill").style.width = width; $("progress-thumb").style.left = width; }
  const whole = pos === null ? -1 : Math.floor(pos);
  if (force || whole !== drawnSecond) {                                           // the time text and the slider's values change once a second at most
    drawnSecond = whole;
    const bar = $("progress");
    const set = (k, v) => { if (bar.getAttribute(k) !== v) bar.setAttribute(k, v); };      // only when it changed
    set("aria-valuemax", String(pos === null ? 0 : a.total));
    set("aria-valuenow", String(whole < 0 ? 0 : whole));
    set("aria-valuetext", pos === null ? "" : `${clockSecs(whole)} of ${clockSecs(a.total)}`);
    set("aria-disabled", String(!seekable()));
    renderMeta();
  }
}

// ---- seek: click or drag on the bar, or the keys on it --------------------------------------------------
function seekable() {
  const a = state.anchor;       // only with a track that has a length, in a room that is playing or paused and answering
  return !!a && a.uid === state.roomUid && a.reachable && !state.unreachable && (a.state === "PLAYING" || a.state === "PAUSED_PLAYBACK");
}

function barPosition(ev) {
  const r = $("progress").getBoundingClientRect(), total = state.anchor.total;
  const f = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
  return Math.min(total - 1, Math.floor(f * total));                              // never the very end: a click at the right edge isn't refused
}

function resetSeek() {                                                              // a room switch: nothing pending belongs to the new room
  clearTimeout(state.seek.keyTimer);
  Object.assign(state.seek, { dragging: false, preview: null, uid: null, pointerId: null, keyTarget: null, keyTimer: 0 });
  seekNote("");
}

function seekNote(text) {
  const el = $("seek-note");                                                         // kept rendered (empty) so a screen reader announces a change
  clearTimeout(state.seek.noteTimer);
  el.textContent = text;
  if (text) state.seek.noteTimer = setTimeout(() => seekNote(""), 6000);
}

async function sendSeek(pos, uid) {
  if (uid !== state.roomUid) return;                                               // the room was switched since the pointer or key was used
  const mine = ++state.seek.seq;
  state.seek.keyTarget = null;
  try {
    await api(`/api/rooms/${encodeURIComponent(uid)}/seek`, { position: pos });
    if (mine !== state.seek.seq) return;                                           // a later seek is in flight: it settles the bar
    seekNote("");
    if (state.anchor && state.anchor.uid === uid) Object.assign(state.anchor, { position: pos, age: 0, at: performance.now() });
    state.seek.holdUntil = performance.now() + 1500;                              // readings from before the seek can't pull the bar back
    state.seek.holdUid = uid;
  } catch (e) {
    if (mine !== state.seek.seq) return;
    seekNote(e.message);                                                           // the bar returns to the room's own position below
  }
  if (state.seek.keyTimer === 0 && !state.seek.dragging) state.seek.preview = null;
  drawProgress(true);
  pollState();
}

(function wireSeek() {
  const bar = $("progress");
  bar.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0 || !seekable() || state.seek.dragging) return;
    try { bar.setPointerCapture(ev.pointerId); } catch { /* a synthetic pointer: carry on without capture */ }
    clearTimeout(state.seek.keyTimer);
    state.seek.keyTimer = 0;
    Object.assign(state.seek, { dragging: true, uid: state.roomUid, pointerId: ev.pointerId, preview: barPosition(ev) });
    drawProgress(true);
    ev.preventDefault();
  });
  const endDrag = () => { state.seek.dragging = false; state.seek.preview = null; state.seek.pointerId = null; drawProgress(true); };
  bar.addEventListener("pointermove", (ev) => {
    if (!state.seek.dragging || ev.pointerId !== state.seek.pointerId) return;
    if (!seekable() || state.seek.uid !== state.roomUid) return endDrag();          // the track or the service went away mid-drag
    state.seek.preview = barPosition(ev);
    drawProgress(false);
  });
  bar.addEventListener("pointerup", (ev) => {
    if (!state.seek.dragging || ev.pointerId !== state.seek.pointerId) return;
    if (!seekable() || state.seek.uid !== state.roomUid) return endDrag();
    state.seek.dragging = false;
    state.seek.pointerId = null;
    state.seek.preview = barPosition(ev);
    sendSeek(state.seek.preview, state.seek.uid);
  });
  bar.addEventListener("pointercancel", endDrag);
  bar.addEventListener("keydown", (ev) => {
    if (!seekable() || state.seek.dragging) return;
    const uid = state.roomUid, total = state.anchor.total, cur = state.seek.keyTarget !== null ? state.seek.keyTarget : Math.floor(shownPosition());
    let t = null;
    if (ev.key === "ArrowRight" || ev.key === "ArrowUp") t = cur + 10;
    else if (ev.key === "ArrowLeft" || ev.key === "ArrowDown") t = cur - 10;
    else if (ev.key === "Home") t = 0;
    else if (ev.key === "End") t = total - 1;
    if (t === null) return;
    ev.preventDefault();
    t = Math.min(total - 1, Math.max(0, t));
    state.seek.keyTarget = t;
    state.seek.preview = t;
    state.seek.uid = uid;
    drawProgress(true);
    clearTimeout(state.seek.keyTimer);
    state.seek.keyTimer = setTimeout(() => { state.seek.keyTimer = 0; sendSeek(t, uid); }, 300);        // one seek, 300 ms after the last key
  });
})();

function frame() { state.frame = requestAnimationFrame(frame); drawProgress(false); }

function renderMeta() {
  const m = state.meta;
  if (!m) return;
  const parts = [m.source, m.word];
  if (m.shown) {
    const pos = shownPosition();
    parts.push(pos === null ? `${clock(m.position)} / ${clock(m.duration)}` : `${clockSecs(pos)} / ${clockSecs(state.anchor.total)}`);
  }
  $("np-meta").textContent = parts.join(", ");
}

function sourceLabel(src) {
  if (src === "ours") return "this Mac";
  if (src && src.startsWith("other-host:")) return "from " + src.slice("other-host:".length);
  return "other source";
}

function renderPlayer() {
  const p = document.querySelector(".player");
  const n = state.now;
  const live = !!n && n.state === "PLAYING";
  p.classList.toggle("live", live);
  if (state.unreachable || !n) {
    $("np-title").textContent = state.unreachable ? "Service unreachable" : "Nothing playing";
    $("np-meta").textContent = state.unreachable ? "Waiting for the service to answer." : "Pick a room and a file.";
  } else {
    const t = n.track || {};
    const shown = t.title || queueTitle(t.index);      // the service names page tracks itself; any other track that reports no title gets the queue's label
    const artist = t.artist || (t.title ? "" : (queueItem(t.index) || {}).artist || "");     // as the queue rows show it: "Title by Artist"
    const heard = shown ? shown + (artist ? ` by ${artist}` : "") : "";
    $("np-title").textContent = heard || (n.state === "STOPPED" ? "Nothing playing" : "Playing");
    if (heard && heard !== lastAnnounced) { lastAnnounced = heard; $("announce").textContent = `Now playing ${heard}`; }
    state.meta = { source: sourceLabel(n.source), word: n.state === "PLAYING" ? "playing" : n.state === "PAUSED_PLAYBACK" ? "paused" : "stopped",
                   shown, position: n.position, duration: n.duration };
    renderMeta();
  }
  if (state.unreachable || !n) state.meta = null;
  const toggle = $("toggle");
  toggle.querySelector("path").setAttribute("d", live ? "M6 4h4.5v16H6zM13.5 4H18v16h-4.5z" : "M7 4.5v15l12-7.5z");
  toggle.setAttribute("aria-label", live ? "Pause" : "Play");
  drawProgress(true);
  if (n && !state.draggingVolume && typeof n.volume === "number") {
    $("volume").value = n.volume;
    $("volume-out").textContent = n.volume;
  } else if (!n) {
    $("volume-out").textContent = "–";
  }
}

async function transport(action) {
  if (!state.roomUid) return;
  if (action !== "pause" && action !== "stop") state.holdUntil = 0;              // Play, Next and Previous must be seen at once
  try {
    await api(`/api/rooms/${encodeURIComponent(state.roomUid)}/transport`, { action });
    if ((action === "pause" || action === "stop") && state.anchor) {              // stop the bar now, not when the next reading says so
      Object.assign(state.anchor, { position: shownPosition(), state: action === "pause" ? "PAUSED_PLAYBACK" : "STOPPED", age: 0, at: performance.now() });
      state.holdUntil = performance.now() + 1500;
      state.holdUid = state.roomUid;
      drawProgress(true);
    }
  }
  catch (e) { note(e.message, true); }
  pollState(); pollRooms();
}

let volumeTimer = null;
function setVolume(level) {
  $("volume-out").textContent = level;
  clearTimeout(volumeTimer);
  volumeTimer = setTimeout(async () => {
    try { await api(`/api/rooms/${encodeURIComponent(state.roomUid)}/volume`, { level: Number(level) }); }
    catch (e) { note(e.message, true); }
    pollState(); pollRooms();                              // the strips show each group's volume too
  }, 150);
}

// ---- queue --------------------------------------------------------------------------------------------

let queueSeq = 0;
async function pollQueue() {
  if (!state.roomUid) return;
  const asked = state.roomUid, mine = ++queueSeq;
  let q = null;
  try { q = await api(`/api/rooms/${encodeURIComponent(asked)}/queue`, undefined, { slow: true }); }
  catch (e) {
    if (mine === queueSeq && asked === state.roomUid) $("queue-note").textContent = e.status !== undefined ? e.message : "Couldn't read the queue just now.";
    return;                                                // keep what is on screen (and the user's focus and scroll) as it was
  }
  if (mine !== queueSeq || asked !== state.roomUid) return;
  state.queue = q;
  renderQueue();
}

// ---- jumping to a queue item ----------------------------------------------------------------------------
// One jump at a time: a double-click, or a click on another row while one is pending, sends nothing more. The row is marked pending in
// the same task as the click, the page asks for state and queue the moment the speaker answers, and a failure says why.
function jumpNote(text) {
  clearTimeout(state.jumpNoteTimer);
  state.jumpNote = text;
  if (text) state.jumpNoteTimer = setTimeout(() => { state.jumpNote = ""; renderQueue(); }, 10000);
}

function resetJump() { state.jump = null; jumpNote(""); $("queue-note").textContent = ""; $("queue-note").classList.remove("error"); }

async function jumpTo(index) {
  if (state.jump) return;
  const uid = state.roomUid, mine = { uid, index };
  state.jump = mine;
  renderQueue();
  let timedOut = false;
  const giveUp = setTimeout(() => {                                               // the speaker has not answered: stop showing it as pending
    if (state.jump !== mine) return;
    timedOut = true;
    state.jump = null;
    jumpNote("The speaker hasn't answered yet. Check the queue in a moment.");
    renderQueue();
  }, 10000);
  let error = "";
  try { await api(`/api/rooms/${encodeURIComponent(uid)}/queue/jump`, { index }); }
  catch (e) { error = e.message; }
  clearTimeout(giveUp);
  const stillMine = state.jump === mine;
  if (stillMine) state.jump = null;                                                 // always: the room may have changed by a route that never calls resetJump
  if (uid !== state.roomUid) return;                                               // the room was switched meanwhile
  if (!stillMine && state.jump) return;                                            // a newer jump is pending: this late answer doesn't speak for the queue
  if (error) jumpNote(error);
  else if (!timedOut || state.jumpNote) {
    jumpNote("");
    if (state.queue && state.queue.items.some((it) => it.index === index)) state.queue.current = index;      // current at once; the next reading confirms
  }
  renderQueue();
  pollState(); pollQueue();
}

function renderQueue() {
  const ol = $("queue-list");
  const had = ol.contains(document.activeElement) ? [...ol.querySelectorAll("button")].indexOf(document.activeElement) : -1;
  ol.replaceChildren();
  const q = state.queue;
  if (!q) { $("clear-queue").hidden = true; return; }
  $("queue-note").textContent = state.jumpNote || (q.items.length ? "" : "The queue is empty. Play something to fill it.");
  $("queue-note").classList.toggle("error", !!state.jumpNote);
  $("clear-queue").hidden = !q.items.length;
  for (const it of q.items) {
    const li = document.createElement("li");
    li.dataset.current = String(it.index === q.current);
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("aria-current", String(it.index === q.current));
    if (state.jump && state.jump.uid === state.roomUid && state.jump.index === it.index) b.setAttribute("aria-busy", "true");      // the pending mark survives a re-render
    const pos = document.createElement("span");
    pos.className = "pos";
    pos.textContent = String(it.index + 1);
    const title = document.createElement("span");
    title.textContent = it.title + (it.artist ? ` by ${it.artist}` : "");
    b.append(pos, title);
    if (it.host) {
      const from = document.createElement("span");
      from.className = "from";
      from.textContent = `from ${it.host}`;
      b.append(from);
    }
    b.addEventListener("click", (ev) => { if (ev.detail > 1) return; jumpTo(it.index); });      // the second click of a double-click is not another jump (Enter has detail 0)
    li.append(b);
    ol.append(li);
  }
  const buttons = ol.querySelectorAll("button");
  if (had >= 0 && buttons[had]) buttons[had].focus({ preventScroll: true });            // a re-render must not drop keyboard focus
  const cur = ol.querySelector('li[data-current="true"]');
  if (cur && q.current !== lastQueueCurrent) {              // scroll the list only, never the page
    const top = cur.offsetTop - ol.offsetTop;
    if (top < ol.scrollTop || top + cur.offsetHeight > ol.scrollTop + ol.clientHeight) ol.scrollTop = Math.max(0, top - ol.clientHeight / 2);
  }
  lastQueueCurrent = q.current;
}
let lastQueueCurrent = null;

// ---- browsing -----------------------------------------------------------------------------------------

function note(text, isError) {
  const el = $("listing-note");
  el.textContent = text || "";
  el.classList.toggle("error", !!isError);
}

async function loadFolder(path, offset = 0) {
  const mine = ++loadSeq;
  loading++;
  $("more").disabled = true;
  try {
    const page = await api(`/api/browse?path=${encodeURIComponent(path)}&offset=${offset}`);
    if (mine !== loadSeq) return;                          // a newer request took over; drop this reply
    state.folderError = false;
    $("empty").hidden = true;
    if (offset === 0) { state.entries = []; }
    state.path = page.path;
    state.parent = page.parent;
    state.entries.push(...page.entries);
    state.nextOffset = page.next_offset;
    if (page.path === "") state.roots = page.entries.map((e) => e.path);
    note("");
    renderListing();
  } catch (e) {
    if (mine !== loadSeq) return;
    if (offset > 0 && e.status === undefined) { note("Couldn't load more of this folder; the service didn't answer.", true); return; }
    state.folderError = true;
    state.entries = [];
    state.nextOffset = null;
    if (e.status === 409) {
      const m = /Add one to (.+?), for example: (.+)$/s.exec(e.message);
      $("empty-message").textContent = "Playing needs at least one folder to look in.";
      $("config-path").textContent = m ? m[1] : "~/Library/Application Support/sonos/config.toml";
      $("config-example").textContent = m ? m[2] : 'music_folders = ["~/Dropbox/Music"]';
      $("empty").hidden = false;
      note("");
    } else if (e.status !== undefined) {
      $("empty").hidden = true;
      note(e.message, true);
    }
    renderListing();
  } finally {
    loading--;
    if (mine === loadSeq) $("more").disabled = false;
    if (!loading && state.filter.trim() && state.nextOffset !== null) loadAllPages();   // a filter typed meanwhile
  }
}

async function loadAllPages() {
  // The filter searches the whole folder, not just the pages already shown. Never while another load is pending:
  // `state.path` still names the old folder then, and loading its pages would cancel the navigation.
  while (state.nextOffset !== null && state.filter.trim() && !loading) {
    const before = state.entries.length, path = state.path;
    await loadFolder(path, before);
    if (state.path !== path || state.entries.length === before) break;
  }
}

function rootOf(path) { return state.roots.find((r) => path === r || path.startsWith(r + "/")) || null; }

function renderCrumbs() {
  const ol = $("crumbs");
  ol.replaceChildren();
  const add = (label, path) => {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.addEventListener("click", () => { state.filter = ""; $("filter").value = ""; loadFolder(path); });
    li.append(b);
    ol.append(li);
    return b;
  };
  add("Music", "");
  const root = rootOf(state.path);
  if (root) {
    add(root.split("/").pop() || root, root);
    let acc = root;
    for (const part of state.path.slice(root.length).split("/").filter(Boolean)) {
      acc += "/" + part;
      add(part, acc);
    }
  }
  const last = ol.lastElementChild && ol.lastElementChild.querySelector("button");
  if (last) last.setAttribute("aria-current", "page");
  const up = $("up");
  up.hidden = state.path === "";
  up.onclick = () => { state.filter = ""; $("filter").value = ""; loadFolder(state.parent ?? ""); };
}

function fmtSize(n) { return n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1e3)) + " kB"; }

// ---- music folders: list, add, remove (FR23) --------------------------------------------------------------------
// The service allows changes only from the Mac itself and says so in `editable`; from another device the list is read-only.

async function loadFolders() {
  try {
    const r = await api("/api/folders", undefined, { slow: true, timeout: 8000 });     // the service stats each root: a dead mount is slow, not a dead service
    state.folders = { editable: !!r.editable, list: r.folders || [], configPath: r.config_path || "" };
  } catch { /* keep what we had; other polls raise the unreachable banner */ }
  renderFolders();
}

function folderNote(text, isError) {
  const el = $("folder-note");
  el.textContent = text || "";
  el.classList.toggle("error", !!isError);
}

function renderFolders() {
  const f = state.folders, atRoot = state.path === "";
  const sec = $("folders");
  sec.hidden = !(atRoot && f.list.length);
  $("add-folder").hidden = !f.editable;
  const ul = $("folder-list");
  ul.replaceChildren();
  for (const it of f.list) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "fname";
    name.textContent = it.name;
    const path = document.createElement("span");
    path.className = "fpath";
    path.textContent = it.path;
    li.append(name, path);
    if (!it.exists) {
      const miss = document.createElement("span");
      miss.className = "missing";
      miss.textContent = "not found";
      li.append(miss);
    }
    if (f.editable) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "quiet";
      b.textContent = "Remove";
      b.setAttribute("aria-label", `Remove ${it.name} from the music folders`);
      b.addEventListener("click", () => openRemove(it));
      li.append(b);
    }
    ul.append(li);
  }
  if (!f.editable && f.list.length) folderNote(`Folders are changed on the Mac itself, in ${f.configPath || "its config file"}.`);
  else if (!$("folder-note").classList.contains("error")) folderNote("");
  // the empty view: Add folder where the Mac can change folders, the config file by hand elsewhere
  $("empty-add").hidden = !f.editable;
  $("empty-by-hand").hidden = f.editable;
}

function busyFolders(on) {
  state.folderBusy = on;
  for (const id of ["add-folder", "empty-add", "fp-add-path", "fr-confirm"]) $(id).disabled = on;
  $("fp-use").disabled = on || !(state.fp && state.fp.path);
}

async function changeFolders(path, what, noteEl) {
  if (state.folderBusy) return false;                                            // one change at a time, however often a key is pressed
  busyFolders(true);
  try {
    const r = await api(`/api/folders/${what}`, { path });
    state.folders = { ...state.folders, editable: true, list: r.folders || [] };
    await loadFolder("");                                                        // the listing now has (or lacks) the folder
    renderFolders();
    return true;
  } catch (e) {
    noteEl.textContent = e.message;
    return false;
  } finally {
    busyFolders(false);
  }
}

let fpSeq = 0;
async function fpLoad(path, focusList = true) {
  const mine = ++fpSeq;                                                          // only the latest request may draw
  $("fp-note").textContent = "";
  try {
    const r = await api(`/api/folders/browse?path=${encodeURIComponent(path)}`, undefined, { slow: true, timeout: 8000 });
    if (mine !== fpSeq) return;
    state.fp = { path: r.path, parent: r.parent };
    $("fp-where").textContent = r.path || "Pick a place to start looking.";
    $("fp-use").disabled = state.folderBusy || !r.path;
    $("fp-up").disabled = !r.path;
    const ul = $("fp-list");
    ul.replaceChildren();
    for (const e of r.entries) {
      const li = document.createElement("li");
      if (e.readable) {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = e.name;
        b.addEventListener("click", () => fpLoad(e.path));
        li.append(b);
      } else {
        li.className = "locked";
        const n = document.createElement("span");
        n.textContent = e.name;
        const why = document.createElement("span");
        why.textContent = "can't open here";
        li.append(n, why);
      }
      ul.append(li);
    }
    if (!r.entries.length) { const li = document.createElement("li"); li.className = "none"; li.textContent = "No folders in here."; ul.append(li); }
    const ae = document.activeElement;                                             // don't take focus from a field being typed in
    if (focusList && (!ae || ae === document.body || ae === $("fp-where") || ul.contains(ae) || ae.id === "fp-up")) (ul.querySelector("button") || $("fp-where")).focus();          // the button that was pressed is gone: keep the keyboard in the dialog
  } catch (e) {
    if (mine !== fpSeq) return;
    $("fp-note").textContent = e.name === "AbortError" ? "That folder is taking too long to answer. Try another." : e.message;
  }
}

function openFolderPicker() {
  $("fp-path").value = "";
  $("fp-note").textContent = "";
  $("fp-list").replaceChildren();                                                // nothing stale from the last time
  state.fp = { path: "", parent: "" };
  $("fp-where").textContent = "Looking…";
  $("fp-use").disabled = true;
  $("fp-up").disabled = true;
  $("folder-picker").showModal();
  fpLoad("");
}

async function addFromPicker(path) {
  if (!path) return;
  if (await changeFolders(path, "add", $("fp-note"))) {
    $("folder-picker").close();
    folderNote(`Added ${path}.`);
    ($("folders").hidden ? $("empty-add") : $("add-folder")).focus();
    $("announce").textContent = `Added music folder ${path}`;
  }
}

let removing = null;
function openRemove(item) {
  removing = item;
  $("fr-what").textContent = item.path;
  $("fr-note").textContent = "";
  $("folder-remove").showModal();
}

$("add-folder").addEventListener("click", openFolderPicker);
$("empty-add").addEventListener("click", openFolderPicker);
$("fp-up").addEventListener("click", () => fpLoad(state.fp.parent || ""));
$("fp-use").addEventListener("click", () => addFromPicker(state.fp.path));
$("fp-add-path").addEventListener("click", () => addFromPicker($("fp-path").value.trim()));
$("fp-path").addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); addFromPicker($("fp-path").value.trim()); } });
$("fr-confirm").addEventListener("click", async () => {
  const gone = removing;
  if (!gone) return;
  if (await changeFolders(gone.path, "remove", $("fr-note"))) {
    $("folder-remove").close();
    folderNote(`Removed ${gone.path}.`);
    $("announce").textContent = `Removed music folder ${gone.path}`;
    removing = null;
    // the Remove button went with its row; the last folder going hides the whole section, so pick by what is showing
    ($("folders").hidden ? $("empty-add") : $("add-folder")).focus();
  }
});

function renderListing() {
  renderFolders();
  renderCrumbs();
  const ul = $("listing");
  ul.replaceChildren();
  const q = state.filter.trim().toLocaleLowerCase();
  const shown = state.entries.filter((e) => !q || e.name.toLocaleLowerCase().includes(q));
  for (const e of shown) {
    const li = document.createElement("li");
    li.dataset.path = e.path;
    const name = document.createElement("span");
    name.className = "name";
    if (e.type === "dir") {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = e.name;
      b.addEventListener("click", () => { state.filter = ""; $("filter").value = ""; loadFolder(e.path); });
      name.append(b);
    } else {
      name.textContent = e.name;
    }
    li.append(name);
    if (e.type === "file") {
      if (e.dataless) { li.classList.add("dataless"); name.title = "Online-only in Dropbox: not on this Mac yet"; }
      const size = document.createElement("span");
      size.className = "size";
      size.textContent = fmtSize(e.size);
      li.append(size);
      const acts = document.createElement("span");
      acts.className = "row-actions";
      if (!e.dataless) {
        acts.append(button("Play", () => play([e.path], "replace")), button("Add", () => play([e.path], "append"), true),
                    button("Playlist", () => openPicker([e.path], e.name), true));
      }
      li.append(acts);
    }
    ul.append(li);
  }
  const atFolder = state.path !== "" && !state.folderError;
  $("folder-actions").hidden = !atFolder;
  $("more").hidden = state.nextOffset === null;
  if (!state.folderError && q && !shown.length && state.entries.length) note(`Nothing in this folder matches "${state.filter}".`);
  else if (!state.folderError && !state.entries.length && state.path !== "") note("Nothing playable in this folder.");
  else if ($("listing-note").classList.contains("error") === false && !q) note("");
  markCurrent();
}

function button(label, onClick, quiet) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  if (quiet) b.className = "quiet";
  b.addEventListener("click", onClick);
  return b;
}

function markCurrent() {
  let uri = (state.now && state.now.track && state.now.track.uri) || "";
  try { uri = decodeURIComponent(uri); } catch { uri = ""; }        // a malformed escape in someone else's URL
  uri = uri.normalize("NFC");
  const root = rootOf(state.path);
  for (const li of $("listing").children) {
    let hit = false;
    if (li.dataset.path && root && uri && state.now.state === "PLAYING") {
      const rel = li.dataset.path.slice(root.length).normalize("NFC");     // "/GLDR/01 Halyard.wav"
      hit = new RegExp("/media/[^/]+" + rel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "(\\.flac)?$").test(uri);      // a converted file's URL adds .flac
    }
    li.classList.toggle("current", hit);
  }
}

let playing = false;
async function play(paths, mode) {
  if (!state.roomUid || !state.rooms.some((r) => r.uid === state.roomUid)) { note("Pick a room first.", true); return; }
  if (playing) return;                                      // a second click while the first is still working
  playing = true;
  document.body.classList.add("busy");
  const room = state.rooms.find((r) => r.uid === state.roomUid);
  try {
    const r = await api("/api/play", { room: state.roomUid, paths, mode });
    const where = room ? room.name : "the room";
    note(`${r.started ? "Playing" : "Added"} ${r.total} track${r.total === 1 ? "" : "s"} on ${where}` +
         (r.truncated ? `; the first ${r.truncated.limit} of ${r.truncated.found}` : "") +
         (r.skipped_total ? `; skipped ${r.skipped_total}` : "") + ".");
  } catch (e) { note(e.message, true); }
  finally { playing = false; document.body.classList.remove("busy"); }
  pollState();
  pollQueue();
}

// ---- playlists ----------------------------------------------------------------------------------------

function plNote(el, text, isError) {
  el.textContent = text || "";
  el.classList.toggle("error", !!isError);
}
const plEnc = (id) => encodeURIComponent(id);
// One playlist change at a time: a double click must not remove, move or add twice (row indexes are only right until the first lands).
let plBusy = false;
async function plLock(fn, noteEl) {
  if (plBusy) { if (noteEl) plNote(noteEl, "Still working on the last change. Try again in a moment.", true); return; }
  plBusy = true;
  document.body.classList.add("busy");
  try { return await fn(); }
  finally { plBusy = false; document.body.classList.remove("busy"); }
}
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function showView(view) {
  state.view = view;
  const panels = { music: "music-panel", playlists: "playlists-panel", web: "web-panel", rooms: "rooms-panel" };
  for (const [v, panel] of Object.entries(panels)) {
    $(`view-${v}`).setAttribute("aria-pressed", String(view === v));
    $(panel).hidden = view !== v;
  }
  if (view === "playlists") loadPlaylists();
  if (view === "rooms") { pollRooms().then(() => loadBalances(true)); renderRoomsPanel(true); }
}

async function loadPlaylists() {
  try { state.playlists = await api("/api/playlists"); plNote($("pl-index-note"), ""); }
  catch (e) { renderPlaylists(); plNote($("pl-index-note"), e.message, true); return; }
  renderPlaylists();
}

function renderPlaylists() {
  $("pl-index").hidden = state.plOpen !== null;
  $("pl-editor").hidden = state.plOpen === null;
  const ul = $("pl-list");
  ul.replaceChildren();
  for (const p of state.playlists) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "name";
    const open = document.createElement("button");
    open.type = "button";
    open.className = "open";
    open.textContent = p.name;
    open.addEventListener("click", () => openPlaylist(p.id));
    name.append(open);
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = plural(p.count, "track");
    li.append(name, count);
    ul.append(li);
  }
  if (!state.playlists.length) plNote($("pl-index-note"), "No playlists yet. Name one above, or use Playlist on a file in Music.");
  else if ($("pl-index-note").textContent.startsWith("No playlists yet")) plNote($("pl-index-note"), "");
}

async function openPlaylist(id) {
  state.plOpen = id;
  state.plConfirmDelete = false;
  $("pl-rename-form").hidden = true;
  plNote($("pl-note"), "");
  state.plFocus = "title";
  await refreshPlaylist();
}

async function refreshPlaylist() {
  const id = state.plOpen;
  if (id === null) return;
  let detail;
  try { detail = await api(`/api/playlists/${plEnc(id)}`); }
  catch (e) {
    if (state.plOpen !== id) return;
    if (e.status === 404) { state.plOpen = null; state.plDetail = null; await loadPlaylists(); plNote($("pl-index-note"), "That playlist no longer exists.", true); return; }
    plNote($("pl-note"), e.message, true);
    return;
  }
  if (state.plOpen !== id) return;                         // another playlist was opened or the list shown meanwhile
  state.plDetail = detail;
  renderPlaylists();
  renderPlaylistEditor();
}

function renderPlaylistEditor() {
  const d = state.plDetail;
  if (!d) return;
  $("pl-title").textContent = d.name;
  $("pl-delete").textContent = state.plConfirmDelete ? "Delete for good" : "Delete";
  $("pl-delete").classList.toggle("danger", state.plConfirmDelete);
  updatePlayLabel();
  const ol = $("pl-tracks");
  ol.replaceChildren();
  d.tracks.forEach((t, i) => {
    const li = document.createElement("li");
    if (!t.available) li.classList.add("unavailable");
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = t.title;
    if (!t.available) {
      const why = document.createElement("span");
      why.className = "why";
      why.textContent = ` — can't play: ${t.reason}`;
      name.append(why);
    }
    const acts = document.createElement("span");
    acts.className = "row-actions";
    const up = button("↑", () => moveTrack(i, i - 1), true), down = button("↓", () => moveTrack(i, i + 1), true), rm = button("Remove", () => removeTrack(i), true);
    up.setAttribute("aria-label", `Move ${t.title} up`); down.setAttribute("aria-label", `Move ${t.title} down`); rm.setAttribute("aria-label", `Remove ${t.title}`);
    up.disabled = i === 0; down.disabled = i === d.tracks.length - 1;
    up.dataset.key = `up${i}`; down.dataset.key = `down${i}`; rm.dataset.key = `rm${i}`;
    acts.append(up, down, rm);
    li.append(name, acts);
    ol.append(li);
  });
  if (state.plFocus) {                                       // put keyboard focus where the user's action leaves it
    const want = state.plFocus.split("|");
    const target = want[0] === "title" ? $("pl-title") : want.map((k) => ol.querySelector(`[data-key="${k}"]`)).find((el) => el && !el.disabled);
    (target || $("pl-back")).focus({ preventScroll: true });
    state.plFocus = null;
  }
  if (!d.tracks.length) plNote($("pl-note"), "Empty. In Music, use Playlist on a file or Add folder to playlist.");
  else if ($("pl-note").textContent.startsWith("Empty.")) plNote($("pl-note"), "");
}

function updatePlayLabel() {
  const room = state.rooms.find((r) => r.uid === state.roomUid);
  const label = room ? `Play on ${room.name}` : "Play on this room";
  $("pl-play").textContent = label;
  $("send-play").textContent = label;
}

async function plAction(action, body, ok) {
  try {
    const r = await api(`/api/playlists/${plEnc(state.plOpen)}/${action}`, body);
    if (ok) plNote($("pl-note"), ok(r));
    return r;
  } catch (e) { plNote($("pl-note"), e.message, true); return null; }
}

function moveTrack(from, to) {
  return plLock(async () => {
    if (!(await plAction("move", { from, to }))) return;
    state.plConfirmDelete = false;
    state.plFocus = from > to ? `up${to}|down${to}` : `down${to}|up${to}`;      // focus follows the track that moved
    await refreshPlaylist();
  }, $("pl-note"));
}
function removeTrack(index) {
  return plLock(async () => {
    if (!(await plAction("remove", { index }))) return;
    state.plConfirmDelete = false;
    state.plFocus = `rm${index}|rm${index - 1}`;
    await refreshPlaylist();
  }, $("pl-note"));
}

function playPlaylist(mode) {
  return plLock(async () => {
    if (!state.roomUid || !state.rooms.some((r) => r.uid === state.roomUid)) { plNote($("pl-note"), "Pick a room first.", true); return; }
    const room = state.rooms.find((r) => r.uid === state.roomUid);
    const r = await plAction("play", { room: state.roomUid, mode }, (res) =>
      `${res.started ? "Playing" : "Added"} ${plural(res.total, "track")} on ${room.name}` +
      (res.unavailable && res.unavailable.length ? `; ${res.unavailable.length} can't be played (marked below)` : "") + ".");
    if (r) { pollState(); pollQueue(); await refreshPlaylist(); }      // refresh: a file gone since the list was drawn is marked now
  }, $("pl-note"));
}

// Adding files or a folder to a playlist: a small dialog lists the playlists, or starts a new one.
let pickPaths = [];
async function openPicker(paths, label) {
  pickPaths = paths;
  $("picker-what").textContent = `Adding ${label}.`;
  plNote($("picker-note"), "");
  $("picker-new").value = "";
  try { state.playlists = await api("/api/playlists"); } catch (e) { note(e.message, true); return; }
  const ul = $("picker-list");
  ul.replaceChildren();
  for (const p of state.playlists) {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    const n = document.createElement("span"), c = document.createElement("span");
    n.textContent = p.name; c.textContent = plural(p.count, "track"); c.className = "count";
    b.append(n, c);
    b.addEventListener("click", () => addToPlaylist(p.id));
    li.append(b);
    ul.append(li);
  }
  if (!state.playlists.length) { const li = document.createElement("li"); li.className = "none"; li.textContent = "No playlists yet."; ul.append(li); }
  $("picker").showModal();
}

async function addPicked(id) {                            // call only while holding plLock
  try {
    const r = await api(`/api/playlists/${plEnc(id)}/add`, { paths: pickPaths });
    $("picker").close();
    note(`Added ${plural(r.added, "track")} to ${id}` +
         (r.truncated ? `; the first ${r.truncated.limit} of ${r.truncated.found}` : "") +
         (r.skipped && r.skipped.length ? `; skipped ${r.skipped.length}` : "") + ".");
    if (state.plOpen === id) refreshPlaylist();
  } catch (e) { plNote($("picker-note"), e.message, true); }
}
function addToPlaylist(id) { return plLock(() => addPicked(id), $("picker-note")); }

async function createPlaylist(name) {
  const r = await api("/api/playlists", { name });
  return r.id;
}

$("view-music").addEventListener("click", () => showView("music"));
$("view-playlists").addEventListener("click", () => { state.plOpen = null; showView("playlists"); });
$("view-rooms").addEventListener("click", () => showView("rooms"));
$("view-web").addEventListener("click", () => { showView("web"); $("send-url").focus({ preventScroll: true }); });

// ---- managing rooms ---------------------------------------------------------------------------------

const visibleMembers = (g) => g.members.filter((m) => m.visible);
let renamingUid = null;
const renameDraft = {};
let roomBusy = false;

async function roomAction(path, body, okText, focusKey) {
  if (roomBusy) { plNote($("rooms-note"), "Still working on the last change. Try again in a moment.", true); return false; }
  roomBusy = true;
  document.body.classList.add("busy");
  try {
    await api(path, body);
    plNote($("rooms-note"), okText);
    return true;
  } catch (e) {
    plNote($("rooms-note"), e.message, true);
    return false;
  } finally {
    roomBusy = false;
    document.body.classList.remove("busy");
    await pollRooms();                                     // the service refreshed its cache; show it now
    setTimeout(() => { pollRooms().then(() => { if (state.view === "rooms") renderRoomsPanel(false); }); }, 1500);      // and once more: a speaker may be slow to report
    if (focusKey) state.roomsFocus = focusKey;
    renderRoomsPanel(true);
  }
}

function renderRoomsPanel(force) {
  const panel = $("rooms-panel");
  const active = document.activeElement;
  if (!force && active && panel.contains(active) && (active.tagName === "INPUT" || active.tagName === "SELECT")) return;     // never pull a field out from under the user
  const hadFocus = panel.contains(document.activeElement) ? document.activeElement.dataset.key : null;
  const ul = $("rooms-manage");
  ul.replaceChildren();
  const groups = [], seenGroups = new Set();                // /api/rooms lists a grouped room once per member: show each group once
  for (const r of state.rooms) {
    if (seenGroups.has(r.coordinator_uid)) continue;
    seenGroups.add(r.coordinator_uid);
    groups.push(r);
  }
  const online = groups.filter((r) => r.online);
  const selected = state.rooms.find((r) => r.uid === state.roomUid);
  $("group-all").textContent = selected ? `Group all rooms with ${selected.name}` : "Group all rooms";
  $("group-all").disabled = !selected || state.rooms.length < 2;
  const notice = $("rooms-note");
  if (!state.rooms.length) plNote(notice, state.unreachable ? "Rooms unknown while the service is unreachable." : "No rooms found yet. Searching the network…");
  else if (notice.dataset.empty === "1") plNote(notice, "");                 // the "no rooms" message goes once rooms arrive
  notice.dataset.empty = state.rooms.length ? "" : "1";
  for (const g of groups) {
    const members = visibleMembers(g);
    const li = document.createElement("li");
    const title = document.createElement("p");
    title.className = "group-name";
    title.textContent = members.map((m) => m.name).join(" + ") || g.name;
    if (members.length > 1) {
      const count = document.createElement("span");
      count.className = "count";
      count.textContent = `${members.length} rooms grouped`;
      title.append(count);
    }
    if (!g.online) { const off = document.createElement("span"); off.className = "count"; off.textContent = "offline"; title.append(off); }
    li.append(title);
    for (const m of members) {
      const row = document.createElement("div");
      row.className = "member";
      if (renamingUid === m.uid) {
        const form = document.createElement("form");
        const input = document.createElement("input");
        input.type = "text"; input.value = renameDraft[m.uid] ?? m.name; input.maxLength = 60; input.autocomplete = "off";
        input.addEventListener("input", () => { renameDraft[m.uid] = input.value; });      // a re-render keeps what was typed
        input.setAttribute("aria-label", `New name for ${m.name}`);
        input.dataset.key = `name-${m.uid}`;
        const save = document.createElement("button"); save.type = "submit"; save.textContent = "Save name";
        const done = () => { renamingUid = null; delete renameDraft[m.uid]; state.roomsFocus = `rename-${m.uid}`; renderRoomsPanel(true); };
        const cancel = button("Cancel", done, true);
        form.append(input, save, cancel);
        form.addEventListener("submit", async (ev) => {
          ev.preventDefault();
          const name = input.value.trim();
          if (!name) { plNote($("rooms-note"), "Give the room a name first.", true); return; }
          if (name === m.name) { done(); return; }
          if (await roomAction(`/api/rooms/${encodeURIComponent(m.uid)}/rename`, { name }, `Renamed ${m.name} to ${name}.`)) done();
        });
        input.addEventListener("keydown", (ev) => { if (ev.key === "Escape") done(); });
        row.append(form);
      } else {
        const name = document.createElement("span");
        name.className = "name";
        name.textContent = m.name;
        const acts = document.createElement("span");
        acts.className = "row-actions";
        const rename = button("Rename", () => { renamingUid = m.uid; state.roomsFocus = `name-${m.uid}`; renderRoomsPanel(true); }, true);
        rename.dataset.key = `rename-${m.uid}`;
        rename.setAttribute("aria-label", `Rename ${m.name}`);
        acts.append(rename);
        if (members.length > 1) {
          const leave = button("Leave group", () => roomAction(`/api/rooms/${encodeURIComponent(m.uid)}/ungroup`, {}, `${m.name} left the group.`, `rename-${m.uid}`), true);
          leave.setAttribute("aria-label", `${m.name} leaves its group`);
          acts.append(leave);
        } else {
          const others = online.filter((o) => o.coordinator_uid !== g.coordinator_uid);
          if (others.length) {
            const pick = document.createElement("select");
            pick.setAttribute("aria-label", `Group ${m.name} with`);
            pick.dataset.key = `pick-${m.uid}`;
            const first = document.createElement("option");
            first.value = ""; first.textContent = "Group with…";
            pick.append(first);
            for (const o of others) {
              const opt = document.createElement("option");
              opt.value = o.uid; opt.textContent = visibleMembers(o).map((x) => x.name).join(" + ") || o.name;
              pick.append(opt);
            }
            const join = button("Group", () => {
              if (!pick.value) { plNote($("rooms-note"), `Pick a room to group ${m.name} with.`, true); pick.focus(); return; }
              const target = others.find((o) => o.uid === pick.value);
              roomAction(`/api/rooms/${encodeURIComponent(m.uid)}/group`, { into: pick.value }, `${m.name} joined ${visibleMembers(target).map((x) => x.name).join(" + ") || target.name}.`, `rename-${m.uid}`);
            }, true);
            join.setAttribute("aria-label", `Group ${m.name} with the chosen room`);
            const wrap = document.createElement("span");                         // a <select> has no ::after: the chevron lives on this wrapper
            wrap.className = "key-select";
            wrap.append(pick);
            acts.append(wrap, join);
          }
        }
        row.append(name, acts);
        const rec = state.rooms.find((r) => r.uid === m.uid);
        if (rec && rec.balance_supported) row.append(buildBalance(m));
      }
      li.append(row);
    }
    ul.append(li);
  }
  const want = state.roomsFocus || hadFocus;
  state.roomsFocus = null;
  if (want) { const el = ul.querySelector(`[data-key="${want}"]`); if (el) el.focus({ preventScroll: true }); }
}

// ---- balance (docs/quick/07): -100 (all left) to 100 (all right) on each room that has it; the room's own, not its group's ------------

const balanceSend = {};                                     // uid -> { want, inflight, last }: one request at a time, at most one per 250 ms
const balanceLoading = new Set();

function balanceText(v) {
  if (v === null || v === undefined) return "…";
  return v === 0 ? "Centre" : v > 0 ? `Right ${v}` : `Left ${-v}`;
}

async function readBalance(uid) {
  if (balanceLoading.has(uid)) return;
  balanceLoading.add(uid);
  try { state.balance[uid] = (await api(`/api/rooms/${encodeURIComponent(uid)}/balance`, undefined, { slow: true })).balance; }
  catch { /* stays unknown: the slider waits */ }
  finally { balanceLoading.delete(uid); }
  const job = balanceSend[uid];
  if (!job || (job.want === null && !job.inflight)) showBalance(uid);       // never overwrite a value the user is setting
}

async function loadBalances(all) {
  // every room that has balance and whose value isn't known yet (or all of them, when the Rooms view opens: the CLI or
  // the Sonos app may have changed one)
  const uids = state.rooms.filter((r) => r.balance_supported && (all || state.balance[r.uid] === undefined || state.balance[r.uid] === null)).map((r) => r.uid);
  await Promise.all(uids.map(readBalance));
}

async function pumpBalance(uid) {
  const job = balanceSend[uid];
  if (job.inflight || job.want === null) return;
  const wait = 250 - (performance.now() - job.last);
  if (wait > 0) { if (!job.timer) job.timer = setTimeout(() => { job.timer = 0; pumpBalance(uid); }, wait); return; }
  const value = job.want;
  job.want = null;
  job.inflight = true;
  job.last = performance.now();
  try {
    const r = await api(`/api/rooms/${encodeURIComponent(uid)}/balance`, { balance: value });
    if (job.want === null) state.balance[uid] = r.balance;
  } catch (e) {
    plNote($("rooms-note"), e.message, true);
    if (job.want === null) state.balance[uid] = undefined;                  // refused: read what the speaker really has
  } finally {
    job.inflight = false;
  }
  if (job.want !== null) pumpBalance(uid);                                  // the user moved it meanwhile: send the latest
  else if (state.balance[uid] === undefined) readBalance(uid);
  else showBalance(uid);
}

function sendBalance(uid, value) {
  const job = (balanceSend[uid] ||= { want: null, inflight: false, last: 0, timer: 0 });
  job.want = value;
  pumpBalance(uid);
}

function showBalance(uid) {                                  // the slider and its words, in place (a re-render would skip a focused slider)
  const range = document.getElementById(`balance-${uid}`);
  if (!range) return;
  const v = state.balance[uid];
  const known = v !== null && v !== undefined;
  range.disabled = !known;                                   // until the speaker's value is known, a key press would overwrite it
  if (known) range.value = String(v);
  range.setAttribute("aria-valuetext", balanceText(v));
  const out = range.parentElement.querySelector("output");
  if (out) out.textContent = balanceText(v);
}

function buildBalance(m) {
  const wrap = document.createElement("div");
  wrap.className = "balance";
  const label = document.createElement("label");
  label.textContent = "Balance";
  const value = state.balance[m.uid];
  const range = document.createElement("input");
  range.type = "range"; range.min = "-100"; range.max = "100"; range.step = "1";
  range.value = String(value ?? 0);
  range.disabled = value === null || value === undefined;
  range.id = `balance-${m.uid}`;
  range.dataset.key = `balance-${m.uid}`;
  range.setAttribute("aria-label", `Balance for ${m.name}, left to right`);
  label.htmlFor = range.id;
  const out = document.createElement("output");
  out.htmlFor = range.id;
  const show = (v) => { out.textContent = balanceText(v); range.setAttribute("aria-valuetext", balanceText(v)); };
  show(value);
  const left = document.createElement("span"); left.className = "side"; left.textContent = "L"; left.setAttribute("aria-hidden", "true");
  const right = document.createElement("span"); right.className = "side"; right.textContent = "R"; right.setAttribute("aria-hidden", "true");
  range.addEventListener("input", () => { const v = Number(range.value); state.balance[m.uid] = v; show(v); sendBalance(m.uid, v); });
  const centre = button("Centre", () => { range.value = "0"; state.balance[m.uid] = 0; show(0); sendBalance(m.uid, 0); }, true);
  centre.setAttribute("aria-label", `Centre the balance for ${m.name}`);
  centre.dataset.key = `centre-${m.uid}`;
  wrap.append(label, left, range, right, out, centre);
  return wrap;
}

$("group-all").addEventListener("click", () => {
  const selected = state.rooms.find((r) => r.uid === state.roomUid);
  if (!selected) { plNote($("rooms-note"), "Pick a room first.", true); return; }
  roomAction("/api/rooms/group-all", { into: selected.uid }, `Every room is now grouped with ${selected.name}.`);
});

// ---- sending a web page's audio ---------------------------------------------------------------------

let sending = false;
async function sendPage(mode) {
  const input = $("send-url"), note = $("send-note");
  let url = input.value.trim();
  if (!url) { plNote(note, "Paste a page address first.", true); input.focus(); return; }
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(url)) { url = "https://" + url; input.value = url; }      // a bare address (even host:8080/x) is the likeliest paste
  if (!state.roomUid || !state.rooms.some((r) => r.uid === state.roomUid)) { plNote(note, "Pick a room first.", true); return; }
  if (sending) { plNote(note, "Still fetching the last page. Try again in a moment.", true); return; }
  sending = true;
  document.body.classList.add("busy");
  plNote(note, "Fetching the page…");
  const room = state.rooms.find((r) => r.uid === state.roomUid);
  try {
    const r = await api("/api/send", { room: state.roomUid, url, mode });
    plNote(note, `${r.started ? "Playing" : "Added"} ${plural(r.total, "track")}${r.title ? ` from ${r.title}` : ""} on ${room.name}.`);
    const ol = $("send-tracks");
    ol.replaceChildren();
    for (const t of r.tracks || []) {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = t.title;
      li.append(name);
      ol.append(li);
    }
    pollState(); pollQueue();
  } catch (e) {
    $("send-tracks").replaceChildren();
    plNote(note, e.message, true);
  } finally { sending = false; document.body.classList.remove("busy"); }
}
$("send-form").addEventListener("submit", (ev) => { ev.preventDefault(); sendPage("replace"); });
$("send-add").addEventListener("click", () => sendPage("append"));
$("pl-back").addEventListener("click", () => { state.plOpen = null; state.plDetail = null; loadPlaylists(); });
$("pl-new").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const name = $("pl-new-name").value.trim();
  if (!name) { plNote($("pl-index-note"), "Give the playlist a name first.", true); return; }
  await plLock(async () => {
    try { const id = await createPlaylist(name); $("pl-new-name").value = ""; await openPlaylist(id); }
    catch (e) { plNote($("pl-index-note"), e.message, true); }
  }, $("pl-index-note"));
});
$("pl-play").addEventListener("click", () => playPlaylist("replace"));
$("pl-queue").addEventListener("click", () => playPlaylist("append"));
$("pl-rename").addEventListener("click", () => {
  $("pl-rename-name").value = state.plDetail ? state.plDetail.name : "";
  $("pl-rename-form").hidden = false;
  $("pl-rename-name").focus();
});
$("pl-rename-cancel").addEventListener("click", () => { $("pl-rename-form").hidden = true; });
$("pl-rename-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const name = $("pl-rename-name").value.trim();
  if (!name) { plNote($("pl-note"), "Give the playlist a name first.", true); return; }
  await plLock(async () => {
    const r = await plAction("rename", { name });
    if (r) {
      state.plOpen = r.id; state.plConfirmDelete = false; state.plFocus = "title";
      $("pl-rename-form").hidden = true;
      await refreshPlaylist();
      plNote($("pl-note"), `Renamed to ${r.id}.`);
    }
  }, $("pl-note"));
});
$("pl-delete").addEventListener("click", async () => {
  if (!state.plConfirmDelete) { state.plConfirmDelete = true; state.plArmedAt = Date.now(); renderPlaylistEditor(); plNote($("pl-note"), "Press Delete for good to remove this playlist. The music files stay where they are."); return; }
  if (Date.now() - state.plArmedAt < 600) return;           // the second click of a double click must not confirm
  await plLock(async () => {
    const r = await plAction("delete", {});
    if (r) {
      state.plOpen = null; state.plDetail = null; state.plConfirmDelete = false;
      await loadPlaylists();
      plNote($("pl-index-note"), "Playlist deleted.");
      $("pl-new-name").focus({ preventScroll: true });
    }
  }, $("pl-note"));
});
$("folder-to-playlist").addEventListener("click", () => openPicker([state.path], "this folder"));
$("picker-new").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.isComposing) { ev.preventDefault(); $("picker-create").click(); }    // Enter creates; it must not fall through to Cancel
});
$("picker-create").addEventListener("click", async () => {
  const name = $("picker-new").value.trim();
  if (!name) { plNote($("picker-note"), "Give the playlist a name first.", true); return; }
  await plLock(async () => {                                // create and add are one step: nothing may slip in between
    let id;
    try { id = await createPlaylist(name); } catch (e) { plNote($("picker-note"), e.message, true); return; }
    await addPicked(id);
  }, $("picker-note"));
});

// ---- wiring -------------------------------------------------------------------------------------------

$("filter").addEventListener("input", async (ev) => { state.filter = ev.target.value; renderListing(); await loadAllPages(); });
$("more").addEventListener("click", () => loadFolder(state.path, state.entries.length));
$("play-folder").addEventListener("click", () => play([state.path], "replace"));
$("append-folder").addEventListener("click", () => play([state.path], "append"));
$("clear-queue").addEventListener("click", async () => {
  try { await api(`/api/rooms/${encodeURIComponent(state.roomUid)}/queue/clear`, {}); } catch (e) { note(e.message, true); }
  pollState(); pollQueue();
});
$("toggle").addEventListener("click", () => transport(state.now && state.now.state === "PLAYING" ? "pause" : "resume"));
$("prev").addEventListener("click", () => transport("prev"));
$("next").addEventListener("click", () => transport("next"));
$("volume").addEventListener("input", (ev) => { state.draggingVolume = true; setVolume(ev.target.value); });
$("volume").addEventListener("change", () => { setTimeout(() => { state.draggingVolume = false; }, 600); });

document.addEventListener("visibilitychange", () => { if (!document.hidden) pollState(); });      // a tab back from the background: don't draw a track that ended meanwhile

(async function start() {
  await pollRooms();
  await loadFolders();
  await loadFolder("");
  await pollState();
  pollQueue();
  frame();
  setInterval(pollState, 2000);
  setInterval(pollQueue, 5000);
  setInterval(pollRooms, 5000);
})();
