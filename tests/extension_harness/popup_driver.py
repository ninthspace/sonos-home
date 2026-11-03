"""Drives the real extension's popup in branded Chrome against the fake-rooms service (epic 01-07 Story 2). Not collected by pytest.

    uv run python tests/extension_harness/popup_driver.py

It starts `sonos service run --fake-rooms` on port 8737 (the extension's only host permission) with this extension's ID in
`extension_ids`, serves two test pages on 127.0.0.1:8798, launches Chrome with a throwaway profile (flags as in README.md),
loads a copy of `extension/` with Extensions.loadUnpacked (the copy's manifest also lists the test pages' origin, because
chrome.action.openPopup() does NOT grant activeTab and the toolbar can't be clicked from CDP: the real toolbar-click grant is checked
by the user in epic 01-08), opens the popup with chrome.action.openPopup(), and prints what the popup shows. Observations are printed as `OBS name: value` lines.
"""

import asyncio, json, os, shutil, signal, subprocess, sys, tempfile, time, urllib.request
import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
EXT_SRC = os.path.join(ROOT, "extension")
# A real toolbar click grants activeTab; Chrome's UI can't be clicked from CDP, and chrome.action.openPopup() doesn't grant it. So the
# harness loads a COPY of the extension whose manifest also lists the test pages' origin. Every script is the real one; the production
# manifest's permissions are checked separately (tests/test_extension.py).
EXT = tempfile.mkdtemp(prefix="sonos-ext-")
import shutil
shutil.copytree(EXT_SRC, EXT, dirs_exist_ok=True)
_m = json.load(open(os.path.join(EXT, "manifest.json")))
_m["host_permissions"] = _m["host_permissions"] + ["http://127.0.0.1:8798/*"]
json.dump(_m, open(os.path.join(EXT, "manifest.json"), "w"), indent=2)
EXTID = open(os.path.join(HERE, "extid.txt")).read().strip()
SERVICE = "http://localhost:8737"
PAGES = "http://127.0.0.1:8798"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

cfgdir = tempfile.mkdtemp(prefix="sonos-popup-cfg-")
open(os.path.join(cfgdir, "config.toml"), "w").write(f'extension_ids = ["{EXTID}"]\n')
svc_log = open(os.path.join(cfgdir, "service.log"), "w")
svc = subprocess.Popen(["uv", "run", "sonos", "service", "run", "--fake-rooms", "--port", "8737"], cwd=ROOT,
                       env=dict(os.environ, SONOS_CLI_CONFIG_DIR=cfgdir), stdout=svc_log, stderr=subprocess.STDOUT, start_new_session=True)
pages_dir = tempfile.mkdtemp(prefix="sonos-pages-")
shutil.copytree(os.path.join(HERE, "pages"), pages_dir, dirs_exist_ok=True)
with open(os.path.join(pages_dir, "big.html"), "w", encoding="utf-8") as _f:        # 2,000,000 CJK characters is about 6 MB of UTF-8, over the service's 5 MB page limit
    _f.write('<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>Big</title></head><body><a href="1/halyard.mp3">Halyard</a><p>' + "日" * 2_000_000 + "</p></body></html>")
shutil.copy(os.path.join(HERE, "page.html"), os.path.join(pages_dir, "tone.html"))
pages = subprocess.Popen([sys.executable, "-m", "http.server", "8798", "--bind", "127.0.0.1"], cwd=pages_dir,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
profile = tempfile.mkdtemp(prefix="chrome-popup-")
chrome = subprocess.Popen([CHROME, f"--user-data-dir={profile}", "--remote-debugging-port=9334", "--enable-unsafe-extension-debugging",
                           f"--allowlisted-extension-id={EXTID}", "--auto-accept-this-tab-capture", "--autoplay-policy=no-user-gesture-required",
                           "--no-first-run", "--no-default-browser-check", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def obs(name, value):
    print(f"OBS {name}: {value}", flush=True)


def service(path, body=None):
    headers = {"Content-Type": "application/json", "Origin": SERVICE}
    req = urllib.request.Request(SERVICE + path, json.dumps(body).encode() if body is not None else None, headers if body is not None else {})
    return json.load(urllib.request.urlopen(req, timeout=20))


class CDP:
    def __init__(self, ws):
        self.ws, self.id, self.pending, self.handlers = ws, 0, {}, []

    async def run(self):
        async for m in self.ws:
            d = json.loads(m)
            if "id" in d and d["id"] in self.pending:
                self.pending.pop(d["id"]).set_result(d)
            else:
                for h in self.handlers:
                    asyncio.ensure_future(h(d))

    async def send(self, method, params=None, sid=None):
        self.id += 1
        f = asyncio.get_event_loop().create_future()
        self.pending[self.id] = f
        msg = {"id": self.id, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        await self.ws.send(json.dumps(msg))
        return await f

    async def eval(self, expr, sid=None, gesture=False):
        r = await self.send("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True, "userGesture": gesture}, sid)
        res = r.get("result", {})
        if res.get("exceptionDetails"):
            return "EXC " + json.dumps(res["exceptionDetails"].get("exception", {}).get("description", ""))[:300]
        return res.get("result", {}).get("value")


async def wait_service():
    for _ in range(80):
        try:
            return service("/api/rooms")
        except Exception:
            await asyncio.sleep(0.5)
    sys.exit("the fake service did not start")


async def main():
    rooms = await wait_service()
    uid = {r["name"]: r["uid"] for r in rooms}
    v = None
    for _ in range(60):
        try:
            v = json.load(urllib.request.urlopen("http://127.0.0.1:9334/json/version"))
            break
        except Exception:
            await asyncio.sleep(0.3)
    if v is None:
        sys.exit("Chrome did not answer")
    obs("browser", v["Browser"])
    slow_next = {"on": False, "mode": "slow"}
    async with websockets.connect(v["webSocketDebuggerUrl"], max_size=None) as ws:
        c = CDP(ws)
        asyncio.create_task(c.run())

        async def on_event(d):
            m = d.get("method")
            p = d.get("params", {})
            if m == "Target.attachedToTarget" and p.get("waitingForDebugger"):
                sid = p["sessionId"]
                if slow_next["on"] and p["targetInfo"]["type"] in ("page", "other"):       # the popup's URL isn't set yet at this point
                    await c.send("Fetch.enable", {"patterns": [{"urlPattern": "*send/preview*"}]}, sid)
                await c.send("Runtime.runIfWaitingForDebugger", {}, sid)
            elif m == "Fetch.requestPaused":
                if slow_next["mode"] == "forbid":                        # what the service answers a stranger: a plain-text 403
                    import base64
                    body = base64.b64encode(b"Origin 'chrome-extension://x' is not allowed").decode()
                    await c.send("Fetch.fulfillRequest", {"requestId": p["requestId"], "responseCode": 403, "body": body,
                                                          "responseHeaders": [{"name": "content-type", "value": "text/plain; charset=utf-8"}]}, d.get("sessionId"))
                else:
                    await asyncio.sleep(3.0)                              # the stubbed slow preview
                    await c.send("Fetch.continueRequest", {"requestId": p["requestId"]}, d.get("sessionId"))
        c.handlers.append(on_event)
        await c.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})

        lr = await c.send("Extensions.loadUnpacked", {"path": EXT})
        ext_id = lr.get("result", {}).get("id")
        obs("loaded extension id", f"{ext_id} (expected {EXTID})")
        await asyncio.sleep(1.5)
        targets = (await c.send("Target.getTargets"))["result"]["targetInfos"]
        sw = next((t for t in targets if t["type"] == "service_worker" and EXTID in t["url"]), None)
        if not sw:
            obs("service worker", "NOT FOUND")
            return
        sw_sid = (await c.send("Target.attachToTarget", {"targetId": sw["targetId"], "flatten": True}))["result"]["sessionId"]
        obs("manifest permissions", await c.eval("JSON.stringify(chrome.runtime.getManifest().permissions)", sw_sid))
        obs("harness copy host permissions (production has only the first)", await c.eval("JSON.stringify(chrome.runtime.getManifest().host_permissions)", sw_sid))

        async def open_page(path):
            r = await c.send("Target.createTarget", {"url": f"{PAGES}/{path}"})
            tid = r["result"]["targetId"]
            await c.send("Target.activateTarget", {"targetId": tid})
            await asyncio.sleep(1.5)
            return tid

        async def open_popup():
            out = await c.eval("chrome.windows.getLastFocused().then(w => chrome.action.openPopup({windowId: w.id})).then(() => 'opened', e => 'ERR ' + e.message)", sw_sid, gesture=True)
            for _ in range(40):
                await asyncio.sleep(0.15)
                ts = (await c.send("Target.getTargets"))["result"]["targetInfos"]
                pop = next((t for t in ts if t["url"].startswith(f"chrome-extension://{EXTID}/popup.html") and t["type"] in ("page", "other")), None)
                if pop:
                    sid = (await c.send("Target.attachToTarget", {"targetId": pop["targetId"], "flatten": True}))["result"]["sessionId"]
                    return out, pop["targetId"], sid
            return out, None, None

        VISIBLE = "[...document.querySelectorAll('#actions button')].filter(b => !b.hidden).map(b => b.textContent)"

        async def visible(sid):
            return await c.eval(f"JSON.stringify({VISIBLE})", sid)

        async def close(tid):
            await c.send("Target.closeTarget", {"targetId": tid})
            await asyncio.sleep(0.5)

        # ---- A: a page with direct MP3 links ----------------------------------------------------------------------------
        page = await open_page("audio.html")
        out, pop, sid = await open_popup()
        obs("A open popup", out)
        if not pop:
            return
        await asyncio.sleep(1.5)
        obs("A buttons on a page with MP3 links", await visible(sid))
        obs("A diagnostic: active tab", await c.eval("chrome.tabs.query({active: true, currentWindow: true}).then(t => JSON.stringify(t.map(x => [x.id, x.url || '(no url)'])))", sid))
        obs("A diagnostic: executeScript", await c.eval("chrome.tabs.query({active: true, currentWindow: true}).then(([t]) => chrome.scripting.executeScript({target: {tabId: t.id}, func: () => document.title})).then(r => JSON.stringify(r), e => 'ERR ' + e.message)", sid))
        obs("A tracks line", await c.eval("document.getElementById('found').textContent", sid))
        obs("A rooms offered", await c.eval("JSON.stringify([...document.querySelectorAll('#room option')].map(o => o.textContent))", sid))
        await c.eval("document.getElementById('room').value = '" + uid["Kitchen"] + "'; document.getElementById('room').dispatchEvent(new Event('change')); 1", sid)
        await asyncio.sleep(0.5)
        await c.eval("[...document.querySelectorAll('#actions button')].find(b => b.textContent === 'Send page').click(); 1", sid)
        await asyncio.sleep(2.5)
        obs("A note after Send page", await c.eval("document.getElementById('note').textContent", sid))
        q = service(f"/api/rooms/{uid['Kitchen']}/queue")
        obs("A Kitchen queue", [i["title"] for i in q["items"]])
        st = service(f"/api/rooms/{uid['Kitchen']}/state")
        obs("A Kitchen state", st["state"])
        await close(pop)
        await c.send("Target.closeTarget", {"targetId": page})

        # ---- B: a page without audio ----------------------------------------------------------------------------------
        page = await open_page("plain.html")
        out, pop, sid = await open_popup()
        obs("B open popup", out)
        first = await visible(sid)
        obs("B buttons right after opening", first)
        await asyncio.sleep(2.5)
        obs("B buttons after the check", await visible(sid))
        await close(pop)

        # ---- C: a slow preview (held for 3 s by a stub): Checking… and then Stream tab, without waiting for it -------------
        await c.send("Target.closeTarget", {"targetId": page})
        for label, path in (("C1 page without audio", "plain.html"), ("C2 page with MP3 links", "audio.html")):
            page = await open_page(path)
            slow_next["on"] = True
            t0 = time.time()
            out, pop, sid = await open_popup()
            seen = None
            while time.time() - t0 < 5:
                now = await visible(sid)
                if now != seen:
                    obs(f"{label}: buttons at {time.time() - t0:.1f}s", now)
                    seen = now
                await asyncio.sleep(0.1)
            slow_next["on"] = False
            await close(pop)
            await c.send("Target.closeTarget", {"targetId": page})

        # ---- E: the service refuses this extension (the 403 is stubbed): the popup says what to do ---------------------------
        page = await open_page("audio.html")
        slow_next["on"], slow_next["mode"] = True, "forbid"
        out, pop, sid = await open_popup()
        await asyncio.sleep(2.5)
        obs("E buttons when the service refuses the extension", await visible(sid))
        obs("E note", await c.eval("document.getElementById('note').textContent", sid))
        slow_next["on"], slow_next["mode"] = False, "slow"
        await close(pop)
        await c.send("Target.closeTarget", {"targetId": page})

        # ---- F: a 6 MB (UTF-8) page: cut by bytes, so the preview still works ---------------------------------------------
        page = await open_page("big.html")
        out, pop, sid = await open_popup()
        await asyncio.sleep(4.0)
        obs("F buttons on a 6 MB page with an MP3 link at the top", await visible(sid))
        obs("F tracks line", await c.eval("document.getElementById('found').textContent", sid))
        await close(pop)
        await c.send("Target.closeTarget", {"targetId": page})

        # ---- G: the stream note and the tracks line don't overwrite each other; Stop pressed while the page is checked stays stopped ---
        tone = await open_page("tone.html")
        out, pop, sid = await open_popup()
        await c.eval("document.getElementById('room').value = '" + uid["Kitchen"] + "'; document.getElementById('room').dispatchEvent(new Event('change')); 1", sid)
        await c.eval("[...document.querySelectorAll('#actions button')].find(b => b.textContent === 'Stream tab').click(); 1", sid)
        await asyncio.sleep(3.0)
        await close(pop)
        await c.send("Target.closeTarget", {"targetId": tone})                       # the tab goes: the stream ends with tab-closed
        await asyncio.sleep(1.5)
        page = await open_page("audio.html")
        out, pop, sid = await open_popup()
        await asyncio.sleep(2.0)
        obs("G page with MP3 links after a tab-closed stream: buttons", await visible(sid))
        obs("G note", await c.eval("document.getElementById('note').textContent", sid))
        obs("G tracks line", await c.eval("document.getElementById('found').textContent", sid))
        await close(pop)
        await c.send("Target.closeTarget", {"targetId": page})
        tone = await open_page("tone.html")
        out, pop, sid = await open_popup()
        await c.eval("[...document.querySelectorAll('#actions button')].find(b => b.textContent === 'Stream tab').click(); 1", sid)
        await asyncio.sleep(3.0)
        await close(pop)
        page = await open_page("audio.html")                                         # a stream is running; its tab (tone) is in the background
        slow_next["on"], slow_next["mode"] = True, "slow"
        out, pop, sid = await open_popup()
        await asyncio.sleep(0.8)
        obs("G while checking with a stream running: buttons", await visible(sid))
        await c.eval("[...document.querySelectorAll('#actions button')].find(b => b.textContent === 'Stop').click(); 1", sid)
        await asyncio.sleep(3.5)
        obs("G after Stop pressed during the check: buttons", await visible(sid))
        obs("G note", await c.eval("document.getElementById('note').textContent", sid))
        slow_next["on"] = False
        await close(pop)
        await c.send("Target.closeTarget", {"targetId": page})
        await c.send("Target.closeTarget", {"targetId": tone})

        # ---- D: the chosen room is remembered across opens and across a rename ----------------------------------------
        page = await open_page("audio.html")
        out, pop, sid = await open_popup()
        await asyncio.sleep(1.0)
        await c.eval("document.getElementById('room').value = '" + uid["Lounge"] + "'; document.getElementById('room').dispatchEvent(new Event('change')); 1", sid)
        await asyncio.sleep(0.5)
        await close(pop)
        out, pop, sid = await open_popup()
        await asyncio.sleep(1.0)
        obs("D selected after reopening", await c.eval("document.getElementById('room').selectedOptions[0].textContent", sid))
        await close(pop)
        service(f"/api/rooms/{uid['Lounge']}/rename", {"name": "Den"})
        out, pop, sid = await open_popup()
        await asyncio.sleep(1.0)
        obs("D selected after renaming Lounge to Den", await c.eval("[document.getElementById('room').selectedOptions[0].textContent, document.getElementById('room').value === '" + uid["Lounge"] + "'].join(' / ')", sid))
        await close(pop)

try:
    asyncio.run(main())
finally:
    chrome.terminate()
    pages.terminate()
    try:
        os.killpg(svc.pid, signal.SIGTERM)                                  # uv and the Python it starts
    except ProcessLookupError:
        pass
    print("service log:", os.path.join(cfgdir, "service.log"))
