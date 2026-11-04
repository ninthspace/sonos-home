"""Times "Also play here" against the stream through the real extension's popup (docs/quick/05). Not collected by pytest.

    .venv/bin/python tests/extension_harness/delay_driver.py            # the extension as it is
    .venv/bin/python tests/extension_harness/delay_driver.py --delay 0  # control: a copy with LOCAL_DELAY_S = 0

Needs port 8737 free (the extension only talks to localhost:8737): it starts `sonos service run --fake-rooms` from this checkout's
.venv on that port with this extension's ID allowed, serves the 440 Hz tone page on 127.0.0.1:8798, launches branded Chrome as in
README.md, loads `extension/` (or the patched copy, same pinned ID) and prints `OBS name: value` lines.
"""

import asyncio, json, os, re, shutil, signal, socket, struct, subprocess, sys, tempfile, time, urllib.error, urllib.request
import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
EXTID = open(os.path.join(HERE, "extid.txt")).read().strip()
SERVICE = "http://localhost:8737"
PAGES = "http://127.0.0.1:8798"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
SONOS = os.path.join(ROOT, ".venv", "bin", "sonos")

try:                                           # never drive a real service: the popup would stream to real rooms
    socket.create_connection(("127.0.0.1", 8737), timeout=1).close()
    sys.exit("port 8737 is in use (the installed service?): stop it first, e.g. launchctl bootout gui/$(id -u)/local.sonos-cli")
except OSError:
    pass

EXT = os.path.join(ROOT, "extension")
if "--delay" in sys.argv:
    value = sys.argv[sys.argv.index("--delay") + 1]
    EXT = os.path.join(tempfile.mkdtemp(prefix="sonos-ext-"), "extension")
    shutil.copytree(os.path.join(ROOT, "extension"), EXT)
    path = os.path.join(EXT, "offscreen.js")
    src, n = re.subn(r"const LOCAL_DELAY_S = [0-9.]+;", f"const LOCAL_DELAY_S = {value};", open(path).read())
    assert n == 1, "LOCAL_DELAY_S not found"
    open(path, "w").write(src)

cfgdir = tempfile.mkdtemp(prefix="sonos-delay-cfg-")
open(os.path.join(cfgdir, "config.toml"), "w").write(f'extension_ids = ["{EXTID}"]\n')
svc_log = open(os.path.join(cfgdir, "service.log"), "w")
svc = subprocess.Popen([SONOS, "service", "run", "--fake-rooms", "--port", "8737"], cwd=ROOT,
                       env=dict(os.environ, SONOS_CLI_CONFIG_DIR=cfgdir), stdout=svc_log, stderr=subprocess.STDOUT, start_new_session=True)
pages_dir = tempfile.mkdtemp(prefix="sonos-pages-")
shutil.copy(os.path.join(HERE, "page.html"), os.path.join(pages_dir, "tone.html"))
pages = subprocess.Popen([sys.executable, "-m", "http.server", "8798", "--bind", "127.0.0.1"], cwd=pages_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
profile = tempfile.mkdtemp(prefix="chrome-delay-")
chrome = subprocess.Popen([CHROME, f"--user-data-dir={profile}", "--remote-debugging-port=9336", "--enable-unsafe-extension-debugging",
                           f"--allowlisted-extension-id={EXTID}", "--auto-accept-this-tab-capture", "--autoplay-policy=no-user-gesture-required",
                           "--no-first-run", "--no-default-browser-check", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def obs(name, value):
    print(f"OBS {name}: {value}", flush=True)


def service(path):
    return json.load(urllib.request.urlopen(SERVICE + path, timeout=20))


def live_id(uid):
    uri = service(f"/api/rooms/{uid}/state")["track"]["uri"]
    return uri.split("/live/", 1)[1].removesuffix(".mp3") if "/live/" in uri else None


def is_404(session_id):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:8737/live/{session_id}.mp3", timeout=3).close()
        return False
    except urllib.error.HTTPError as e:
        return e.code == 404
    except Exception:
        return False


def stream_tone_hz(session_id, seconds=3.0):
    """Zero crossings per second of the first `seconds` the service serves: the stream to the speaker, which is not delayed."""
    s = socket.create_connection(("127.0.0.1", 8737), timeout=5)
    s.sendall(f"GET /live/{session_id}.mp3 HTTP/1.1\r\nHost: localhost:8737\r\n\r\n".encode())
    data, end = b"", time.time() + seconds
    s.settimeout(0.5)
    while time.time() < end:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            break
        data += chunk
    s.close()
    body = data.partition(b"\r\n\r\n")[2]
    r = subprocess.run([FFMPEG, "-v", "error", "-f", "mp3", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "48000", "pipe:1"], input=body, capture_output=True)
    samples = struct.unpack(f"<{len(r.stdout) // 2}h", r.stdout[:len(r.stdout) // 2 * 2])
    mid = samples[4800:]
    if len(mid) < 24000:
        return 0.0
    return round(sum(1 for a, b in zip(mid, mid[1:]) if a < 0 <= b) / (len(mid) / 48000), 1)


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


async def main():
    rooms = None
    for _ in range(80):
        try:
            rooms = service("/api/rooms")
            break
        except Exception:
            await asyncio.sleep(0.5)
    if rooms is None:
        sys.exit("the fake service did not start (is port 8737 free?)")
    uid = {r["name"]: r["uid"] for r in rooms}
    v = None
    for _ in range(60):
        try:
            v = json.load(urllib.request.urlopen("http://127.0.0.1:9336/json/version"))
            break
        except Exception:
            await asyncio.sleep(0.3)
    obs("browser", v["Browser"])
    obs("extension", EXT)
    async with websockets.connect(v["webSocketDebuggerUrl"], max_size=None) as ws:
        c = CDP(ws)
        asyncio.create_task(c.run())
        lr = await c.send("Extensions.loadUnpacked", {"path": EXT})
        obs("loaded extension id", f"{lr.get('result', {}).get('id')} (expected {EXTID})")
        await asyncio.sleep(1.5)
        targets = (await c.send("Target.getTargets"))["result"]["targetInfos"]
        sw = next(t for t in targets if t["type"] == "service_worker" and EXTID in t["url"])
        sw_sid = (await c.send("Target.attachToTarget", {"targetId": sw["targetId"], "flatten": True}))["result"]["sessionId"]

        async def open_tone():
            r = await c.send("Target.createTarget", {"url": f"{PAGES}/tone.html"})
            await c.send("Target.activateTarget", {"targetId": r["result"]["targetId"]})
            await asyncio.sleep(1.5)
            return r["result"]["targetId"]

        async def open_popup():
            await c.eval("chrome.windows.getLastFocused().then(w => chrome.action.openPopup({windowId: w.id}))", sw_sid, gesture=True)
            for _ in range(40):
                await asyncio.sleep(0.15)
                ts = (await c.send("Target.getTargets"))["result"]["targetInfos"]
                pop = next((t for t in ts if t["url"].startswith(f"chrome-extension://{EXTID}/popup.html")), None)
                if pop:
                    sid = (await c.send("Target.attachToTarget", {"targetId": pop["targetId"], "flatten": True}))["result"]["sessionId"]
                    await asyncio.sleep(1.2)
                    return pop["targetId"], sid
            raise RuntimeError("popup did not open")

        async def set_local(sid, on):
            await c.eval(f"document.getElementById('local').checked = {str(on).lower()}; document.getElementById('local').dispatchEvent(new Event('change')); 1", sid)

        async def click(sid, label):
            for _ in range(50):                                  # the popup shows "Checking…" first; wait for the button itself
                done = await c.eval(f"(() => {{ const b = [...document.querySelectorAll('#actions button')].find(b => b.textContent === '{label}' && !b.hidden && !b.disabled); if (b) b.click(); return !!b; }})()", sid)
                if done is True:
                    return
                await asyncio.sleep(0.1)
            raise RuntimeError(f"no {label} button")

        async def offscreen():
            for _ in range(40):
                ts = (await c.send("Target.getTargets"))["result"]["targetInfos"]
                off = next((t for t in ts if t["url"].startswith(f"chrome-extension://{EXTID}/offscreen.html")), None)
                if off:
                    return (await c.send("Target.attachToTarget", {"targetId": off["targetId"], "flatten": True}))["result"]["sessionId"]
                await asyncio.sleep(0.1)
            raise RuntimeError("no offscreen document")

        async def levels(off_sid, seconds, step=0.25):
            """(seconds since the call, RMS on the local branch) every `step` s; None would mean nothing is captured."""
            out, t0 = [], time.time()
            while time.time() - t0 < seconds:
                value = await c.eval("self.localLevel()", off_sid)
                out.append((round(time.time() - t0, 2), None if value is None else round(value, 4)))
                await asyncio.sleep(step)
            return out

        def first(series, pred):
            return next((t for t, v in series if v is not None and pred(v)), None)

        async def wait_live(room, timeout=12):
            t0 = time.time()
            while time.time() - t0 < timeout:
                sid_ = live_id(uid[room])
                if sid_ and not is_404(sid_):
                    return sid_
                await asyncio.sleep(0.1)
            return None

        # ---- A: ticked before Stream tab -----------------------------------------------------------------------------------------
        tone = await open_tone()
        pop, sid = await open_popup()
        await c.eval(f"document.getElementById('room').value = '{uid['Kitchen']}'; document.getElementById('room').dispatchEvent(new Event('change')); 1", sid)
        await set_local(sid, True)
        await click(sid, "Stream tab")
        stream_id = await wait_live("Kitchen")
        obs("A the stream is answering", bool(stream_id))
        off = await offscreen()
        series = await levels(off, 7.0)
        obs("A local-branch RMS after the stream answered (s, RMS)", series)
        obs("A max RMS in the first 3 s", max((v for t, v in series if t < 3 and v is not None), default=None))
        obs("A first time RMS > 0.05 (s)", first(series, lambda v: v > 0.05))
        obs("A the stream itself: tone Hz in its first 3 s", stream_tone_hz(stream_id))
        await click(sid, "Stop")
        await asyncio.sleep(2)
        await c.send("Target.closeTarget", {"targetId": pop})
        await c.send("Target.closeTarget", {"targetId": tone})
        await asyncio.sleep(1.5)

        # ---- B: off at the start, ticked once the delay has filled, then unticked ------------------------------------------------
        tone = await open_tone()
        pop, sid = await open_popup()
        await c.eval(f"document.getElementById('room').value = '{uid['Kitchen']}'; document.getElementById('room').dispatchEvent(new Event('change')); 1", sid)
        await set_local(sid, False)
        await click(sid, "Stream tab")
        stream_id = await wait_live("Kitchen")
        obs("B the stream is answering", bool(stream_id))
        off = await offscreen()
        series = await levels(off, 5.5)
        obs("B off throughout the first 5.5 s: max RMS", max((v for _, v in series if v is not None), default=None))
        await set_local(sid, True)
        series = await levels(off, 2.0)
        obs("B ticked: (s, RMS)", series)
        obs("B ticked: first time RMS > 0.05 (s)", first(series, lambda v: v > 0.05))
        await set_local(sid, False)
        series = await levels(off, 2.0)
        obs("B unticked: (s, RMS)", series)
        obs("B unticked: first time RMS < 0.01 (s)", first(series, lambda v: v < 0.01))
        obs("B the stream is still answering", not is_404(stream_id))
        await click(sid, "Stop")
        await asyncio.sleep(1)


try:
    asyncio.run(main())
finally:
    chrome.terminate()
    pages.terminate()
    try:
        os.killpg(svc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    print("service log:", os.path.join(cfgdir, "service.log"))
