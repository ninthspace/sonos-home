"""Drives "Stream tab" through the real extension's popup in branded Chrome against the fake-rooms service (epic 01-07 Story 3).
Not collected by pytest.

    .venv/bin/python tests/extension_harness/stream_driver.py               # needs port 8737 free
    .venv/bin/python tests/extension_harness/stream_driver.py --port 8747   # a copy of the extension patched to that port

Starts `sonos service run --fake-rooms` from .venv on :8737, or the --port given (this extension's ID in `extension_ids`), serves the
440 Hz tone page and a plain page on 127.0.0.1:8798, launches Chrome as in README.md, loads `extension/` (unmodified on 8737, else a
copy whose `localhost:8737` is patched to the port), refuses to start if that port is already taken, and for each scenario prints
`OBS name: value` lines. The listener reads the service's `/live/<id>.mp3` over a raw socket and measures the tone with ffmpeg.
"""

import asyncio, json, os, shutil, signal, socket, struct, subprocess, sys, tempfile, time, urllib.error, urllib.request
import websockets

PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8737
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
EXT = os.path.join(ROOT, "extension")
EXTID = open(os.path.join(HERE, "extid.txt")).read().strip()
SERVICE = f"http://localhost:{PORT}"
PAGES = "http://127.0.0.1:8798"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"   # the harness drives Chrome, not the service: any ffmpeg will do

cfgdir = tempfile.mkdtemp(prefix="sonos-stream-cfg-")
open(os.path.join(cfgdir, "config.toml"), "w").write(f'extension_ids = ["{EXTID}"]\n')
svc_log = open(os.path.join(cfgdir, "service.log"), "w")
try:                                           # never drive a real service: the popup would stream to real rooms
    socket.create_connection(("127.0.0.1", PORT), timeout=1).close()
    sys.exit(f"port {PORT} is in use (the installed service?): stop it first, or pass --port with a free one")
except OSError:
    pass
if PORT != 8737:                               # the extension only talks to localhost:8737: load a copy that talks to PORT instead
    copy = os.path.join(tempfile.mkdtemp(prefix="sonos-ext-"), "extension")
    shutil.copytree(EXT, copy)
    for name in ("popup.js", "offscreen.js", "manifest.json"):
        path = os.path.join(copy, name)
        src = open(path).read()
        assert "localhost:8737" in src, name
        open(path, "w").write(src.replace("localhost:8737", f"localhost:{PORT}"))
    EXT = copy
svc = subprocess.Popen([os.path.join(ROOT, ".venv", "bin", "sonos"), "service", "run", "--fake-rooms", "--port", str(PORT)], cwd=ROOT,
                       env=dict(os.environ, SONOS_CLI_CONFIG_DIR=cfgdir), stdout=svc_log, stderr=subprocess.STDOUT, start_new_session=True)
pages_dir = tempfile.mkdtemp(prefix="sonos-pages-")
shutil.copy(os.path.join(HERE, "page.html"), os.path.join(pages_dir, "tone.html"))
shutil.copy(os.path.join(HERE, "pages", "plain.html"), os.path.join(pages_dir, "plain.html"))
pages = subprocess.Popen([sys.executable, "-m", "http.server", "8798", "--bind", "127.0.0.1"], cwd=pages_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
profile = tempfile.mkdtemp(prefix="chrome-stream-")
chrome = subprocess.Popen([CHROME, f"--user-data-dir={profile}", "--remote-debugging-port=9335", "--enable-unsafe-extension-debugging",
                           f"--allowlisted-extension-id={EXTID}", "--auto-accept-this-tab-capture", "--autoplay-policy=no-user-gesture-required",
                           "--no-first-run", "--no-default-browser-check", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def obs(name, value):
    print(f"OBS {name}: {value}", flush=True)


def service(path, body=None):
    headers = {"Content-Type": "application/json", "Origin": SERVICE}
    req = urllib.request.Request(SERVICE + path, json.dumps(body).encode() if body is not None else None, headers if body is not None else {})
    return json.load(urllib.request.urlopen(req, timeout=20))


def live_url_of(uid):
    uri = service(f"/api/rooms/{uid}/state")["track"]["uri"]
    return uri.split("/live/", 1)[1].removesuffix(".mp3") if "/live/" in uri else None


def listen(session_id, seconds):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    s.sendall(f"GET /live/{session_id}.mp3 HTTP/1.1\r\nHost: localhost:{PORT}\r\n\r\n".encode())
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
    head, _, body = data.partition(b"\r\n\r\n")
    return head, body


def tone_hz(mp3):
    r = subprocess.run([FFMPEG, "-v", "error", "-f", "mp3", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "48000", "pipe:1"], input=mp3, capture_output=True)
    samples = struct.unpack(f"<{len(r.stdout) // 2}h", r.stdout[:len(r.stdout) // 2 * 2])
    mid = samples[24000:24000 + 96000]
    if len(mid) < 24000:
        return 0.0, r.stderr.decode()[:100]
    return round(sum(1 for a, b in zip(mid, mid[1:]) if a < 0 <= b) / (len(mid) / 48000), 1), r.stderr.decode()[:100]


def is_404(session_id):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/live/{session_id}.mp3", timeout=3).close()
        return False
    except urllib.error.HTTPError as e:
        return e.code == 404
    except Exception:
        return False


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
        sys.exit("the fake service did not start")
    uid = {r["name"]: r["uid"] for r in rooms}
    v = None
    for _ in range(60):
        try:
            v = json.load(urllib.request.urlopen("http://127.0.0.1:9335/json/version"))
            break
        except Exception:
            await asyncio.sleep(0.3)
    obs("browser", v["Browser"])
    async with websockets.connect(v["webSocketDebuggerUrl"], max_size=None) as ws:
        c = CDP(ws)
        asyncio.create_task(c.run())
        lr = await c.send("Extensions.loadUnpacked", {"path": EXT})
        obs("loaded extension id", f"{lr.get('result', {}).get('id')} (expected {EXTID})")
        await asyncio.sleep(1.5)
        targets = (await c.send("Target.getTargets"))["result"]["targetInfos"]
        sw = next(t for t in targets if t["type"] == "service_worker" and EXTID in t["url"])
        sw_sid = (await c.send("Target.attachToTarget", {"targetId": sw["targetId"], "flatten": True}))["result"]["sessionId"]
        obs("manifest permissions" + (" (unmodified)" if PORT == 8737 else f" (a copy patched to port {PORT})"), await c.eval("JSON.stringify(chrome.runtime.getManifest().permissions) + JSON.stringify(chrome.runtime.getManifest().host_permissions)", sw_sid))

        async def open_page(name):
            r = await c.send("Target.createTarget", {"url": f"{PAGES}/{name}"})
            tid = r["result"]["targetId"]
            await c.send("Target.activateTarget", {"targetId": tid})
            await asyncio.sleep(1.5)
            return tid

        async def open_popup():
            out = await c.eval("chrome.windows.getLastFocused().then(w => chrome.windows.update(w.id, {focused: true})).then(w => chrome.action.openPopup({windowId: w.id})).then(() => 'opened', e => 'ERR ' + e.message)", sw_sid, gesture=True)
            for _ in range(40):
                await asyncio.sleep(0.15)
                ts = (await c.send("Target.getTargets"))["result"]["targetInfos"]
                pop = next((t for t in ts if t["url"].startswith(f"chrome-extension://{EXTID}/popup.html") and t["type"] in ("page", "other")), None)
                if pop:
                    sid = (await c.send("Target.attachToTarget", {"targetId": pop["targetId"], "flatten": True}))["result"]["sessionId"]
                    await asyncio.sleep(1.2)
                    return pop["targetId"], sid
            raise RuntimeError("popup did not open: " + str(out))

        async def close(tid):
            await c.send("Target.closeTarget", {"targetId": tid})
            await asyncio.sleep(0.4)

        async def pick(sid, room):
            await c.eval(f"document.getElementById('room').value = '{uid[room]}'; document.getElementById('room').dispatchEvent(new Event('change')); 1", sid)

        async def click(sid, label):
            return await c.eval(f"[...document.querySelectorAll('#actions button')].find(b => b.textContent === '{label}').click(); 1", sid)

        async def note(sid):
            return await c.eval("document.getElementById('note').textContent", sid)

        async def buttons(sid):
            return await c.eval("JSON.stringify([...document.querySelectorAll('#actions button')].filter(b => !b.hidden).map(b => b.textContent))", sid)

        used = set()

        async def wait_live(room, timeout=12):
            """The id of a NEW stream for the room that is really serving (the room's cached URI can still name an ended one)."""
            t0 = time.time()
            while time.time() - t0 < timeout:
                sid_ = live_url_of(uid[room])
                if sid_ and sid_ not in used and not is_404(sid_):
                    used.add(sid_)
                    return sid_
                await asyncio.sleep(0.2)
            return None

        async def ended_within(session_id, room, limit=8, since=None):
            t0 = since or time.time()
            while time.time() - t0 < limit:
                if is_404(session_id):
                    return round(time.time() - t0, 1)
                await asyncio.sleep(0.2)
            return None

        # ---- 1: stream the tone tab; keep going with the popup closed and the tab in the background -------------------------
        tone = await open_page("tone.html")
        pop, sid = await open_popup()
        obs("1 popup controls (labels, selects, checkboxes)", await c.eval("JSON.stringify({labels: [...document.querySelectorAll('label')].map(l => l.textContent.trim()), selects: document.querySelectorAll('select').length, checkboxes: document.querySelectorAll('input[type=checkbox]').length})", sid))
        obs("1 buttons on the tone page", await buttons(sid))
        await pick(sid, "Kitchen")
        await click(sid, "Stream tab")
        stream_id = await wait_live("Kitchen")
        obs("1 popup note after Stream tab", await note(sid))
        obs("1 Kitchen reports our stream", bool(stream_id))
        obs("1 the room's URI starts", service(f"/api/rooms/{uid['Kitchen']}/state")["track"]["uri"].split("/live/")[0])
        head, body = listen(stream_id, 5)
        obs("1 listener head", head.split(b"\r\n")[0].decode())
        obs("1 tone at first listen (Hz, ffmpeg stderr)", tone_hz(body))
        await close(pop)
        other = await c.send("Target.createTarget", {"url": f"{PAGES}/plain.html"})            # the tone tab goes to the background
        await c.send("Target.activateTarget", {"targetId": other["result"]["targetId"]})
        await asyncio.sleep(8)
        head, body = listen(stream_id, 4)
        obs("1 after 8 s with the popup closed and the tab in the background: bytes", len(body))
        obs("1 tone then (Hz, ffmpeg stderr)", tone_hz(body))
        obs("1 Kitchen state", service(f"/api/rooms/{uid['Kitchen']}/state")["state"])
        await c.send("Target.closeTarget", {"targetId": other["result"]["targetId"]})
        await c.send("Target.activateTarget", {"targetId": tone})
        await asyncio.sleep(1)

        # ---- 2a: Stop ends the stream within 5 s and the popup says why --------------------------------------------------------
        pop, sid = await open_popup()
        obs("2a buttons while streaming", await buttons(sid))
        obs("2a note while streaming", await note(sid))
        t0 = time.time()
        await click(sid, "Stop")
        took = await ended_within(stream_id, "Kitchen", since=t0)
        obs("2a seconds from Stop to /live/<id>.mp3 answering 404", took)
        await asyncio.sleep(1)
        obs("2a Kitchen state after Stop", service(f"/api/rooms/{uid['Kitchen']}/state")["state"])
        await asyncio.sleep(0.2)
        obs("2a popup note about 0.2 s after Stop was clicked", await note(sid))
        await close(pop)
        pop, sid = await open_popup()
        obs("2a popup note when reopened", await note(sid))
        await close(pop)

        # ---- 2b: closing the tab ends it within 5 s, and the popup says why ------------------------------------------------------
        pop, sid = await open_popup()
        await pick(sid, "Kitchen")
        await click(sid, "Stream tab")
        stream_id = await wait_live("Kitchen")
        await close(pop)
        await asyncio.sleep(2)
        obs("2b the stream was answering just before the tab was closed", not is_404(stream_id))
        t0 = time.time()
        await c.send("Target.closeTarget", {"targetId": tone})
        took = await ended_within(stream_id, "Kitchen", since=t0)
        obs("2b seconds from closing the tab to 404", took)
        plain = await open_page("plain.html")
        pop, sid = await open_popup()
        obs("2b popup note on another page", await note(sid))
        await close(pop)
        await c.send("Target.closeTarget", {"targetId": plain})

        # ---- 4: a second stream stops the first -----------------------------------------------------------------------------------------
        tone1 = await open_page("tone.html")
        pop, sid = await open_popup()
        await pick(sid, "Kitchen")
        await click(sid, "Stream tab")
        first_id = await wait_live("Kitchen")
        await close(pop)
        tone2 = await open_page("tone.html")
        pop, sid = await open_popup()
        obs("4 buttons on the second tone tab while the first streams", await buttons(sid))
        await pick(sid, "Lounge")
        t0 = time.time()
        await click(sid, "Stream tab")
        second_id = await wait_live("Lounge")
        took = await ended_within(first_id, "Kitchen", since=t0)
        obs("4 seconds until the first stream was gone", took)
        obs("4 sessions answering: first, second", [not is_404(first_id), not is_404(second_id)])
        obs("4 Kitchen state, Lounge state", [service(f"/api/rooms/{uid['Kitchen']}/state")["state"], service(f"/api/rooms/{uid['Lounge']}/state")["state"]])
        head, body = listen(second_id, 4)
        obs("4 second stream tone (Hz, ffmpeg stderr)", tone_hz(body))
        await click(sid, "Stop")
        await close(pop)
        await c.send("Target.closeTarget", {"targetId": tone1})
        await c.send("Target.closeTarget", {"targetId": tone2})
        await asyncio.sleep(1)

        # ---- 5: the tab is closed while the service is still answering (the service process is paused while the start is pending) ----
        pids = subprocess.run(["pgrep", "-f", f"sonos service run --fake-rooms --port {PORT}"], capture_output=True, text=True).stdout.split()
        tone = await open_page("tone.html")
        pop, sid = await open_popup()
        await pick(sid, "Kitchen")
        for pid in pids:
            os.kill(int(pid), signal.SIGSTOP)
        try:
            urllib.request.urlopen(SERVICE + "/api/health", timeout=1.5).close()
            paused = False
        except Exception:
            paused = True
        obs(f"5 service paused by SIGSTOP (pids {pids}): a request times out", paused)
        await click(sid, "Stream tab")
        await asyncio.sleep(1.5)
        obs("5 popup note while the start is pending", await note(sid))
        await c.send("Target.closeTarget", {"targetId": tone})
        await asyncio.sleep(1.5)
        for pid in pids:
            os.kill(int(pid), signal.SIGCONT)
        await asyncio.sleep(2.5)
        obs("5 the still-open popup's note once the service answered", await note(sid))
        await close(pop)
        plain = await open_page("plain.html")
        pop, sid = await open_popup()
        obs("5 the worker's stored state afterwards", await c.eval("chrome.storage.session.get('stream').then(x => JSON.stringify(x))", sw_sid))
        obs("5 popup note afterwards", await note(sid))
        obs("5 buttons afterwards (no Stop: nothing is streaming)", await buttons(sid))
        obs("5 Kitchen state", service(f"/api/rooms/{uid['Kitchen']}/state")["state"])
        sessions = [i for i in [live_url_of(uid['Kitchen'])] if i and not is_404(i)]
        obs("5 streams still answering", sessions)
        await close(pop)

        # ---- 6: Stop pressed during a pending start (the service is paused again) ------------------------------------------------------
        for label, action in (("6a Stop while the start is pending", "stop"),):
            tone = await open_page("tone.html")
            pop, sid = await open_popup()
            await pick(sid, "Kitchen")
            for pid in pids:
                os.kill(int(pid), signal.SIGSTOP)
            await click(sid, "Stream tab")
            await asyncio.sleep(1.0)
            t0 = time.time()
            if action == "stop":
                # the popup can't be reopened while the service is paused (it lists rooms from it), so send the very message the Stop button sends
                obs(f"{label}: the reply to Stop", await c.eval("chrome.runtime.sendMessage({type: 'stop'}).then(r => JSON.stringify(r))", sid))
                obs(f"{label}: seconds Stop took to be answered", round(time.time() - t0, 1))
                await asyncio.sleep(0.5)
                obs(f"{label}: the worker's state about 1 s after Stop (service still paused)", await c.eval("chrome.storage.session.get('stream').then(x => JSON.stringify(x))", sw_sid))
                for pid in pids:
                    os.kill(int(pid), signal.SIGCONT)
                await asyncio.sleep(2.5)
                obs(f"{label}: Kitchen state / streams answering after the service resumed", [service(f"/api/rooms/{uid['Kitchen']}/state")["state"], bool(live_url_of(uid['Kitchen']) and not is_404(live_url_of(uid['Kitchen'])))])
            await close(pop)
            await c.send("Target.closeTarget", {"targetId": tone})
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
