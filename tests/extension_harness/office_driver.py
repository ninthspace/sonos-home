"""Streams the 440 Hz test page to the REAL Office through the extension's popup (epic 01-07 Story 4, `[manual]`). Not collected by pytest.

    uv run python tests/extension_harness/office_driver.py

Office is muted first with tests/live.py (every member, volume and mute restored in their own step afterwards). It starts the real
service (`sonos service run`, no fake rooms) on :8737 with this extension's ID in `extension_ids`, launches Chrome as in README.md,
loads the unmodified `extension/`, opens the tone page, chooses Office in the popup and clicks Stream tab. Then it polls Office's state
for 60 s, clicks Stop, and times how long Office takes to leave PLAYING. Observations are printed as `OBS name: value` lines.
"""

import asyncio, json, os, shutil, signal, subprocess, sys, tempfile, time, urllib.request
import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import soco, live

EXT = os.path.join(ROOT, "extension")
EXTID = open(os.path.join(HERE, "extid.txt")).read().strip()
SERVICE = "http://localhost:8737"
PAGES = "http://127.0.0.1:8798"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HOLD_SECONDS = 60

cfgdir = tempfile.mkdtemp(prefix="sonos-office-cfg-")
open(os.path.join(cfgdir, "config.toml"), "w").write(f'extension_ids = ["{EXTID}"]\n')
svc_log = open(os.path.join(cfgdir, "service.log"), "w")
svc = subprocess.Popen(["uv", "run", "sonos", "service", "run", "--port", "8737"], cwd=ROOT, env=dict(os.environ, SONOS_CLI_CONFIG_DIR=cfgdir),
                       stdout=svc_log, stderr=subprocess.STDOUT, start_new_session=True)
pages_dir = tempfile.mkdtemp(prefix="sonos-pages-")
shutil.copy(os.path.join(HERE, "page.html"), os.path.join(pages_dir, "tone.html"))
pages = subprocess.Popen([sys.executable, "-m", "http.server", "8798", "--bind", "127.0.0.1"], cwd=pages_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
profile = tempfile.mkdtemp(prefix="chrome-office-")
chrome = subprocess.Popen([CHROME, f"--user-data-dir={profile}", "--remote-debugging-port=9336", "--enable-unsafe-extension-debugging",
                           f"--allowlisted-extension-id={EXTID}", "--auto-accept-this-tab-capture", "--autoplay-policy=no-user-gesture-required",
                           "--no-first-run", "--no-default-browser-check", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def obs(name, value):
    print(f"OBS {name}: {value}", flush=True)


def get(path):
    return json.load(urllib.request.urlopen(SERVICE + path, timeout=10))


class CDP:
    def __init__(self, ws):
        self.ws, self.id, self.pending = ws, 0, {}

    async def run(self):
        async for m in self.ws:
            d = json.loads(m)
            if "id" in d and d["id"] in self.pending:
                self.pending.pop(d["id"]).set_result(d)

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


async def run_stream(office_uid, coord):
    v = None
    for _ in range(60):
        try:
            v = json.load(urllib.request.urlopen("http://127.0.0.1:9336/json/version"))
            break
        except Exception:
            await asyncio.sleep(0.3)
    obs("browser", v["Browser"])
    async with websockets.connect(v["webSocketDebuggerUrl"], max_size=None) as ws:
        c = CDP(ws)
        asyncio.create_task(c.run())
        await c.send("Extensions.loadUnpacked", {"path": EXT})
        await asyncio.sleep(1.5)
        targets = (await c.send("Target.getTargets"))["result"]["targetInfos"]
        sw = next(t for t in targets if t["type"] == "service_worker" and EXTID in t["url"])
        sw_sid = (await c.send("Target.attachToTarget", {"targetId": sw["targetId"], "flatten": True}))["result"]["sessionId"]
        page = (await c.send("Target.createTarget", {"url": f"{PAGES}/tone.html"}))["result"]["targetId"]
        await c.send("Target.activateTarget", {"targetId": page})
        await asyncio.sleep(1.5)
        await c.eval("chrome.windows.getLastFocused().then(w => chrome.action.openPopup({windowId: w.id}))", sw_sid, gesture=True)
        pop = None
        for _ in range(40):
            await asyncio.sleep(0.15)
            ts = (await c.send("Target.getTargets"))["result"]["targetInfos"]
            pop = next((t for t in ts if t["url"].startswith(f"chrome-extension://{EXTID}/popup.html") and t["type"] in ("page", "other")), None)
            if pop:
                break
        sid = (await c.send("Target.attachToTarget", {"targetId": pop["targetId"], "flatten": True}))["result"]["sessionId"]
        await asyncio.sleep(1.5)
        obs("popup rooms", await c.eval("JSON.stringify([...document.querySelectorAll('#room option')].map(o => o.textContent))", sid))
        await c.eval(f"document.getElementById('room').value = '{office_uid}'; document.getElementById('room').dispatchEvent(new Event('change')); 1", sid)
        t0 = time.time()
        await c.eval("[...document.querySelectorAll('#actions button')].find(b => b.textContent === 'Stream tab').click(); 1", sid)
        playing_at = None
        for i in range(HOLD_SECONDS + 15):
            await asyncio.sleep(1)
            st = get(f"/api/rooms/{office_uid}/state")
            if st["state"] == "PLAYING" and playing_at is None:
                playing_at = time.time() - t0
                obs("Office first PLAYING after (s) from the click", round(playing_at, 1))
            if playing_at is not None and i % 10 == 0:
                obs(f"t+{time.time() - t0:.0f}s", f"{st['state']} {st['track']['uri'][:60]} position {st['position']}")
            if playing_at is not None and time.time() - t0 - playing_at >= HOLD_SECONDS:
                break
        obs("popup note while streaming", await c.eval("document.getElementById('note').textContent", sid))
        mi, ti, tr = coord.get_current_media_info(), coord.get_current_transport_info(), coord.get_current_track_info()
        obs("speaker raw: media uri", mi.get("uri"))
        obs("speaker raw: transport", {k: ti.get(k) for k in ("current_transport_state", "current_transport_status", "current_speed")})
        obs("speaker raw: track uri / title / position", [tr.get("uri"), tr.get("title"), tr.get("position")])
        ok_all = get(f"/api/rooms/{office_uid}/state")
        obs("sleep assertion while streaming (FR17)", get("/api/health")["sleep_assertion"])
        obs("Office after the hold", f"{ok_all['state']} source={ok_all['source']} uri={ok_all['track']['uri'][:70]}")
        t1 = time.time()
        await c.eval("[...document.querySelectorAll('#actions button')].find(b => b.textContent === 'Stop').click(); 1", sid)
        left = None
        for _ in range(40):
            await asyncio.sleep(0.5)
            if get(f"/api/rooms/{office_uid}/state")["state"] != "PLAYING":
                left = time.time() - t1
                break
        obs("seconds from Stop until Office is no longer PLAYING", round(left, 1) if left is not None else None)
        await asyncio.sleep(3)
        obs("Office state after Stop", get(f"/api/rooms/{office_uid}/state")["state"])
        await asyncio.sleep(4)
        obs("sleep assertion after Stop", get("/api/health")["sleep_assertion"])
        obs("popup note after Stop", await c.eval("document.getElementById('note').textContent", sid))


def main():
    for _ in range(80):
        try:
            rooms = get("/api/rooms")
            if any(r["name"] == "Office" for r in rooms):
                break
        except Exception:
            time.sleep(0.5)
    office = next(r for r in rooms if r["name"] == "Office")
    obs("office online", office["online"])
    coord = soco.SoCo("192.168.0.21").group.coordinator
    with live.muted_room(coord) as members:
        obs("muted", [(m.player_name, m.mute) for m in members])
        try:
            asyncio.run(run_stream(office["uid"], coord))
        finally:
            try:
                coord.stop()
            except Exception as e:
                obs("coordinator.stop failed", e)
    obs("after restore", [(m.player_name, m.volume, m.mute) for m in soco.SoCo("192.168.0.21").group.members])


try:
    main()
finally:
    chrome.terminate()
    pages.terminate()
    try:
        os.killpg(svc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    print("service log:", os.path.join(cfgdir, "service.log"))
