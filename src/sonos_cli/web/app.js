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
  view: "music", playlists: [], plOpen: null, plDetail: null, plConfirmDelete: false, plArmedAt: 0, plFocus: null,
};

// ---- talking to the service ---------------------------------------------------------------------------

async function api(path, body, opts = {}) {
  // Reads give up after 3.5 s (the service is unreachable). Writes can legitimately take long (a hi-res file is
  // converted first, an append waits for the previous queueing), so they get two minutes.
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), body === undefined ? 3500 : 120000);
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
  if (!ok) { state.now = null; renderPlayer(); }
  renderRooms();
}

// ---- rooms --------------------------------------------------------------------------------------------

async function pollRooms() {
  try {
    state.rooms = await api("/api/rooms");
    if (!state.rooms.some((r) => r.uid === state.roomUid) && state.rooms.length) {
      state.roomUid = state.rooms[0].uid;
      store.set("room", state.roomUid);
    }
    renderRooms();
  } catch { /* the banner says so */ }
}

const STRIP_COLOURS = ["#3d7bff", "#ff4fa3", "#2fe0a0", "#ff8a3d", "#a78bff", "#3ddcff"];

function renderRooms() {
  const ul = $("room-list");
  const focused = ul.contains(document.activeElement) && document.activeElement.dataset ? document.activeElement.dataset.uid : null;
  ul.replaceChildren();
  if (!state.rooms.length) {
    const li = document.createElement("li");
    li.textContent = state.unreachable ? "Rooms unknown while the service is unreachable." : "No rooms found yet. Searching the network…";
    li.className = "none";
    ul.append(li);
  }
  const order = state.rooms.map((r) => r.uid).sort();                 // a room keeps its colour when others are renamed
  for (const r of state.rooms) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "strip";
    btn.dataset.uid = r.uid;
    btn.style.setProperty("--cap", STRIP_COLOURS[order.indexOf(r.uid) % STRIP_COLOURS.length]);
    const vol = typeof r.volume === "number" ? r.volume : 0;
    btn.style.setProperty("--vol", String(vol));
    const cap = document.createElement("span");
    cap.className = "cap";
    const name = document.createElement("span");
    name.className = "rname";
    const members = r.members.filter((m) => m.visible).length;
    name.textContent = r.name;
    if (members > 1) {
      const plus = document.createElement("span");
      plus.className = "plus";
      plus.textContent = `+${members - 1}`;
      name.append(plus);
    }
    const st = document.createElement("span");
    st.className = "state";
    const led = document.createElement("span");
    led.className = "led";
    const word = document.createElement("span");
    word.textContent = state.unreachable ? "Unknown" : !r.online ? "Offline" : r.state === "PLAYING" ? "Playing" : r.state === "PAUSED_PLAYBACK" ? "Paused" : "Stopped";
    st.append(led, word);
    const chan = document.createElement("span");
    chan.className = "chan";
    chan.setAttribute("aria-hidden", "true");
    const meter = document.createElement("span");
    meter.className = "meter";
    const slot = document.createElement("span");
    slot.className = "slot";
    const knob = document.createElement("span");
    knob.className = "knob";
    slot.append(knob);
    chan.append(meter, slot);
    const level = document.createElement("span");
    level.className = "vol";
    level.textContent = typeof r.volume === "number" ? String(r.volume) : "–";
    btn.append(cap, name, st, chan, level);
    btn.setAttribute("aria-current", String(r.uid === state.roomUid));
    btn.setAttribute("aria-label", `${r.name}${members > 1 ? ` and ${members - 1} more` : ""}, ${word.textContent.toLowerCase()}${typeof r.volume === "number" ? `, volume ${r.volume}` : ""}`);
    if (r.state === "PLAYING" && r.online && !state.unreachable) li.classList.add("playing");
    if (r.state === "PAUSED_PLAYBACK" && r.online && !state.unreachable) li.classList.add("paused");
    if (!r.online) li.classList.add("offline");
    if (typeof r.volume !== "number") li.classList.add("novol");
    btn.addEventListener("click", () => { state.roomUid = r.uid; store.set("room", r.uid); state.queue = null; lastQueueCurrent = null; renderQueue(); renderRooms(); pollState(); pollQueue(); });
    li.append(btn);
    ul.append(li);
  }
  if (focused) { const again = ul.querySelector(`[data-uid="${CSS.escape(focused)}"]`); if (again) again.focus({ preventScroll: true }); }     // a re-render must not drop keyboard focus
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
  renderPlayer();
  markCurrent();
  const idx = now && now.track ? now.track.index : null;
  if (idx !== prevIndex) pollQueue();                      // the track changed: the highlighted row moves
}

function queueTitle(index) {
  // Only when the service says the room is playing that queue item (`current` is null for a radio or line-in source that merely
  // reports track 1), so a stale queue never names audio that isn't from it.
  const q = state.queue;
  if (!q || index === null || index === undefined || q.current !== index) return "";
  const item = q.items.find((it) => it.index === index);
  return item ? item.title : "";
}

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
    const shown = t.title || queueTitle(t.index);      // a file's own tags win on the speaker; a page's tracks may have none: use the queue's title
    $("np-title").textContent = shown || (n.state === "STOPPED" ? "Nothing playing" : "Playing");
    if (shown && shown !== lastAnnounced) { lastAnnounced = shown; $("announce").textContent = `Now playing ${shown}`; }
    const parts = [sourceLabel(n.source), n.state === "PLAYING" ? "playing" : n.state === "PAUSED_PLAYBACK" ? "paused" : "stopped"];
    if (shown) parts.push(`${clock(n.position)} / ${clock(n.duration)}`);
    $("np-meta").textContent = parts.join(", ");
  }
  const toggle = $("toggle");
  toggle.querySelector("path").setAttribute("d", live ? "M6 4h4.5v16H6zM13.5 4H18v16h-4.5z" : "M7 4.5v15l12-7.5z");
  toggle.setAttribute("aria-label", live ? "Pause" : "Play");
  const total = seconds(n && n.duration), at = seconds(n && n.position);
  const pct = total > 0 ? Math.min(100, Math.round((at / total) * 100)) : 0;
  $("progress-fill").style.width = `${pct}%`;
  $("progress").setAttribute("aria-valuenow", String(pct));
  if (n && !state.draggingVolume && typeof n.volume === "number") {
    $("volume").value = n.volume;
    $("volume-out").textContent = n.volume;
  } else if (!n) {
    $("volume-out").textContent = "–";
  }
}

async function transport(action) {
  if (!state.roomUid) return;
  try { await api(`/api/rooms/${encodeURIComponent(state.roomUid)}/transport`, { action }); }
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

function renderQueue() {
  const ol = $("queue-list");
  const had = ol.contains(document.activeElement) ? [...ol.querySelectorAll("button")].indexOf(document.activeElement) : -1;
  ol.replaceChildren();
  const q = state.queue;
  if (!q) { $("clear-queue").hidden = true; return; }
  $("queue-note").textContent = q.items.length ? "" : "The queue is empty. Play something to fill it.";
  $("clear-queue").hidden = !q.items.length;
  for (const it of q.items) {
    const li = document.createElement("li");
    li.dataset.current = String(it.index === q.current);
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("aria-current", String(it.index === q.current));
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
    b.addEventListener("click", async () => {
      try { await api(`/api/rooms/${encodeURIComponent(state.roomUid)}/queue/jump`, { index: it.index }); }
      catch (e) { note(e.message, true); }
      pollState(); pollQueue();
    });
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

function renderListing() {
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
      hit = new RegExp("/media/[^/]+" + rel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "$").test(uri);
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
  if (view === "rooms") { pollRooms(); renderRoomsPanel(true); }
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
            acts.append(pick, join);
          }
        }
        row.append(name, acts);
      }
      li.append(row);
    }
    ul.append(li);
  }
  const want = state.roomsFocus || hadFocus;
  state.roomsFocus = null;
  if (want) { const el = ul.querySelector(`[data-key="${want}"]`); if (el) el.focus({ preventScroll: true }); }
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

(async function start() {
  await pollRooms();
  await loadFolder("");
  await pollState();
  pollQueue();
  setInterval(pollState, 2000);
  setInterval(pollQueue, 5000);
  setInterval(pollRooms, 5000);
})();
