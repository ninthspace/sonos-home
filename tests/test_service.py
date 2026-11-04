"""Service skeleton: health, stable guarded media URLs, and origin hardening (spec FR1, FR6, FR7, NFR6)."""

import socket
import threading
import time
import unicodedata
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from audio import make_tone
from sonos_cli import config, library, media, service
from sonos_cli.library import Library

BASE = "http://127.0.0.1:8737"
HOSTS = {"127.0.0.1", "localhost"}


@pytest.fixture
def world(tmp_path):
    """A music folder with real audio, plus the things the guard must keep out."""
    music = tmp_path / "music"
    make_tone(music / "a.wav")
    make_tone(music / "hires.wav", rate=96000, bits=24)
    make_tone(music / "c.mp3")
    outside = tmp_path / "outside"
    make_tone(outside / "y.wav")
    (tmp_path / "secret.txt").write_text("secret")
    make_tone(tmp_path / "music2" / "x.wav")
    (music / "link-to-outside").symlink_to(outside)
    cfg = config.Config(music_folders=(music.resolve(),), path=tmp_path / "config.toml")
    lib = Library.create([music], [tmp_path / "cache"])
    return type("World", (), dict(tmp=tmp_path, music=music.resolve(), cfg=cfg, lib=lib,
                                  rid=library.root_id(music.resolve())))


def client_for(world, **kw):
    app = service.create_app(world.cfg, library=world.lib, allowed_hosts=HOSTS, **kw)
    return TestClient(app, base_url=BASE)


@pytest.fixture
def client(world):
    return client_for(world)


# ---- FR1: health ---------------------------------------------------------------------------------

def run_server(app):
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=0, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, t, port


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("203.0.113.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def test_health_answers_from_loopback_and_from_the_lan_address(world):
    ip = lan_ip()
    if not ip or ip.startswith("127."):
        pytest.skip("no LAN address on this machine")
    app = service.create_app(world.cfg, library=world.lib, rooms_seen=lambda: 4)   # real default allowed hosts
    server, thread, port = run_server(app)
    try:
        for host in ("127.0.0.1", ip):
            body = urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=5).read()
            import json
            data = json.loads(body)
            assert data["rooms_seen"] == 4 and data["host"] == host and data["port"] == port != world.cfg.port
            assert data["ffmpeg"] in ("ok", "missing")
    finally:
        server.should_exit = True
        thread.join(5)


def test_health_reports_ffmpeg_missing_when_the_configured_path_does_not_exist(world):
    cfg = config.Config(music_folders=world.cfg.music_folders, ffmpeg="/nonexistent/ffmpeg", path=world.cfg.path)
    assert TestClient(service.create_app(cfg, library=world.lib, allowed_hosts=HOSTS), base_url=BASE) \
        .get("/api/health").json()["ffmpeg"] == "missing"


def test_health_reports_ffmpeg_ok_when_it_can_be_found(world, client):
    assert client.get("/api/health").json()["ffmpeg"] == "ok"      # control for the test above


# ---- media: types, ranges, conversion, stable URLs ---------------------------------------------------

def url(world, name):
    return world.lib.media_path("", world.music / name)


def test_media_serves_files_with_mime_accept_ranges_and_206(world, client):
    r = client.get(url(world, "a.wav"))
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.content == (world.music / "a.wav").read_bytes()
    part = client.get(url(world, "a.wav"), headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert part.headers["content-range"].startswith("bytes 0-99/")
    mp3 = client.get(url(world, "c.mp3"))
    assert mp3.headers["content-type"] == "audio/mpeg"
    assert client.head(url(world, "a.wav")).status_code == 200


def test_hires_file_is_converted_on_first_fetch_and_served_as_16bit_flac(world, client, tmp_path, monkeypatch):
    monkeypatch.setattr(media, "CACHE_DIR", tmp_path / "convcache")
    r = client.get(url(world, "hires.wav"))
    assert r.status_code == 200 and r.headers["content-type"] == "audio/flac"
    assert r.content[:4] == b"fLaC"
    served = tmp_path / "out.flac"
    served.write_bytes(r.content)
    info = media.probe(served)
    assert (info.codec, info.sample_rate, info.bits) == ("flac", 48000, 16)
    assert len(list((tmp_path / "convcache").glob("*.flac"))) == 1
    assert client.get(url(world, "hires.wav")).content == r.content        # cached, same bytes


def test_a_converted_files_url_ends_in_flac_and_serves_the_converted_flac(world, client, tmp_path, monkeypatch):
    monkeypatch.setattr(media, "CACHE_DIR", tmp_path / "convcache")
    flac_url = world.lib.media_path(BASE, world.music / "hires.wav", converted=True)
    assert flac_url.endswith("/hires.wav.flac") and world.lib.media_path(BASE, world.music / "hires.wav").endswith("/hires.wav")
    path = flac_url.split(BASE)[1]
    r = client.get(path)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/flac" and r.content[:4] == b"fLaC"
    assert client.get(url(world, "hires.wav")).content == r.content                  # the old URL still gives the same converted bytes
    assert client.get(url(world, "a.wav")).headers["content-type"] == "audio/wav"    # control: a plain WAV is still served as it is
    assert len(list((tmp_path / "convcache").glob("*.flac"))) == 1                   # converted once


def test_a_flac_suffixed_url_for_a_file_that_needs_no_conversion_or_does_not_exist_is_refused_and_converts_nothing(world, client, tmp_path, monkeypatch):
    monkeypatch.setattr(media, "CACHE_DIR", tmp_path / "convcache")
    make_tone(world.music / "master24.flac", rate=96000, bits=24)                    # a hi-res FLAC: converted, but already named .flac
    for name in ("a.wav.flac", "c.mp3.flac", "nope.wav.flac", "master24.flac.flac", "link-to-outside/y.wav.flac"):
        r = client.get(f"/media/{world.rid}/{name}")
        assert r.status_code in (403, 404), (name, r.status_code)
    for evil in ("..%2Fmusic2%2Fx.wav.flac", "%2E%2E/music2/x.wav.flac", "/etc/passwd.flac"):
        r = client.get(f"/media/{world.rid}/{evil}")
        assert r.status_code in (403, 404), (evil, r.status_code)
    assert not (tmp_path / "convcache").exists() or not list((tmp_path / "convcache").glob("*"))      # nothing was converted
    assert client.get(f"/media/{world.rid}/hires.wav.flac").status_code == 200                     # control: the real conversion URL works


def test_a_flac_suffixed_url_cannot_reach_a_file_outside_the_root_through_a_symlink_named_like_its_source(world, client, tmp_path, monkeypatch):
    monkeypatch.setattr(media, "CACHE_DIR", tmp_path / "convcache")
    make_tone(tmp_path / "outside" / "big.wav", rate=96000, bits=24)               # hi-res, outside the music root
    (world.music / "evil.wav").symlink_to(tmp_path / "outside" / "big.wav")
    for u in (f"/media/{world.rid}/evil.wav.flac", f"/media/{world.rid}/evil.wav"):
        assert client.get(u).status_code in (403, 404), u
    assert not (tmp_path / "convcache").exists() or not list((tmp_path / "convcache").glob("*"))      # nothing outside was converted
    assert client.get(f"/media/{world.rid}/hires.wav.flac").status_code == 200       # control: a real conversion inside the root works


def test_a_hi_res_flac_keeps_its_name_in_its_url_and_is_served_converted(world, client, tmp_path, monkeypatch):
    monkeypatch.setattr(media, "CACHE_DIR", tmp_path / "convcache")
    make_tone(world.music / "master24.flac", rate=96000, bits=24)
    u = world.lib.media_path(BASE, world.music / "master24.flac", converted=True)
    assert u.endswith("/master24.flac") and not u.endswith(".flac.flac")
    r = client.get(u.split(BASE)[1])
    assert r.status_code == 200 and r.headers["content-type"] == "audio/flac"
    served = tmp_path / "served.flac"
    served.write_bytes(r.content)
    info = media.probe(served)
    assert (info.codec, info.sample_rate, info.bits) == ("flac", 48000, 16)          # it was converted, not served as the 24-bit master


def test_a_real_file_named_like_a_conversion_url_is_served_as_itself(world, client, tmp_path, monkeypatch):
    monkeypatch.setattr(media, "CACHE_DIR", tmp_path / "convcache")
    make_tone(world.music / "hires.wav.flac")                                       # a real 16-bit FLAC beside hires.wav
    r = client.get(f"/media/{world.rid}/hires.wav.flac")
    assert r.status_code == 200 and r.content == (world.music / "hires.wav.flac").read_bytes()
    assert not (tmp_path / "convcache").exists() or not list((tmp_path / "convcache").glob("*"))      # it was not the converted one


def test_a_url_built_before_a_restart_serves_the_same_bytes_after_it(world, tmp_path):
    u = world.lib.media_path("http://10.0.0.5:8737", world.music / "a.wav")
    path = u.split("http://10.0.0.5:8737")[1]
    first = client_for(world).get(path).content
    restarted = Library.create(world.cfg.music_folders, [world.tmp / "cache"])      # new objects: no memory of the first
    app = service.create_app(world.cfg, library=restarted, allowed_hosts=HOSTS)
    assert TestClient(app, base_url=BASE).get(path).content == first == (world.music / "a.wav").read_bytes()


def test_unknown_and_missing_files(world, client):
    assert client.get(f"/media/nosuchroot/a.wav").status_code == 404
    assert client.get(f"/media/{world.rid}/nope.wav").status_code == 404
    assert client.get(f"/media/{world.rid}/").status_code == 404


# ---- FR6: the guard ---------------------------------------------------------------------------------

def refused_paths(world):
    return {
        "absolute /etc/hosts": f"/media/{world.rid}//etc/hosts",
        "dot-dot": f"/media/{world.rid}/..%2fsecret.txt",
        "double-encoded dot-dot": f"/media/{world.rid}/%252e%252e%252fsecret.txt",
        "symlink out of the root": f"/media/{world.rid}/link-to-outside/y.wav",
        "sibling-prefix": f"/media/{world.rid}/..%2fmusic2%2fx.wav",
    }


def test_paths_outside_the_music_folder_are_refused_with_403(world, client):
    for label, path in refused_paths(world).items():
        r = client.get(path)
        assert r.status_code == 403, label


def test_with_the_guard_removed_the_same_requests_are_served(world, monkeypatch):
    """Control: the refusals above come from the guard, not from the files being absent."""
    monkeypatch.setattr(Library, "guard", lambda self, c: Path(c).resolve())
    monkeypatch.setattr(library, "has_traversal", lambda rel: False)
    c = client_for(world)
    paths = refused_paths(world)
    for label in ("absolute /etc/hosts", "dot-dot", "symlink out of the root", "sibling-prefix"):
        r = c.get(paths[label])
        assert r.status_code in (200, 415), label        # served (415: /etc/hosts isn't audio, but it was read)
    assert c.get(paths["double-encoded dot-dot"]).status_code != 403   # reaches the filesystem (no such file)


def test_a_playlist_name_and_an_m3u_entry_outside_the_roots_are_refused_by_the_guard(world):
    lib = Library.create([world.music], [world.tmp / "playlists"])
    for bad in ("/etc/hosts", str(world.tmp / "playlists" / ".." / ".." / "x"), "\x00"):
        with pytest.raises(library.PathNotAllowed):
            lib.guard(bad)
    assert lib.guard(world.tmp / "playlists" / "ok.m3u8")


# ---- FR7: floor cases ----------------------------------------------------------------------------------

def test_a_music_root_that_is_a_symlink_serves_its_files(tmp_path):
    real = tmp_path / "CloudStorage-Dropbox"
    make_tone(real / "song.wav")
    link = tmp_path / "Dropbox"
    link.symlink_to(real)
    toml = tmp_path / "config.toml"
    toml.write_text(f'music_folders = ["{link}"]\n')
    cfg = config.load(toml)
    lib = Library.create(cfg.music_folders)
    app = service.create_app(cfg, library=lib, allowed_hosts=HOSTS)
    for requested in (link / "song.wav", real / "song.wav"):        # asked for through the symlink and directly
        u = lib.media_path("", requested)
        r = TestClient(app, base_url=BASE).get(u)
        assert r.status_code == 200 and r.content == (real / "song.wav").read_bytes()


def test_unicode_names_are_served_requested_in_nfc_and_in_nfd(world, client):
    name = "Café – Ünïcode 日本.wav"
    make_tone(world.music / unicodedata.normalize("NFC", name))
    expected = (world.music / unicodedata.normalize("NFC", name)).read_bytes()
    for form in ("NFC", "NFD"):
        r = client.get(f"/media/{world.rid}/{quote(unicodedata.normalize(form, name))}")
        assert r.status_code == 200 and r.content == expected, form


# ---- NFR6: hardening ------------------------------------------------------------------------------------

def echo_app():
    async def echo(request):
        return PlainTextResponse("accepted")

    async def ws(websocket):
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    return Starlette(routes=[Route("/echo", echo, methods=["GET", "POST"]), WebSocketRoute("/ws", ws)])


def hardened(**kw):
    return TestClient(service.Hardening(echo_app(), HOSTS, **kw), base_url=BASE)


JSON = {"content-type": "application/json", "origin": BASE}


def test_a_forged_host_is_refused(world):
    c = client_for(world)
    assert c.get("/api/health", headers={"host": "evil.example.com"}).status_code == 403
    assert c.get("/api/health", headers={"host": "evil.example.com:8737"}).status_code == 403
    assert c.get("/api/health").status_code == 200


def test_post_with_a_foreign_origin_is_refused_and_our_own_is_accepted():
    c = hardened()
    assert c.post("/echo", content=b"{}", headers={**JSON, "origin": "https://evil.example"}).status_code == 403
    assert c.post("/echo", content=b"{}", headers={"content-type": "application/json"}).status_code == 403   # no Origin
    assert c.post("/echo", content=b"{}", headers=JSON).status_code == 200


def test_text_plain_post_is_refused():
    c = hardened()
    assert c.post("/echo", content=b"x", headers={"content-type": "text/plain", "origin": BASE}).status_code == 403
    assert c.post("/echo", content=b"x", headers={"origin": BASE}).status_code == 403
    assert c.post("/echo", content=b"{}", headers={**JSON, "content-type": "application/json; charset=utf-8"}).status_code == 200


def test_websocket_upgrade_with_a_foreign_origin_is_refused():
    c = hardened()
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws", headers={"origin": "https://evil.example", "host": "127.0.0.1:8737"}):
            pass
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8737"}):   # no Origin at all
            pass
    with c.websocket_connect("/ws", headers={"origin": BASE, "host": "127.0.0.1:8737"}) as ws:
        assert ws.receive_text() == "hi"


def test_the_configured_extension_id_is_accepted_and_another_is_refused():
    c = hardened(extension_ids=("abcdefghijklmnopabcdefghijklmnop",))
    ok = {"content-type": "application/json", "origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop"}
    assert c.post("/echo", content=b"{}", headers=ok).status_code == 200
    bad = {**ok, "origin": "chrome-extension://ponmlkjihgfedcbaponmlkjihgfedcba"}
    assert c.post("/echo", content=b"{}", headers=bad).status_code == 403
    with c.websocket_connect("/ws", headers={"origin": ok["origin"], "host": "127.0.0.1:8737"}) as ws:
        assert ws.receive_text() == "hi"
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws", headers={"origin": bad["origin"], "host": "127.0.0.1:8737"}):
            pass


def test_media_get_without_an_origin_is_accepted(world, client):
    r = client.get(url(world, "a.wav"))             # a speaker: GET, no Origin
    assert r.status_code == 200 and "origin" not in r.request.headers


def test_with_the_checks_removed_the_refused_requests_are_accepted(world):
    """Control: every refusal above is the Hardening middleware's doing."""
    c = TestClient(echo_app(), base_url=BASE)
    assert c.post("/echo", content=b"x", headers={"content-type": "text/plain", "origin": "https://evil.example"}).status_code == 200
    assert c.get("/echo", headers={"host": "evil.example.com"}).status_code == 200
    with c.websocket_connect("/ws", headers={"origin": "https://evil.example", "host": "127.0.0.1:8737"}) as ws:
        assert ws.receive_text() == "hi"
    app = service.create_app(world.cfg, library=world.lib, hardening=False)
    assert TestClient(app, base_url=BASE).get("/api/health", headers={"host": "evil.example.com"}).status_code == 200


@pytest.mark.parametrize("rel", ["%00", "a%00b.wav", "x" * 400 + ".wav", "..", "%2e%2e", "link-to-outside", "."])
def test_odd_media_paths_get_a_clean_4xx_never_a_500(world, client, rel):
    r = client.get(f"/media/{world.rid}/{rel}")
    assert 400 <= r.status_code < 500, rel


def test_has_traversal_sees_dot_dot_through_any_number_of_encodings():
    for rel in ("../x", "a/../x", "..%2fx", "%2e%2e%2fx", "%252e%252e%252fx", "a\\..\\x"):
        assert library.has_traversal(rel), rel
    for rel in ("a/b.wav", "..a/b", "a..b/c", "Café.wav", "100%25.wav"):
        assert not library.has_traversal(rel), rel


def test_resolve_media_refuses_encoded_traversal_before_touching_the_filesystem(world):
    for rel in ("%2e%2e%2fsecret.txt", "%252e%252e%252fsecret.txt", "../secret.txt"):
        with pytest.raises(library.PathNotAllowed):
            world.lib.resolve_media(world.rid, rel)


def test_double_encoded_traversal_is_refused_by_a_real_server_too(world):
    import http.client
    app = service.create_app(world.cfg, library=world.lib, rooms_seen=lambda: 0)
    server, thread, port = run_server(app)
    try:
        for raw in (f"/media/{world.rid}/%252e%252e%252fsecret.txt", f"/media/{world.rid}/..%2fsecret.txt"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", raw)             # http.client sends the path exactly as given
            assert conn.getresponse().status == 403, raw
            conn.close()
    finally:
        server.should_exit = True
        thread.join(5)


def test_guard_alone_refuses_a_sibling_whose_name_starts_with_the_root_name(world):
    sibling = world.tmp / "music2" / "x.wav"
    assert sibling.exists() and str(sibling).startswith(str(world.music))
    with pytest.raises(library.PathNotAllowed):
        world.lib.guard(sibling)
    assert world.lib.guard(world.music / "a.wav")


def test_a_library_built_on_a_symlinked_root_serves_through_either_path(tmp_path):
    real = tmp_path / "real"
    make_tone(real / "s.wav")
    link = tmp_path / "link"
    link.symlink_to(real)
    lib = Library.create([link])
    assert lib.music_roots == (real.resolve(),)
    assert lib.guard(link / "s.wav") == (real / "s.wav").resolve()


def raw_request(port, lines):
    import socket as _s
    with _s.create_connection(("127.0.0.1", port), timeout=5) as c:
        c.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        data = b""
        while chunk := c.recv(4096):
            data += chunk
    return data.split(b"\r\n", 1)[0].decode(), data


def test_duplicate_host_or_origin_headers_are_refused_whichever_order_they_come_in(world):
    app = service.create_app(world.cfg, library=world.lib, rooms_seen=lambda: 0)
    server, thread, port = run_server(app)
    try:
        good = f"Host: 127.0.0.1:{port}"
        for lines in (["GET /api/health HTTP/1.1", "Host: evil.com", good, "Connection: close"],
                      ["GET /api/health HTTP/1.1", good, "Host: evil.com", "Connection: close"],
                      ["POST /api/health HTTP/1.1", good, f"Origin: http://127.0.0.1:{port}", "Origin: https://evil.example",
                       "Content-Type: application/json", "Content-Length: 0", "Connection: close"]):
            status, _ = raw_request(port, lines)
            assert " 403 " in status, lines
        status, _ = raw_request(port, ["GET /api/health HTTP/1.1", good, "Connection: close"])
        assert " 200 " in status                       # control: one Host header is fine
    finally:
        server.should_exit = True
        thread.join(5)


@pytest.mark.parametrize("host", ["localhost:abc", "127.0.0.1:", "127.0.0.1:80@evil.com", "[::1]evil", "127.0.0.1.evil.com",
                                  "evil.com#127.0.0.1", "localhost."])
def test_malformed_host_values_are_refused(world, host):
    assert client_for(world).get("/api/health", headers={"host": host}).status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8737", "LOCALHOST:8737", "localhost"])
def test_well_formed_allowed_hosts_are_accepted(world, host):
    assert client_for(world).get("/api/health", headers={"host": host}).status_code == 200


def test_an_address_the_mac_acquires_after_start_up_is_accepted_but_a_stranger_is_not():
    calls = []

    def addresses():
        calls.append(1)
        return {"10.1.2.3"}

    app = service.Hardening(echo_app(), {"127.0.0.1"}, refresh_hosts=addresses, refresh_every=0)
    c = TestClient(app, base_url=BASE)
    assert c.get("/echo", headers={"host": "10.1.2.3:8737"}).status_code == 200          # got its address late
    assert c.get("/echo", headers={"host": "10.9.9.9:8737"}).status_code == 403          # an address that isn't ours
    assert c.get("/echo", headers={"host": "evil.example:8737"}).status_code == 403      # names never trigger a refresh
    assert len(calls) == 2


def test_json_content_type_must_match_exactly_not_by_prefix():
    c = hardened()
    assert c.post("/echo", content=b"{}", headers={**JSON, "content-type": "application/json-x"}).status_code == 403
    assert c.post("/echo", content=b"{}", headers={**JSON, "content-type": "application/jsonp"}).status_code == 403


def test_the_sibling_prefix_path_is_refused_end_to_end_in_its_absolute_form(world, client):
    sibling = world.tmp / "music2" / "x.wav"
    assert client.get(f"/media/{world.rid}/{sibling}").status_code == 403


def test_media_failures_are_clean_responses_that_do_not_leak_paths(world, client, monkeypatch):
    bad = world.music / "notaudio.wav"
    bad.write_text("not audio")
    r = client.get(f"/media/{world.rid}/notaudio.wav")
    assert r.status_code == 415 and str(world.music) not in r.text
    monkeypatch.setattr(media, "probe", lambda p: (_ for _ in ()).throw(FileNotFoundError("ffprobe")))
    assert client_for(world).get(url(world, "c.mp3")).status_code == 503
    monkeypatch.undo()
    monkeypatch.setattr(media, "convert", lambda t: (_ for _ in ()).throw(RuntimeError("ffmpeg failed")))
    assert client_for(world).get(url(world, "hires.wav")).status_code == 502


# ---- the UI's static files ---------------------------------------------------------------------------------------

def test_the_ui_is_served_revalidated_and_does_not_shadow_the_api(world, client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>Sonos at home</title>" in r.text and r.headers["cache-control"] == "no-cache"
    js = client.get("/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"] and js.headers["cache-control"] == "no-cache"
    assert client.get("/style.css").status_code == 200
    assert client.get("/api/health").headers["content-type"].startswith("application/json")      # the API still answers first
    assert client.get(f"/media/{world.rid}/a.wav").headers["content-type"] == "audio/wav"


@pytest.mark.parametrize("path", ["/../service.py", "/%2e%2e/service.py", "/..%2fservice.py", "/web/app.js", "/launcher.c", "/nothing"])
def test_only_the_ui_files_are_reachable_through_the_static_mount(client, path):
    assert client.get(path).status_code == 404


def test_the_ui_files_are_served_to_the_embedded_cli_server_not_at_all(world):
    app = service.create_app(world.cfg, library=world.lib, allowed_hosts=HOSTS, media_only=True)
    c = TestClient(app, base_url=BASE)
    assert c.get("/").status_code == 404 and c.get("/app.js").status_code == 404


def test_health_says_ffmpeg_ok_with_nothing_installed_because_the_package_carries_one(world, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")                     # no Homebrew copy to find, on PATH or in its folders: only the bundled one is left
    monkeypatch.setattr(config, "FALLBACK_BIN_DIRS", ())
    assert config.find_binary("ffmpeg", None, "/nonexistent", ()) == config.managed_binary("ffmpeg")
    cfg = config.Config(music_folders=world.cfg.music_folders, path=world.cfg.path)
    body = TestClient(service.create_app(cfg, library=world.lib, allowed_hosts=HOSTS), base_url=BASE).get("/api/health").json()
    assert body["ffmpeg"] == "ok"


def test_health_says_ffmpeg_missing_when_the_bundled_copy_and_every_other_are_gone(world, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(config, "managed_binary", lambda name: None)
    monkeypatch.setattr(config, "FALLBACK_BIN_DIRS", ())
    cfg = config.Config(music_folders=world.cfg.music_folders, path=world.cfg.path)
    body = TestClient(service.create_app(cfg, library=world.lib, allowed_hosts=HOSTS), base_url=BASE).get("/api/health").json()
    assert body["ffmpeg"] == "missing"                             # control: the same app and the same cfg said "ok" with the bundled copy in place
