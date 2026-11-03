import asyncio, json, subprocess, sys, time, os, tempfile, urllib.request
import websockets
HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.join(HERE, "ext"); EXTID = open(os.path.join(HERE, "extid.txt")).read().strip()
DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 60
ud = tempfile.mkdtemp(prefix="chrome-spike-")
http = subprocess.Popen([sys.executable, "-m", "http.server", "8798", "--bind", "127.0.0.1"], cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
chrome = subprocess.Popen(["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", f"--user-data-dir={ud}", "--remote-debugging-port=9333",
    "--enable-unsafe-extension-debugging",
    f"--allowlisted-extension-id={EXTID}", "--auto-accept-this-tab-capture", "--autoplay-policy=no-user-gesture-required",
    "--no-first-run", "--no-default-browser-check", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
class CDP:
    def __init__(s, ws): s.ws = ws; s.id = 0; s.pending = {}; s.events = []
    async def run(s):
        async for m in s.ws:
            d = json.loads(m)
            if "id" in d and d["id"] in s.pending: s.pending.pop(d["id"]).set_result(d)
            else: s.events.append(d)
    async def send(s, method, params=None, sid=None):
        s.id += 1; f = asyncio.get_event_loop().create_future(); s.pending[s.id] = f
        msg = {"id": s.id, "method": method, "params": params or {}}
        if sid: msg["sessionId"] = sid
        await s.ws.send(json.dumps(msg)); return await f
async def main():
    v = None
    for _ in range(50):
        try: v = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/version")); break
        except Exception: await asyncio.sleep(0.3)
    if v is None: sys.exit("Chrome did not answer on port 9333")
    print("browser", v["Browser"], flush=True)
    async with websockets.connect(v["webSocketDebuggerUrl"], max_size=None) as ws:
        c = CDP(ws); asyncio.create_task(c.run())
        lr = await c.send("Extensions.loadUnpacked", {"path": EXT}); print("loadUnpacked:", lr, flush=True)
        global EXTID
        EXTID = lr.get("result", {}).get("id", EXTID)
        await c.send("Target.setDiscoverTargets", {"discover": True})
        r = await c.send("Target.createTarget", {"url": "http://127.0.0.1:8798/page.html"}); tone = r["result"]["targetId"]
        await asyncio.sleep(2)
        targets = (await c.send("Target.getTargets"))["result"]["targetInfos"]
        print("targets:", [(t["type"], t["url"][:60]) for t in targets], flush=True)
        sw = [t for t in targets if t["type"] == "service_worker" and EXTID in t["url"]]
        if not sw: print("EXTENSION NOT LOADED"); return
        sid = (await c.send("Target.attachToTarget", {"targetId": sw[0]["targetId"], "flatten": True}))["result"]["sessionId"]
        r = await c.send("Runtime.evaluate", {"expression": "[typeof startCapture, chrome.runtime.getManifest().name, location.href].join()", "returnByValue": True}, sid); print("sw ctx:", r["result"], flush=True)
        # tab id of the tone page, via chrome.tabs.query in the worker
        r = await c.send("Runtime.evaluate", {"expression": "chrome.tabs.query({url:'http://127.0.0.1:8798/*'}).then(t=>t[0].id)", "awaitPromise": True, "returnByValue": True}, sid)
        tabid = r["result"]["result"].get("value"); print("tab id", tabid, r["result"].get("exceptionDetails"), flush=True)
        r = await c.send("Runtime.evaluate", {"expression": f"startCapture({tabid}, 'ws://127.0.0.1:8799/ingest').then(x=>'ok '+x.length, e=>'ERR '+e.message)", "awaitPromise": True, "returnByValue": True}, sid)
        print("startCapture:", r["result"]["result"].get("value"), r["result"].get("exceptionDetails"), flush=True)
        # put the tone tab in the background
        await c.send("Target.createTarget", {"url": "about:blank#other"}); await asyncio.sleep(1)
        infos = (await c.send("Target.getTargets"))["result"]["targetInfos"]
        print("pages:", [(t["url"][:40], t.get("attached")) for t in infos if t["type"] == "page"], flush=True)
        for i in range(DUR // 10):
            await asyncio.sleep(10); print(f"t+{(i+1)*10}s capturing", flush=True)
        await c.send("Runtime.evaluate", {"expression": "stopCapture()", "awaitPromise": True}, sid)
        await asyncio.sleep(2)
try:
    asyncio.run(main())
finally:
    chrome.terminate(); http.terminate()
