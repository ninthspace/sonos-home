"use strict";
// The popup: pick a room, send the page's audio, or stream the tab. Talks to the service on this Mac only.

const SERVICE = "http://localhost:8737";
const PREVIEW_WAIT_MS = 2000;                     // a preview slower than this is not waited for: Stream tab appears without it
const MAX_PAGE_BYTES = 4_000_000;                 // UTF-8 bytes: under the service's 5 MB page limit, and under its 12 MB body limit once JSON-escaped
const $ = (id) => document.getElementById(id);

function say(text, isError) {
  const n = $("note");
  n.textContent = text || "";
  n.classList.toggle("error", !!isError);
}

function show(...ids) {
  for (const id of ["checking", "send", "stream", "stop"]) $(id).hidden = !ids.includes(id);
}

async function call(path, body, ms = 15000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try {
    const res = await fetch(SERVICE + path, body === undefined ? { signal: ctl.signal } : {
      method: "POST", signal: ctl.signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const text = await res.text();
    let data = null;
    try { data = JSON.parse(text); } catch { /* not JSON (the service refuses a stranger with plain text) */ }
    if (!res.ok) {
      const refused = res.status === 403 ? `The service doesn't allow this extension. Add ${chrome.runtime.id} to extension_ids in config.toml, then restart the service.` : null;
      throw Object.assign(new Error(refused || (data && data.error) || text.slice(0, 200) || `HTTP ${res.status}`), { status: res.status });
    }
    return data;
  } finally {
    clearTimeout(timer);
  }
}

const ENDED = {
  "stop": "You stopped the stream",
  "tab-closed": "The tab was closed",
  "room-switched": "The room switched to another source",
  "replaced": "Another stream took the room over",
  "service-error": "The service had a problem",
};

function endedText(last) {
  const why = ENDED[last.reason] || "The stream ended";
  return `${why} (${last.reason})${last.detail ? `: ${last.detail}` : ""}.`;
}

function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

// ---- rooms --------------------------------------------------------------------------------------------------

async function loadRooms() {
  const rooms = await call("/api/rooms", undefined, 4000);
  const seen = new Set();
  const unique = rooms.filter((r) => r.online && !seen.has(r.uid) && seen.add(r.uid));
  const select = $("room");
  select.replaceChildren();
  for (const r of unique) {
    const opt = document.createElement("option");
    opt.value = r.uid;
    const more = r.members.filter((m) => m.visible).length - 1;
    opt.textContent = r.name + (more > 0 ? ` +${more}` : "");
    select.append(opt);
  }
  const { room } = await chrome.storage.local.get("room");          // remembered by UID, so a rename doesn't lose it
  if (room && unique.some((r) => r.uid === room)) select.value = room;
  select.disabled = unique.length === 0;
  if (!unique.length) { const opt = document.createElement("option"); opt.textContent = "No rooms found"; select.append(opt); }
  select.addEventListener("change", () => chrome.storage.local.set({ room: select.value }));
  return unique.length > 0;
}

// ---- the page ----------------------------------------------------------------------------------------------------

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function pageOf(tabId) {
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    injectImmediately: true,                                        // don't wait for the page to finish loading
    func: () => ({ html: document.documentElement.outerHTML.slice(0, 4_500_000), base: document.baseURI }),
  });
  return capBytes(res.result);
}

function capBytes(page) {
  const bytes = new TextEncoder().encode(page.html);
  if (bytes.length <= MAX_PAGE_BYTES) return page;
  return { ...page, html: new TextDecoder().decode(bytes.slice(0, MAX_PAGE_BYTES)) };       // a cut character at the end is replaced, not an error
}

function withStop() {                                   // decided when it is needed: Stop may have been pressed while the page was checked
  return $("stop").hidden ? [] : ["stop"];
}

async function checkPage(tab) {
  show("checking", ...withStop());
  let page = null;
  try { page = await pageOf(tab.id); } catch { /* a page the extension can't read (chrome://, the web store) */ }
  if (!page) { show("stream", ...withStop()); return null; }
  const found = await Promise.race([
    call("/api/send/preview", { html: page.html, base_url: page.base }, 10000)
      .then((r) => (r && r.tracks ? r.tracks.length : 0))
      .catch((e) => { if (e.status === 403) say(e.message, true); return 0; }),
    new Promise((resolve) => setTimeout(() => resolve(-1), PREVIEW_WAIT_MS)),
  ]);
  if (found > 0) {
    show("send", "stream", ...withStop());
    $("found").textContent = `${plural(found, "track")} on this page.`;
  } else {
    show("stream", ...withStop());
  }
  return page;
}

// ---- go ----------------------------------------------------------------------------------------------------------

async function start() {
  let hasRooms = false;
  try {
    hasRooms = await loadRooms();
  } catch {
    say("The service isn't answering. Start it on this Mac with sonos service run.", true);
    return;
  }
  if (!hasRooms) { say("No rooms found yet. Try again in a few seconds.", true); return; }
  const tab = await activeTab();
  const status = (await chrome.runtime.sendMessage({ type: "status" }).catch(() => null)) || { streaming: false };
  let page = null;
  chrome.storage.local.remove("alsoPlay").catch(() => {});        // the old "Also play here" setting, gone with the option
  $("stop").hidden = !status.streaming;                              // Stop is there from the start while a stream runs
  if (status.streaming) say((status.starting ? "Starting a stream to " : "Streaming a tab to ") + (roomName(status.room) || "a room") + ".");
  else if (status.last) say(endedText(status.last), status.last.reason === "service-error");

  $("send").addEventListener("click", async () => {                // listeners first, so Stop works while the page is still being checked
    if (!page) return;
    $("send").disabled = true;
    say("Sending…");
    try {
      const r = await call("/api/send", { room: $("room").value, html: page.html, base_url: page.base, mode: "replace" }, 120000);
      const where = $("room").selectedOptions[0].textContent;
      say(`${r.started ? "Playing" : "Added"} ${plural(r.total, "track")}${r.title ? ` from ${r.title}` : ""} on ${where}.`);
    } catch (e) {
      say(e.message, true);
    } finally {
      $("send").disabled = false;
    }
  });
  $("stream").addEventListener("click", async () => {
    if (!tab || $("stream").disabled) return;
    $("stream").disabled = true;
    say("Starting…");
    const r = await chrome.runtime.sendMessage({ type: "start", tabId: tab.id, room: $("room").value }).catch((e) => ({ error: e.message }));
    $("stream").disabled = false;
    if (r && r.error) say(r.reason ? endedText({ reason: r.reason }) : r.error, true);
    else { say("Streaming this tab to " + ($("room").selectedOptions[0] || {}).textContent + "."); show(...visibleNow(), "stop"); }
  });
  $("stop").addEventListener("click", async () => {
    const r = await chrome.runtime.sendMessage({ type: "stop" }).catch((e) => ({ error: e.message }));
    if (r && r.error) say(r.error, true); else { say(endedText({ reason: "stop" })); show(...visibleNow().filter((id) => id !== "stop")); }
  });

  if (tab) page = await checkPage(tab); else show("stream", ...withStop());
}

function roomName(uid) {
  const opt = [...$("room").options].find((o) => o.value === uid);
  return opt ? opt.textContent : "";
}

function visibleNow() {
  return ["checking", "send", "stream", "stop"].filter((id) => !$(id).hidden);
}

start();
