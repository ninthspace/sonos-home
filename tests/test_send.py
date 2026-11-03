"""POST /api/send, /api/send/preview and the /remote/ proxy (spec FR13, AD5)."""

import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from fakes import FakeSystem
from sonos_cli import config, remote, service
from sonos_cli.library import Library
from sonos_cli.rooms import RoomMonitor
from sonos_cli.server import local_ip_towards
from test_play_api import JSON, post_json

BASE = "http://127.0.0.1:8737"
FIX = Path(__file__).parent / "fixtures"
PUBLIC = "93.184.216.34"
RELEASE_URL = "https://shtl-experiment.com/hello-ep/"
MP3 = b"ID3" + bytes(range(256)) * 40


class Origin:
    """A pretend internet: records what was requested, and where (host header, IP, SNI)."""

    def __init__(self):
        self.requests = []
        self.routes = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({"url": str(request.url), "host": request.headers.get("host"), "range": request.headers.get("range"),
                              "method": request.method, "sni": request.extensions.get("sni_hostname"),
                              "accept_encoding": request.headers.get("accept-encoding")})
        path = request.url.path
        if path in self.routes:
            return self.routes[path](request)
        return httpx.Response(404, text="nope")


def resolve(table=None):
    table = table or {}
    return lambda host, port: table.get(host, [PUBLIC])


@pytest.fixture
def origin():
    o = Origin()
    page = (FIX / "shtl-hello-ep.html").read_text(encoding="utf-8")
    o.routes["/hello-ep/"] = lambda r: httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})

    def audio(r):
        data = MP3
        rng = r.headers.get("range")
        if rng:
            start, _, end = rng.removeprefix("bytes=").partition("-")
            start, end = int(start), int(end) if end else len(data) - 1
            return httpx.Response(206, content=data[start:end + 1], headers={"content-type": "audio/mpeg", "accept-ranges": "bytes",
                                                                             "content-range": f"bytes {start}-{end}/{len(data)}"})
        return httpx.Response(200, content=data, headers={"content-type": "audio/mpeg", "accept-ranges": "bytes"})

    o.routes["/hello-ep/1/mp3-v5/Ui6CBz2DJ_s/01 Halyard.mp3"] = audio
    o.routes["/hello-ep/1/mp3-v5/Ui6CBz2DJ_s/01%20Halyard.mp3"] = audio
    return o


def make(tmp_path, origin, resolver=None, fetcher=None):
    system = FakeSystem()
    cfg = config.Config(path=tmp_path / "service" / "config.toml")
    monitor = RoomMonitor(system.discover)
    monitor.refresh()
    fetcher = fetcher or remote.Fetcher(resolve=resolver or resolve(), transport=httpx.MockTransport(origin.handler))
    app = service.create_app(cfg, library=Library.create([]), allowed_hosts={"127.0.0.1"}, monitor=monitor, port=8737, fetcher=fetcher)
    return system, monitor, TestClient(app, base_url=BASE), cfg


def send(client, **body):
    return client.post("/api/send", json=body, headers=JSON)


# ---- extract and queue ---------------------------------------------------------------------------------------------

def test_send_a_page_url_queues_its_tracks_as_signed_remote_urls_with_matching_protocol_info(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    r = send(client, room=system.lounge.uid, url=RELEASE_URL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Hello EP" and body["total"] == 2 and body["started"] is True
    queue = system.lounge.queue
    assert [i.title for i in queue] == ["Halyard", "Undertow"]
    for item, track in zip(queue, body["tracks"]):
        uri = item.resources[0].uri
        assert uri.startswith(f"http://{local_ip_towards(system.lounge.ip_address)}:8737/remote/") and "shtl-experiment" not in uri
        assert item.resources[0].protocol_info == "http-get:*:audio/mpeg:*"
        assert remote.verify(uri.split("/remote/", 1)[1].split("/", 1)[0], remote.load_secret(cfg.path.parent / "remote-secret")) == track["url"]
    assert system.lounge.transport_state == "PLAYING"


def test_html_sent_by_the_extension_is_used_without_fetching_anything(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    html = '<audio src="/a & b.mp3"></audio><a href="c.flac">c</a>'
    r = send(client, room=system.lounge.uid, html=html, base_url="https://example.com/page/")
    assert r.status_code == 200 and [t["url"] for t in r.json()["tracks"]] == ["https://example.com/a%20&%20b.mp3", "https://example.com/page/c.flac"]
    assert origin.requests == []
    assert [i.resources[0].protocol_info for i in system.lounge.queue] == ["http-get:*:audio/mpeg:*", "http-get:*:audio/flac:*"]   # each item's own type


def test_titles_with_markup_characters_are_escaped_in_the_didl(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    html = '<a href="x.mp3">Tom &amp; Jerry &lt;b&gt;"live"&lt;/b&gt;</a>'
    assert send(client, room=system.lounge.uid, html=html, base_url="http://x.test/").status_code == 200
    from soco.data_structures import to_didl_string
    didl = to_didl_string(system.lounge.queue[0])
    assert "Tom &amp; Jerry &lt;b&gt;" in didl and "<b>" not in didl


def test_non_ascii_titles_survive_into_the_didl_and_the_queue(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    html = '<a href="x.mp3">Café – Ünïcode 日本 &amp; co</a>'
    assert send(client, room=system.lounge.uid, html=html, base_url="http://x.test/").status_code == 200
    from soco.data_structures import to_didl_string
    assert system.lounge.queue[0].title == "Café – Ünïcode 日本 & co"
    assert "Café – Ünïcode 日本 &amp; co" in to_didl_string(system.lounge.queue[0])


def test_append_adds_to_the_queue_and_replace_clears_it(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    html = '<a href="x.mp3">x</a>'
    send(client, room=system.lounge.uid, html=html, base_url="http://x.test/")
    send(client, room=system.lounge.uid, html='<a href="y.mp3">y</a>', base_url="http://x.test/", mode="append")
    assert [i.title for i in system.lounge.queue] == ["x", "y"]
    send(client, room=system.lounge.uid, html='<a href="z.mp3">z</a>', base_url="http://x.test/")
    assert [i.title for i in system.lounge.queue] == ["z"]


def test_a_page_with_nothing_playable_is_422_and_nothing_is_queued(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    r = send(client, room=system.lounge.uid, html="<p>nothing</p>", base_url="http://x.test/")
    assert r.status_code == 422 and "No audio Sonos can play" in r.json()["error"]
    assert system.lounge.queue == [] and system.lounge.calls == []


def test_preview_lists_the_tracks_without_queueing_anything(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    r = client.post("/api/send/preview", json={"url": RELEASE_URL}, headers=JSON)
    assert r.status_code == 200 and [t["title"] for t in r.json()["tracks"]] == ["Halyard", "Undertow"]
    assert system.lounge.queue == [] and all(d.calls == [] for d in system.devices)
    r = client.post("/api/send/preview", json={"html": "<p>x</p>", "base_url": "http://x.test/"}, headers=JSON)
    assert r.status_code == 422


@pytest.mark.parametrize("body, status", [
    ({}, 400), ({"room": 5, "url": RELEASE_URL}, 400), ({"room": "ROOM"}, 400), ({"room": "ROOM", "html": "<p/>"}, 400),
    ({"room": "ROOM", "html": "<p/>", "base_url": "ftp://x"}, 400), ({"room": "ROOM", "html": "<p/>", "base_url": "javascript:1"}, 400),
    ({"room": "RINCON_NOPE", "url": RELEASE_URL}, 404), ({"room": "ROOM", "url": "https://shtl-experiment.com/missing/"}, 502),
    ({"room": "ROOM", "url": "file:///etc/passwd"}, 403), ({"room": "ROOM", "url": "ftp://shtl-experiment.com/x"}, 403),
])
def test_send_errors_have_the_right_status_and_queue_nothing(tmp_path, origin, body, status):
    system, monitor, client, cfg = make(tmp_path, origin)
    body = {k: (system.lounge.uid if v == "ROOM" else v) for k, v in body.items()}
    r = send(client, **body)
    assert r.status_code == status, r.text
    assert "error" in r.json() and system.lounge.queue == [] and system.lounge.calls == []


# ---- the /remote/ proxy --------------------------------------------------------------------------------------------

AUDIO_URL = "https://shtl-experiment.com/hello-ep/1/mp3-v5/Ui6CBz2DJ_s/01%20Halyard.mp3"


def token_for(cfg, url):
    return remote.sign(url, remote.load_secret(cfg.path.parent / "remote-secret"))


def test_the_proxy_serves_the_origins_bytes_and_passes_range_through(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    t = token_for(cfg, AUDIO_URL)
    full = client.get(f"/remote/{t}")
    assert full.status_code == 200 and full.content == MP3 and full.headers["content-type"] == "audio/mpeg"
    part = client.get(f"/remote/{t}", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == MP3[100:200] and part.headers["content-range"] == f"bytes 100-199/{len(MP3)}"
    assert origin.requests[-1]["range"] == "bytes=100-199"
    head = client.head(f"/remote/{t}")
    assert head.status_code == 200 and head.headers["content-type"] == "audio/mpeg" and head.content == b"" and origin.requests[-1]["method"] == "HEAD"


def test_the_origin_is_reached_by_the_checked_ip_with_the_real_host_and_sni(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    client.get(f"/remote/{token_for(cfg, AUDIO_URL)}")
    req = origin.requests[-1]
    assert req["url"].startswith(f"https://{PUBLIC}/") and req["host"] == "shtl-experiment.com" and req["sni"] == "shtl-experiment.com"


def test_a_token_stays_valid_across_a_restart_and_the_secret_file_is_private(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    t = token_for(cfg, AUDIO_URL)
    system2, monitor2, client2, cfg2 = make(tmp_path, origin)            # a fresh app reading the same config folder
    assert client2.get(f"/remote/{t}").content == MP3
    assert (cfg.path.parent / "remote-secret").stat().st_mode & 0o777 == 0o600


def test_altered_unsigned_and_foreign_tokens_are_refused_and_nothing_is_fetched(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    good = token_for(cfg, AUDIO_URL)
    raw, mac = good.split(".")
    other = remote.sign("https://shtl-experiment.com/other.mp3", b"x" * 32)
    for bad in (remote._b64(b"https://evil.test/x.mp3") + "." + mac, good[:-2] + "AA", raw, raw + ".", "." + mac, "", other,
                remote._b64(AUDIO_URL.encode()) + "." + remote._b64(b"short"), "not base64!.zzz"):
        r = client.get(f"/remote/{bad}") if bad else client.get("/remote/")
        assert r.status_code in (403, 404), bad
    assert origin.requests == []
    assert client.get(f"/remote/{good}").status_code == 200                  # control: the real token works


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://shtl-experiment.com/x.mp3", "gopher://x/", "https:///nohost", "javascript:1"])
def test_a_signed_token_for_a_non_http_url_is_refused(tmp_path, origin, url):
    system, monitor, client, cfg = make(tmp_path, origin)
    r = client.get(f"/remote/{token_for(cfg, url)}")
    assert r.status_code == 403 and origin.requests == []


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.1.2.3", "192.168.0.21", "172.16.0.1", "169.254.169.254", "0.0.0.0", "100.64.0.1",
                                  "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1"])
def test_names_resolving_to_non_public_addresses_are_refused(tmp_path, origin, addr):
    system, monitor, client, cfg = make(tmp_path, origin, resolver=resolve({"shtl-experiment.com": [addr]}))
    r = client.get(f"/remote/{token_for(cfg, AUDIO_URL)}")
    assert r.status_code == 403 and origin.requests == []
    r = send(client, room=system.lounge.uid, url=RELEASE_URL)                  # and so is fetching the page itself
    assert r.status_code == 403 and system.lounge.queue == []


def test_one_private_address_among_public_ones_is_enough_to_refuse(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin, resolver=resolve({"shtl-experiment.com": [PUBLIC, "10.0.0.5"]}))
    assert client.get(f"/remote/{token_for(cfg, AUDIO_URL)}").status_code == 403


def test_a_redirect_is_followed_but_every_hop_is_checked(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin, resolver=resolve({"internal.test": ["127.0.0.1"]}))
    origin.routes["/go"] = lambda r: httpx.Response(302, headers={"location": "/hello-ep/1/mp3-v5/Ui6CBz2DJ_s/01%20Halyard.mp3"})
    origin.routes["/evil"] = lambda r: httpx.Response(302, headers={"location": "http://internal.test/secret"})
    origin.routes["/loop"] = lambda r: httpx.Response(302, headers={"location": "/loop"})
    ok = client.get(f"/remote/{token_for(cfg, 'https://shtl-experiment.com/go')}")
    assert ok.status_code == 200 and ok.content == MP3
    n = len(origin.requests)
    evil = client.get(f"/remote/{token_for(cfg, 'https://shtl-experiment.com/evil')}")
    assert evil.status_code == 403 and len(origin.requests) == n + 1          # the first hop was fetched, the internal one never
    n = len(origin.requests)
    assert client.get(f"/remote/{token_for(cfg, 'https://shtl-experiment.com/loop')}").status_code == 502
    assert len(origin.requests) - n == remote.MAX_REDIRECTS + 1                  # it gives up after the limit, not never


def test_an_origin_error_is_a_502_and_a_range_the_origin_refuses_is_passed_back(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    origin.routes["/gone.mp3"] = lambda r: httpx.Response(404)
    origin.routes["/big.mp3"] = lambda r: httpx.Response(416, headers={"content-range": "bytes */10"})
    assert client.get(f"/remote/{token_for(cfg, 'https://shtl-experiment.com/gone.mp3')}").status_code == 502
    assert client.get(f"/remote/{token_for(cfg, 'https://shtl-experiment.com/big.mp3')}", headers={"Range": "bytes=999-"}).status_code == 416


def test_control_with_the_address_check_removed_a_loopback_origin_is_fetched(tmp_path, origin, monkeypatch):
    system, monitor, client, cfg = make(tmp_path, origin, resolver=resolve({"shtl-experiment.com": ["127.0.0.1"]}))
    assert client.get(f"/remote/{token_for(cfg, AUDIO_URL)}").status_code == 403
    monkeypatch.setattr(remote, "is_public", lambda ip: True)
    assert client.get(f"/remote/{token_for(cfg, AUDIO_URL)}").content == MP3


def test_a_page_that_is_too_large_is_refused(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    origin.routes["/huge/"] = lambda r: httpx.Response(200, content=b"<p>" + b"x" * (remote.PAGE_LIMIT + 10))
    r = send(client, room=system.lounge.uid, url="https://shtl-experiment.com/huge/")
    assert r.status_code == 502 and "too large" in r.json()["error"]


# ---- the CLI ---------------------------------------------------------------------------------------------------------

def test_sonos_send_posts_the_url_for_the_default_room_or_dash_r(tmp_path, origin, monkeypatch, capsys):
    import threading
    import time as _t
    import uvicorn
    from sonos_cli import cli
    from test_cli import point_cli_at, Recorder
    system = FakeSystem().install(monkeypatch)
    cfg = config.Config(path=tmp_path / "service" / "config.toml")
    monitor = RoomMonitor(system.discover)
    monitor.refresh()
    fetcher = remote.Fetcher(resolve=resolve(), transport=httpx.MockTransport(origin.handler))
    app = Recorder(service.create_app(cfg, library=Library.create([]), monitor=monitor, fetcher=fetcher))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        _t.sleep(0.05)
    try:
        point_cli_at(tmp_path, monkeypatch, server.servers[0].sockets[0].getsockname()[1])
        assert cli.main(["send", RELEASE_URL]) == 0                       # the default room: Office
        assert "Playing 2 track(s) from Hello EP on Office." in capsys.readouterr().out
        assert [i.title for i in system.office.queue] == ["Halyard", "Undertow"]
        assert cli.main(["-r", "Kitchen", "send", "--append", RELEASE_URL]) == 0
        assert "Added 2 track(s)" in capsys.readouterr().out and system.kitchen.queue_size == 2
        bodies = [b for m, p, b in app.calls() if p == "/api/send"]
        assert [(b["url"], b["mode"]) for b in bodies] == [(RELEASE_URL, "replace"), (RELEASE_URL, "append")]
        assert cli.main(["send", "https://shtl-experiment.com/missing/"]) == 1
        assert "answered 404" in capsys.readouterr().err
    finally:
        server.should_exit = True
        t.join(5)


def test_sonos_send_without_the_service_explains_that_it_needs_it(capsys):
    from sonos_cli import cli
    assert cli.main(["send", RELEASE_URL]) == 1
    assert "needs the service running" in capsys.readouterr().err


def test_every_track_of_a_five_track_release_is_queued_as_a_remote_url_on_this_host_and_port(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    names = ["Halyard", "Undertow", "Slack Water", "Ebb", "Reprise"]
    html = "<title>Five</title>" + "".join(
        f'<div class="track"><a class="title" href="{n}/">{name}</a><audio><source type="audio/mpeg" src="{n}/mp3-v5/h{n}/0{n} {name}.mp3"></audio></div>'
        for n, name in enumerate(names, 1))
    r = send(client, room=system.lounge.uid, html=html, base_url="https://shtl-experiment.com/five/")
    assert r.status_code == 200, r.text
    uris = [i.resources[0].uri for i in system.lounge.queue]
    assert [i.title for i in system.lounge.queue] == names
    prefix = f"http://{local_ip_towards(system.lounge.ip_address)}:8737/remote/"
    assert len(uris) == 5 and all(u.startswith(prefix) for u in uris)
    assert [remote.verify(u.split("/remote/", 1)[1].split("/", 1)[0], remote.load_secret(cfg.path.parent / "remote-secret")) for u in uris][0] == \
        "https://shtl-experiment.com/five/1/mp3-v5/h1/01%20Halyard.mp3"


# ---- what the audit found -----------------------------------------------------------------------------------------------

def test_relative_links_resolve_against_the_page_after_its_redirects(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    origin.routes["/dir"] = lambda r: httpx.Response(301, headers={"location": "/dir/"})
    origin.routes["/dir/"] = lambda r: httpx.Response(200, text='<audio src="1/a.mp3"></audio>', headers={"content-type": "text/html"})
    r = send(client, room=system.lounge.uid, url="http://shtl-experiment.com/dir")
    assert r.status_code == 200 and [t["url"] for t in r.json()["tracks"]] == ["http://shtl-experiment.com/dir/1/a.mp3"]


def test_a_base_href_decides_where_relative_links_point():
    from sonos_cli.pages import extract
    html = '<base href="https://cdn.example.com/files/"><audio src="a.mp3"></audio>'
    assert [t.url for t in extract(html, "http://page.test/x/")[1]] == ["https://cdn.example.com/files/a.mp3"]


@pytest.mark.parametrize("url", ["http://shtl-experiment.com:99999/", "http://shtl-experiment.com:abc/", "http://[::1/", "http://", "https://exa mple.com/"])
def test_malformed_addresses_get_an_error_not_a_crash(tmp_path, origin, url):
    system, monitor, client, cfg = make(tmp_path, origin)
    assert client.post("/api/send/preview", json={"url": url}, headers=JSON).status_code in (400, 403, 502)
    assert client.get(f"/remote/{token_for(cfg, url)}").status_code in (400, 403, 502)
    assert origin.requests == []


def test_one_malformed_link_on_a_page_does_not_break_the_rest(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    html = '<a href="http://[zz/x.mp3">bad</a><a href="http://a.test:99999/y.mp3">bad port</a><a href="good.mp3">good</a>'
    r = send(client, room=system.lounge.uid, html=html, base_url="http://x.test/")
    assert r.status_code == 200 and [t["title"] for t in r.json()["tracks"]] == ["good"]
    assert send(client, room=system.lounge.uid, html=html, base_url="http://[::1").status_code == 400


def test_names_are_looked_up_off_the_event_loop(tmp_path, origin):
    import asyncio
    seen = []

    def resolver(host, port):
        try:
            asyncio.get_running_loop()
            seen.append("on the loop")
        except RuntimeError:
            seen.append("off the loop")
        return [PUBLIC]

    system, monitor, client, cfg = make(tmp_path, origin, resolver=resolver)
    assert client.get(f"/remote/{token_for(cfg, AUDIO_URL)}").status_code == 200
    assert send(client, room=system.lounge.uid, url=RELEASE_URL).status_code == 200
    assert seen and set(seen) == {"off the loop"}


def test_the_origin_is_asked_for_identity_encoding_for_pages_and_audio(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    client.get(f"/remote/{token_for(cfg, AUDIO_URL)}")
    client.post("/api/send/preview", json={"url": RELEASE_URL}, headers=JSON)
    assert [r["accept_encoding"] for r in origin.requests] == ["identity", "identity"]


class RealOrigin:
    """A real HTTP server on a socket (not a mock transport), so the proxy's streaming path runs for real."""

    def __init__(self):
        import gzip
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        outer = self
        self.seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append({"range": self.headers.get("Range"), "accept_encoding": self.headers.get("Accept-Encoding"), "host": self.headers.get("Host")})
                data, status, extra = MP3, 200, {}
                if self.headers.get("Range"):
                    start, _, end = self.headers["Range"].removeprefix("bytes=").partition("-")
                    start, end = int(start), int(end) if end else len(MP3) - 1
                    data, status, extra = MP3[start:end + 1], 206, {"Content-Range": f"bytes {start}-{end}/{len(MP3)}"}
                if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                    data, extra = gzip.compress(data), {**extra, "Content-Encoding": "gzip"}    # what a compressing origin would do
                self.send_response(status)
                for k, v in {"Content-Type": "audio/mpeg", "Content-Length": str(len(data)), **extra}.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_the_proxy_streams_a_real_origin_uncompressed_and_with_ranges(tmp_path, origin, monkeypatch):
    real = RealOrigin()
    try:
        monkeypatch.setattr(remote, "is_public", lambda ip: True)               # the one stand-in: a local origin counts as public
        fetcher = remote.Fetcher(resolve=lambda h, p: ["127.0.0.1"])             # no mock transport: real sockets, real streaming
        system, monitor, client, cfg = make(tmp_path, origin, fetcher=fetcher)
        t = token_for(cfg, f"http://audio.test:{real.port}/a.mp3")
        full = client.get(f"/remote/{t}")
        assert full.status_code == 200 and full.content == MP3 and "content-encoding" not in full.headers
        part = client.get(f"/remote/{t}", headers={"Range": "bytes=10-19"})
        assert part.status_code == 206 and part.content == MP3[10:20] and part.headers["content-range"] == f"bytes 10-19/{len(MP3)}"
        assert all(r["accept_encoding"] == "identity" and r["host"] == f"audio.test:{real.port}" for r in real.seen)
    finally:
        real.close()


@pytest.mark.parametrize("url", ["http://127.0.0.1:8737/api/rooms", "http://192.168.0.21:1400/xml/device_description.xml", "http://[::1]/x.mp3",
                                 "http://2130706433/x.mp3", "http://0x7f.1/x.mp3", "http://localhost/x.mp3", "http://user@127.0.0.1/x.mp3", "http://[::ffff:127.0.0.1]/x.mp3"])
def test_literal_loopback_and_lan_addresses_are_refused_with_the_real_resolver(tmp_path, origin, url):
    system, monitor, client, cfg = make(tmp_path, origin, fetcher=remote.Fetcher())      # the real resolver, no transport at all
    assert client.get(f"/remote/{token_for(cfg, url)}").status_code == 403
    assert client.post("/api/send/preview", json={"url": url}, headers=JSON).status_code == 403


def test_two_first_sends_at_once_make_one_secret(tmp_path, origin, monkeypatch):
    import threading
    system, monitor, client, cfg = make(tmp_path, origin)
    calls = []
    real_load = remote.load_secret

    def slow_load(path):
        calls.append(1)
        time.sleep(0.3)
        return real_load(path)

    monkeypatch.setattr(remote, "load_secret", slow_load)
    results = []
    html = '<a href="x.mp3">x</a>'

    def go():
        results.append(send(client, room=system.lounge.uid, html=html, base_url="http://x.test/", mode="append").status_code)

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [200, 200] and len(calls) == 1


def test_a_request_body_over_the_limit_is_refused_and_one_under_it_is_served(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    big = '{"room": "%s", "base_url": "http://x.test/", "html": "%s"}' % (system.lounge.uid, "x" * 13_000_000)
    r = client.post("/api/send", content=big, headers=JSON)                       # declared length
    assert r.status_code == 413 and system.lounge.queue == []
    chunks = (big[i:i + 1_000_000].encode() for i in range(0, len(big), 1_000_000))
    r = client.post("/api/send", content=chunks, headers=JSON)                    # no declared length (chunked): counted as it arrives
    assert r.status_code == 413 and system.lounge.queue == []
    ok = '{"room": "%s", "base_url": "http://x.test/", "html": "<a href=\\"x.mp3\\">x</a>%s"}' % (system.lounge.uid, "x" * 4_500_000)
    assert client.post("/api/send", content=ok, headers=JSON).status_code == 200


def test_a_page_that_trickles_in_for_too_long_is_given_up_on(monkeypatch):
    import asyncio
    import anyio

    async def slow(request):
        await anyio.sleep(5)
        return httpx.Response(200, text="<p/>")

    monkeypatch.setattr(remote, "PAGE_DEADLINE", 0.3)
    f = remote.Fetcher(resolve=resolve(), transport=httpx.MockTransport(slow))
    started = time.monotonic()
    with pytest.raises(remote.RemoteError, match="too long"):
        asyncio.run(f.get_text("https://shtl-experiment.com/slow"))
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("url", ["http://a..b/x.mp3", "http://" + "a" * 64 + ".com/x.mp3", "http://.example.com/x.mp3"])
def test_empty_and_over_long_host_labels_are_refused_with_the_real_resolver(tmp_path, origin, url):
    system, monitor, client, cfg = make(tmp_path, origin, fetcher=remote.Fetcher())
    assert client.get(f"/remote/{token_for(cfg, url)}").status_code == 403
    assert client.post("/api/send/preview", json={"url": url}, headers=JSON).status_code == 403


def test_an_international_host_name_is_sent_in_its_ascii_form(tmp_path, origin):
    system, monitor, client, cfg = make(tmp_path, origin)
    origin.routes["/a.mp3"] = lambda r: httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"})
    r = client.get(f"/remote/{token_for(cfg, 'https://bücher.example:8443/a.mp3')}")
    assert r.status_code == 200 and r.content == MP3
    req = origin.requests[-1]
    assert req["host"] == "xn--bcher-kva.example:8443" and req["sni"] == "xn--bcher-kva.example"


def test_a_compressed_origin_response_is_refused_not_passed_on_as_audio(tmp_path, origin):
    import gzip
    system, monitor, client, cfg = make(tmp_path, origin)
    origin.routes["/z.mp3"] = lambda r: httpx.Response(200, content=gzip.compress(MP3), headers={"content-type": "audio/mpeg", "content-encoding": "gzip"})
    r = client.get(f"/remote/{token_for(cfg, 'https://shtl-experiment.com/z.mp3')}")
    assert r.status_code == 502 and b"\x1f\x8b" not in r.content


def test_the_fake_origin_of_the_dev_service_serves_a_release_page_and_a_page_without_audio(tmp_path, origin):
    from sonos_cli.fakes import FAKE_EMPTY_URL, FAKE_RELEASE_URL, fake_fetcher
    system, monitor, client, cfg = make(tmp_path, origin, fetcher=fake_fetcher())
    ok = send(client, room=system.lounge.uid, url=FAKE_RELEASE_URL)
    assert ok.status_code == 200 and ok.json()["title"] == "Hello EP" and [i.title for i in system.lounge.queue] == ["Halyard", "Undertow"]
    empty = send(client, room=system.lounge.uid, url=FAKE_EMPTY_URL)
    assert empty.status_code == 422 and "No audio Sonos can play" in empty.json()["error"]
    uri = system.lounge.queue[0].resources[0].uri
    assert client.get("/remote/" + uri.split("/remote/", 1)[1]).content.startswith(b"ID3")        # and the proxy reaches it
    for private in ("http://127.0.0.1:8777/", "http://192.168.1.5/hello-ep/", "http://localhost/hello-ep/"):
        assert send(client, room=system.lounge.uid, url=private).status_code == 403              # not pretended: refused for real


def test_a_name_that_does_not_resolve_is_a_502_that_says_so_without_the_os_error_text(tmp_path, origin):
    import socket

    def nowhere(host, port):
        raise socket.gaierror(8, "nodename nor servname provided, or not known")

    system, monitor, client, cfg = make(tmp_path, origin, resolver=nowhere)
    r = send(client, room=system.lounge.uid, url="https://nowhere.example/")
    assert r.status_code == 502 and r.json()["error"] == "couldn't find nowhere.example: check the address"
    assert client.get(f"/remote/{token_for(cfg, 'https://nowhere.example/x.mp3')}").status_code == 502


def test_the_proxy_url_ends_in_the_tracks_title_so_the_speaker_titles_it_and_the_name_is_never_trusted(tmp_path, origin):
    from urllib.parse import unquote
    system, monitor, client, cfg = make(tmp_path, origin)
    html = '<a href="a.mp3">Halyard</a><a href="b.flac">Slack / Water ? 100% #1</a><a href="c.mp3">日本語 &amp; Ünï</a>'
    assert send(client, room=system.lounge.uid, html=html, base_url="http://x.test/").status_code == 200
    uris = [i.resources[0].uri for i in system.lounge.queue]
    names = [unquote(u.rsplit("/", 1)[1]) for u in uris]
    assert names == ["Halyard.mp3", "Slack Water 100 1.flac", "日本語 & Ünï.mp3"]
    assert all(u.count("/remote/") == 1 and "?" not in u and "#" not in u for u in uris)
    token = uris[0].split("/remote/", 1)[1].split("/", 1)[0]
    origin.routes["/a.mp3"] = lambda r: httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"})
    resolve_ok = client.get(f"/remote/{token}/whatever-name-here.mp3")
    assert resolve_ok.status_code == 200 and resolve_ok.content == MP3                  # served by the token alone ...
    assert origin.requests[-1]["url"].endswith("/a.mp3")                                # ... and the name never reached the origin
    assert client.get(f"/remote/{token}x/Halyard.mp3").status_code == 403               # the name can't stand in for a signature
    assert remote.display_name("", "audio/mpeg") == "track.mp3" and remote.display_name("x" * 300, "audio/ogg").endswith(".ogg")


def test_a_proxy_file_name_is_capped_in_encoded_bytes_and_keeps_an_emoji_family_together():
    long_emoji = remote.display_name("🎵" * 100, "audio/flac")
    long_cjk = remote.display_name("日本語" * 40, "audio/mpeg")
    assert len(long_emoji) <= 240 and len(long_cjk) <= 240 and long_emoji.endswith(".flac") and long_cjk.endswith(".mp3")
    assert len(remote.display_name("a" * 236, "audio/mpeg")) == 240 and len(remote.display_name("a" * 237, "audio/mpeg")) <= 240      # the edge itself
    from urllib.parse import unquote
    assert unquote(remote.display_name("👨\u200d👩\u200d👧 fam", "audio/mpeg")) == "👨\u200d👩\u200d👧 fam.mp3"
    assert unquote(remote.display_name(".mp3", "audio/mpeg")) == "track.mp3"


def test_a_huge_link_text_cannot_hold_a_worker_for_long():
    import time as _t
    began = _t.time()
    for title in ("🎵" * 20000, "a" * 200000, "日本語" * 30000):
        assert len(remote.display_name(title, "audio/mpeg")) <= 240
    assert _t.time() - began < 1.0


def test_a_page_with_an_artist_names_each_proxy_url_title_by_artist_and_the_response_carries_the_artist(tmp_path, origin):
    from urllib.parse import unquote
    system, monitor, client, cfg = make(tmp_path, origin)
    r = send(client, room=system.lounge.uid, url=RELEASE_URL)
    assert r.status_code == 200 and [t.get("artist") for t in r.json()["tracks"]] == ["Minim", "Minim"]
    names = [unquote(i.resources[0].uri.rsplit("/", 1)[1]) for i in system.lounge.queue]
    assert names == ["Halyard by Minim.mp3", "Undertow by Minim.mp3"]
    assert [i.title for i in system.lounge.queue] == ["Halyard", "Undertow"]                  # the DIDL title is still the plain title
    prev = client.post("/api/send/preview", json={"url": RELEASE_URL}, headers=JSON).json()
    assert [t.get("artist") for t in prev["tracks"]] == ["Minim", "Minim"]


def test_a_page_without_an_artist_keeps_the_plain_title_in_the_proxy_url_name(tmp_path, origin):
    from urllib.parse import unquote
    system, monitor, client, cfg = make(tmp_path, origin)
    assert send(client, room=system.lounge.uid, html='<a href="x.mp3">Plain</a>', base_url="http://x.test/").status_code == 200
    assert unquote(system.lounge.queue[0].resources[0].uri.rsplit("/", 1)[1]) == "Plain.mp3"
