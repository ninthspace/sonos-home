"""The CLI as a thin client of the service, and its one narrow fallback (spec FR8, AD3)."""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import uvicorn

from audio import make_tone
from fakes import FakeSystem
from sonos_cli import cli, config, service
from sonos_cli.library import Library
from sonos_cli.rooms import RoomMonitor


class Recorder:
    """ASGI middleware that records (method, path, json body) of every API request."""

    def __init__(self, app):
        self.app, self.log = app, []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        chunks = []

        async def tee():
            msg = await receive()
            chunks.append(msg.get("body", b""))
            return msg

        self.log.append([scope["method"], scope["path"], chunks])
        await self.app(scope, tee, send)

    def calls(self):
        out = []
        for m, p, chunks in self.log:
            body = b"".join(chunks)
            out.append((m, p, json.loads(body) if body else None))
        return out


@pytest.fixture
def system(monkeypatch):
    return FakeSystem().install(monkeypatch)


@pytest.fixture
def music(tmp_path):
    m = tmp_path / "music"
    make_tone(m / "a.wav")
    make_tone(m / "b.wav")
    return m.resolve()


def point_cli_at(tmp_path, monkeypatch, port, folders=()):
    d = tmp_path / "cfg"
    d.mkdir(exist_ok=True)
    folder_line = "music_folders = [" + ", ".join(f'"{f}"' for f in folders) + "]\n"
    (d / "config.toml").write_text(f"port = {port}\n{folder_line}")
    monkeypatch.setenv("SONOS_CLI_CONFIG_DIR", str(d))
    config._cached = None


@pytest.fixture
def running(system, music, tmp_path, monkeypatch):
    """A real service (uvicorn, port 0) over the fake rooms; the CLI's config points at it."""
    cfg = config.Config(music_folders=(music,), path=tmp_path / "service" / "config.toml")
    monitor = RoomMonitor(system.discover)
    monitor.refresh()
    app = Recorder(service.create_app(cfg, library=Library.create([music]), monitor=monitor))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    point_cli_at(tmp_path, monkeypatch, port, [music])
    yield type("R", (), dict(app=app, port=port, monitor=monitor, system=system))
    server.should_exit = True
    t.join(5)


def run_cli(*argv):
    return cli.main(list(argv))


# ---- every command calls its endpoint when the service is up -----------------------------------------------

def test_each_command_calls_the_matching_endpoint(running, music, capsys):
    s = running.system
    uid = s.lounge.uid
    assert run_cli("-r", "Lounge", "play", str(music / "a.wav")) == 0
    assert s.lounge.transport_state == "PLAYING"
    for command, action in (("pause", "pause"), ("resume", "resume"), ("stop", "stop")):
        assert run_cli("-r", "lounge", command) == 0, command
    assert run_cli("-r", "Lounge", "volume", "25") == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "25" and s.lounge.group.volume == 25
    assert run_cli("-r", "Lounge", "volume") == 0
    assert capsys.readouterr().out.strip() == "25"
    assert run_cli("-r", "Lounge", "status") == 0
    out = capsys.readouterr().out
    assert "Lounge" in out and "STOPPED" in out and "volume 25" in out
    assert run_cli("rooms") == 0
    assert "Kitchen" in capsys.readouterr().out
    calls = running.app.calls()
    assert ("POST", "/api/play") in [(m, p) for m, p, _ in calls]
    assert [b["action"] for m, p, b in calls if p == f"/api/rooms/{uid}/transport"] == ["pause", "resume", "stop"]
    assert [b for m, p, b in calls if p == f"/api/rooms/{uid}/volume"] == [{"level": 25}]
    assert ("GET", f"/api/rooms/{uid}/state") in [(m, p) for m, p, _ in calls]
    assert [c for c in calls if c[1] == "/api/rooms"]                     # name -> UID, and `sonos rooms`
    assert [c[0] for c in s.lounge.calls if c[0] in ("pause", "play", "stop")] == ["pause", "play", "stop"]


def test_next_and_prev_go_through_the_service(running, music):
    s = running.system
    assert run_cli("-r", "Lounge", "play", str(music)) == 0
    running.monitor.refresh()
    for _ in range(100):
        if s.lounge.queue_size == 2:
            break
        time.sleep(0.05)
    assert run_cli("-r", "Lounge", "next") == 0
    assert s.lounge.queue_index == 1
    assert run_cli("-r", "Lounge", "prev") == 0
    assert s.lounge.queue_index == 0
    transports = [b["action"] for m, p, b in running.app.calls() if p.endswith("/transport")]
    assert transports == ["next", "prev"]


def test_play_sends_absolute_paths(running, music, monkeypatch):
    monkeypatch.chdir(music)
    assert run_cli("-r", "Lounge", "play", "a.wav", "./b.wav") == 0
    body = next(b for m, p, b in running.app.calls() if p == "/api/play")
    assert body["paths"] == [str(music / "a.wav"), str(music / "b.wav")]
    assert all(Path(p).is_absolute() for p in body["paths"]) and body["mode"] == "replace"
    assert run_cli("-r", "Lounge", "play", "--append", "a.wav") == 0
    assert [b["mode"] for m, p, b in running.app.calls() if p == "/api/play"] == ["replace", "append"]


def test_an_unknown_room_lists_the_available_ones(running, capsys):
    assert run_cli("-r", "Garage", "pause") == 1
    err = capsys.readouterr().err
    assert "Garage" in err and "Kitchen" in err and "Lounge" in err


# ---- the outside-roots hint -------------------------------------------------------------------------------------

def test_playing_a_path_outside_the_music_folders_exits_non_zero_naming_config_toml(running, tmp_path, capsys):
    outside = make_tone(tmp_path / "elsewhere" / "x.wav")
    assert run_cli("-r", "Lounge", "play", str(outside)) == 1
    err = capsys.readouterr().err
    assert "config.toml" in err and "music_folders" in err and running.system.lounge.queue_size == 0


# ---- a service that errors is reported, not papered over --------------------------------------------------------

@pytest.fixture
def broken_service(system, tmp_path, monkeypatch):
    seen = []

    class H(BaseHTTPRequestHandler):
        def _go(self):
            seen.append((self.command, self.path))
            body = json.dumps({"error": "the service exploded"}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _go

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    point_cli_at(tmp_path, monkeypatch, srv.server_address[1])
    yield seen
    srv.shutdown()


@pytest.mark.parametrize("argv", [("pause",), ("rooms",), ("volume", "30"), ("status",), ("play", "/tmp/x.wav")])
def test_a_500_from_the_service_is_shown_and_neither_fallback_runs(system, broken_service, capsys, argv):
    assert run_cli("--ip", "192.168.0.26", "-r", "Lounge", *argv) == 1
    assert "the service exploded" in capsys.readouterr().err
    assert broken_service and all(system.by_ip(ip).calls == [] for ip in ("192.168.0.26", "192.168.0.21"))


# ---- connection refused: the fallbacks ----------------------------------------------------------------------------

def test_with_the_connection_refused_pause_calls_soco_directly(system, capsys):
    assert run_cli("--ip", "192.168.0.26", "pause") == 0
    assert ("pause",) in system.lounge.calls
    assert run_cli("--ip", "192.168.0.26", "volume", "33") == 0
    assert system.lounge.group.volume == 33
    assert run_cli("--ip", "192.168.0.26", "status") == 0
    assert run_cli("rooms") == 0
    assert "Kitchen" in capsys.readouterr().out


def test_with_the_connection_refused_play_runs_in_process_with_stable_urls(system, music, monkeypatch, capsys):
    served = {}

    def fake_serve(coordinator, our_prefix, count, idle):          # the speaker's turn: fetch the first URL, then end
        url = coordinator.queue[0].resources[0].uri
        assert url.startswith(our_prefix)
        served["url"] = url
        served["bytes"] = urllib.request.urlopen(url, timeout=10).read()

    monkeypatch.setattr(cli, "_serve_until_done", fake_serve)
    import types
    monkeypatch.setattr(cli, "subprocess", types.SimpleNamespace(
        Popen=lambda *a, **k: types.SimpleNamespace(terminate=lambda: None)))        # no real caffeinate in a test
    assert run_cli("--ip", "192.168.0.26", "play", str(music / "a.wav")) == 0
    assert served["bytes"] == (music / "a.wav").read_bytes()
    lib = Library.create([music])
    assert served["url"].split(":", 2)[2].split("/", 1)[1] == lib.media_path("", music / "a.wav").lstrip("/")
    assert system.lounge.transport_state == "PLAYING"


def test_the_in_process_play_refuses_a_missing_path(system, tmp_path, capsys):
    assert run_cli("--ip", "192.168.0.26", "play", str(tmp_path / "nope")) == 1
    assert "No such file or folder" in capsys.readouterr().err


def test_an_in_process_append_is_refused_because_nothing_would_keep_serving_the_files(system, music, capsys):
    assert run_cli("--ip", "192.168.0.26", "play", "--append", str(music)) == 1
    err = capsys.readouterr().err
    assert "--append needs the service" in err and "sonos service" in err
    assert system.lounge.queue_size == 0 and system.lounge.calls == []


def test_in_process_play_of_a_symlinked_file_works_as_the_old_cli_did(system, music, tmp_path, monkeypatch):
    import types
    served = {}
    monkeypatch.setattr(cli, "_serve_until_done", lambda c, prefix, n, idle: served.update(
        data=urllib.request.urlopen(c.queue[0].resources[0].uri, timeout=10).read()))
    monkeypatch.setattr(cli, "subprocess", types.SimpleNamespace(Popen=lambda *a, **k: types.SimpleNamespace(terminate=lambda: None)))
    links = tmp_path / "links"
    links.mkdir()
    (links / "x.wav").symlink_to(music / "a.wav")
    assert run_cli("--ip", "192.168.0.26", "play", str(links / "x.wav")) == 0
    assert served["data"] == (music / "a.wav").read_bytes() and system.lounge.queue_size == 1


def test_the_in_process_message_does_not_claim_a_service_is_serving(system, music, monkeypatch, capsys):
    import types
    monkeypatch.setattr(cli, "_serve_until_done", lambda *a: None)
    monkeypatch.setattr(cli, "subprocess", types.SimpleNamespace(Popen=lambda *a, **k: types.SimpleNamespace(terminate=lambda: None)))
    assert run_cli("--ip", "192.168.0.26", "play", str(music / "a.wav")) == 0
    out = capsys.readouterr().out
    assert "service keeps serving" not in out and "from this command" in out


def test_ip_picks_the_room_through_the_service_including_a_stereo_partners_address(running, music):
    s = running.system
    assert run_cli("--ip", "192.168.0.26", "pause") == 0                    # Lounge, though the default room is Office
    assert ("pause",) in s.lounge.calls and ("pause",) not in s.office.calls
    assert run_cli("--ip", "192.168.0.27", "stop") == 0                     # Office's invisible partner -> Office
    assert ("stop",) in s.office.calls
    assert run_cli("--ip", "10.9.9.9", "pause") == 1


def test_the_default_room_comes_from_config_when_neither_flag_nor_env_say(running, tmp_path, monkeypatch):
    d = tmp_path / "cfg"
    (d / "config.toml").write_text((d / "config.toml").read_text() + 'room = "Kitchen"\n')
    config._cached = None
    assert run_cli("pause") == 0
    assert ("pause",) in running.system.kitchen.calls and ("pause",) not in running.system.office.calls


def test_a_port_that_is_in_use_is_a_clean_error(system, music, capsys):
    import socket
    with socket.socket() as busy:
        busy.bind(("0.0.0.0", 0))
        busy.listen()
        port = busy.getsockname()[1]
        t0 = time.monotonic()
        assert run_cli("--ip", "192.168.0.26", "play", "-p", str(port), str(music / "a.wav")) == 1
        assert time.monotonic() - t0 < 4, "it should notice the dead server thread, not wait out the timeout"
    assert "already in use" in capsys.readouterr().err


def test_the_embedded_server_answers_to_the_ip_the_speaker_uses(music):
    from sonos_cli.server import EmbeddedServer
    from sonos_cli.library import Library
    server = EmbeddedServer(Library.create([music]), config.get(), "10.99.99.99")
    server.start()
    try:
        for host, expect in (("10.99.99.99", 200), ("evil.example.com", 403)):
            req = urllib.request.Request(f"http://127.0.0.1:{server.port}" + Library.create([music]).media_path("", music / "a.wav"),
                                         headers={"Host": host})
            try:
                status = urllib.request.urlopen(req, timeout=10).status
            except urllib.error.HTTPError as e:
                status = e.code
            assert status == expect, host
    finally:
        server.stop()


# ---- flags beat the environment; the printed room is the room used ------------------------------------------------

def test_an_explicit_room_beats_sonos_ip_from_the_environment(running, monkeypatch):
    monkeypatch.setenv("SONOS_IP", "192.168.0.26")                        # Lounge
    assert run_cli("-r", "Kitchen", "pause") == 0
    assert ("pause",) in running.system.kitchen.calls and ("pause",) not in running.system.lounge.calls
    assert run_cli("pause") == 0                                            # no -r: the env IP applies
    assert ("pause",) in running.system.lounge.calls


def test_sonos_room_fills_in_a_missing_room_but_an_explicit_ip_still_wins(running, monkeypatch):
    monkeypatch.setenv("SONOS_ROOM", "Kitchen")
    assert run_cli("pause") == 0 and ("pause",) in running.system.kitchen.calls
    assert run_cli("--ip", "192.168.0.26", "pause") == 0 and ("pause",) in running.system.lounge.calls


def test_an_ip_that_belongs_to_another_room_than_the_named_one_is_refused(running, capsys):
    assert run_cli("--ip", "192.168.0.26", "-r", "Kitchen", "pause") == 1
    assert "belongs to Lounge, not Kitchen" in capsys.readouterr().err
    assert all(("pause",) not in d.calls for d in running.system.devices)
    assert run_cli("--ip", "192.168.0.27", "-r", "office", "pause") == 0          # a partner's address, right room


def test_the_direct_path_refuses_a_mismatched_ip_and_room_too(system, capsys):
    assert run_cli("--ip", "192.168.0.26", "-r", "Kitchen", "pause") == 1
    assert "belongs to Lounge, not Kitchen" in capsys.readouterr().err and system.lounge.calls == []


def test_the_play_message_names_the_room_actually_used(running, music, capsys):
    assert run_cli("--ip", "192.168.0.26", "play", str(music / "a.wav")) == 0
    assert "on Lounge;" in capsys.readouterr().out
    assert run_cli("-r", "kitchen", "play", str(music / "a.wav")) == 0
    assert "on Kitchen;" in capsys.readouterr().out


def test_the_embedded_server_serves_only_the_files_being_played_not_the_api(music):
    from sonos_cli.server import EmbeddedServer
    server = EmbeddedServer(Library.create([music]), config.get(), "127.0.0.1")
    server.start()
    try:
        def status(path):
            try:
                return urllib.request.urlopen(f"http://127.0.0.1:{server.port}{path}", timeout=10).status
            except urllib.error.HTTPError as e:
                return e.code
        assert status(Library.create([music]).media_path("", music / "a.wav")) == 200
        for path in ("/api/browse", f"/api/browse?path={music}", "/api/health", "/api/rooms"):
            assert status(path) == 404, path
    finally:
        server.stop()
