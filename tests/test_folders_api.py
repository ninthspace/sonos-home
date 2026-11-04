"""Music folders from the Music view: /api/folders, add, remove, browse (spec FR23, FR6, NFR6; epic 01-10 Story 8)."""

import os
import stat
import threading
import unicodedata
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from audio import make_tone
from fakes import FakeSystem
from sonos_cli import config, playback, service
from sonos_cli.library import Library, LiveLibrary
from sonos_cli.playback import Player
from sonos_cli.rooms import RoomMonitor
from test_play_api import post_play
from test_playback import FAST_INFO, tiny_wav

BASE = "http://127.0.0.1:8737"
JSON = {"Content-Type": "application/json", "Origin": BASE}
MAC = "10.1.2.3"                                        # the address the connection comes in on, when a test says the Mac is opened on its LAN address


def make(tmp_path, monkeypatch, peer=("127.0.0.1", 50000), folders=(), text=None, server="127.0.0.1"):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    cfgp = tmp_path / "cfg" / "config.toml"
    cfgp.parent.mkdir(exist_ok=True)
    cfgp.write_text(text if text is not None else "".join(f'music_folders = ["{f}"]\n' for f in folders[:1]) if folders else (text or ""))
    cfg = config.load(cfgp)
    lib = Library.create(cfg.music_folders, [tmp_path / "cache"])
    system = FakeSystem()
    monitor = RoomMonitor(system.discover)
    monitor.refresh()
    app = service.create_app(cfg, library=lib, allowed_hosts={"127.0.0.1", MAC}, monitor=monitor, port=8737)
    return system, cfgp, TestClient(app, base_url=f"http://{server}:8737", client=peer), home


def post(client, path, body, headers=None):
    own = {"Content-Type": "application/json", "Origin": str(client.base_url).rstrip("/")}      # the service's own origin, whichever address it was opened on
    return client.post(path, json=body, headers=headers or own)


def folder(tmp_path, name="m"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


# ---- who is "this Mac" ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("peer,server,editable", [
    ("127.0.0.1", "127.0.0.1", True), ("::1", "127.0.0.1", True), ("::ffff:127.0.0.1", "127.0.0.1", True),
    (MAC, MAC, True), (f"::ffff:{MAC}", MAC, True),                                          # the Mac opened through its own LAN address
    ("192.168.0.50", MAC, False), ("::ffff:192.168.0.50", MAC, False), (MAC, "127.0.0.1", False),   # another device; an address that is not the connection's own
    ("testclient", MAC, False), ("not an address", "127.0.0.1", False), ("fe80::1%en0", MAC, False), ("0.0.0.0", MAC, False)])
def test_editable_is_true_only_for_this_mac_judged_from_the_peer_address_and_the_address_it_came_in_on(tmp_path, monkeypatch, peer, server, editable):
    system, cfgp, client, home = make(tmp_path, monkeypatch, peer=(peer, 50000), server=server)
    r = client.get("/api/folders")
    assert r.status_code == 200 and r.json()["editable"] is editable                  # listing is open to any allowed client


def test_a_missing_peer_counts_as_another_device(tmp_path, monkeypatch):
    system, cfgp, client, home = make(tmp_path, monkeypatch)
    client = TestClient(client.app, base_url=BASE, client=None)
    assert client.get("/api/folders").json()["editable"] is False


def test_the_folder_list_gives_real_paths_and_whether_each_exists_now(tmp_path, monkeypatch):
    here, gone = folder(tmp_path, "here"), folder(tmp_path, "gone")
    link = tmp_path / "link"
    link.symlink_to(here)
    system, cfgp, client, home = make(tmp_path, monkeypatch)
    cfgp.write_text(f'music_folders = ["{link}", "{gone}"]\n')
    client = TestClient(service.create_app(config.load(cfgp), library=Library.create(config.load(cfgp).music_folders), allowed_hosts={"127.0.0.1"}),
                        base_url=BASE, client=("127.0.0.1", 1))
    gone.rmdir()
    items = client.get("/api/folders").json()["folders"]
    assert [(i["path"], i["exists"]) for i in items] == [(str(here), True), (str(gone), False)]


def test_run_starts_uvicorn_without_trusting_proxy_headers(tmp_path, monkeypatch):
    import uvicorn
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    assert service.run(config.Config(path=tmp_path / "config.toml"), port=8799, fake_rooms=True) == 0
    assert seen["proxy_headers"] is False


# ---- another device: refused; this Mac: the same requests work -------------------------------------------------------

def three_requests(client, new_dir, existing):
    return [post(client, "/api/folders/add", {"path": str(new_dir)}),
            post(client, "/api/folders/remove", {"path": str(existing)}),
            client.get("/api/folders/browse")]


def test_add_remove_and_browse_are_refused_403_from_another_device_and_succeed_from_this_mac_in_the_same_fixture(tmp_path, monkeypatch):
    a, b = folder(tmp_path, "a"), folder(tmp_path, "b")
    for peer, server in (("192.168.0.50", MAC), ("testclient", MAC), (MAC, "127.0.0.1")):
        system, cfgp, client, home = make(tmp_path, monkeypatch, peer=(peer, 50000), server=server, text=f'music_folders = ["{a}"]\nport = 9000\n')
        before = cfgp.read_bytes()
        assert [r.status_code for r in three_requests(client, b, a)] == [403, 403, 403]
        assert cfgp.read_bytes() == before                                                 # the file is byte-identical
    for peer, server in (("127.0.0.1", "127.0.0.1"), ("::1", "127.0.0.1"), (MAC, MAC)):     # control and floor: each way of being this Mac
        system, cfgp, client, home = make(tmp_path, monkeypatch, peer=(peer, 50000), server=server, text=f'music_folders = ["{a}"]\nport = 9000\n')
        added, removed, browsed = three_requests(client, b, a)
        assert (added.status_code, removed.status_code, browsed.status_code) == (200, 200, 200), peer


def test_headers_naming_loopback_do_not_make_another_device_this_mac(tmp_path, monkeypatch):
    a, b = folder(tmp_path, "a"), folder(tmp_path, "b")
    system, cfgp, client, home = make(tmp_path, monkeypatch, peer=("192.168.0.50", 50000), text=f'music_folders = ["{a}"]\n')
    before = cfgp.read_bytes()
    spoof = {**JSON, "X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1", "Forwarded": "for=127.0.0.1", "Host": "127.0.0.1"}
    assert post(client, "/api/folders/add", {"path": str(b)}, spoof).status_code == 403
    assert client.get("/api/folders/browse", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
    assert client.get("/api/folders", headers={"X-Forwarded-For": "127.0.0.1"}).json()["editable"] is False
    assert cfgp.read_bytes() == before
    system, cfgp, client, home = make(tmp_path, monkeypatch, peer=("127.0.0.1", 50000), text=f'music_folders = ["{a}"]\n')       # control
    assert post(client, "/api/folders/add", {"path": str(b)}).status_code == 200


def test_the_nfr6_checks_apply_to_the_folder_routes_from_this_mac(tmp_path, monkeypatch):
    a, b = folder(tmp_path, "a"), folder(tmp_path, "b")
    system, cfgp, client, home = make(tmp_path, monkeypatch, text=f'music_folders = ["{a}"]\n')
    before = cfgp.read_bytes()
    for route, path in (("add", b), ("remove", a)):
        url = f"/api/folders/{route}"
        assert post(client, url, {"path": str(path)}, {**JSON, "Origin": "http://evil.test"}).status_code == 403
        assert client.post(url, content=f'{{"path": "{path}"}}', headers={"Content-Type": "text/plain", "Origin": BASE}).status_code == 403
    assert cfgp.read_bytes() == before
    assert post(client, "/api/folders/add", {"path": str(b)}).status_code == 200            # control: own Origin and JSON


# ---- add -------------------------------------------------------------------------------------------------------------

def test_an_add_changes_the_file_and_the_library_at_once_and_its_files_play(tmp_path, monkeypatch):
    system, cfgp, client, home = make(tmp_path, monkeypatch)
    cfgp.write_text("port = 8737\n")
    music = folder(tmp_path, "my music")
    make_tone(music / "a.wav")
    assert client.get("/api/browse").status_code == 409                                     # no folders yet
    r = post(client, "/api/folders/add", {"path": str(music)})
    assert r.status_code == 200
    assert [f["path"] for f in r.json()["folders"]] == [str(music)]
    assert f'"{music}"' in cfgp.read_text() and cfgp.read_text().startswith("port = 8737\n")
    assert [e["name"] for e in client.get("/api/browse").json()["entries"]] == ["my music"]      # listed at once, no restart
    assert post_play(client, room=system.lounge.uid, paths=[str(music / "a.wav")]).status_code == 200
    url = None
    for _ in range(50):
        if system.lounge.queue:
            url = system.lounge.queue[0].resources[0].uri.split("://", 1)[1].split("/", 1)[1]
            break
        threading.Event().wait(0.05)
    assert url and client.get("/" + url).status_code == 200                                  # served under the new root


def test_a_tilde_path_a_symlink_and_an_nfd_name_with_spaces_are_accepted_and_stored_as_real_paths(tmp_path, monkeypatch):
    system, cfgp, client, home = make(tmp_path, monkeypatch, text="")
    (home / "Dropbox" / "Music").mkdir(parents=True)
    nfd = unicodedata.normalize("NFD", "Café Ünïcode 日本 folder")
    odd = folder(tmp_path, nfd)
    real_target = folder(tmp_path, "real target")
    link = tmp_path / "link to target"
    link.symlink_to(real_target)
    for raw, stored in ((f"~/Dropbox/Music", (home / "Dropbox" / "Music").resolve()), (str(link), real_target), (str(odd), odd)):
        r = post(client, "/api/folders/add", {"path": raw})
        assert r.status_code == 200, (raw, r.text)
        assert str(stored) in [f["path"] for f in r.json()["folders"]]
    assert set(config.load(cfgp).music_folders) == {(home / "Dropbox" / "Music").resolve(), real_target, odd}


@pytest.mark.parametrize("body,status", [
    ({}, 400), ({"path": ""}, 400), ({"path": "   "}, 400), ({"path": None}, 400), ({"path": 5}, 400), ({"path": ["/a"]}, 400),
    ({"path": "relative/folder"}, 400), ({"path": "a\x00b"}, 400)])
def test_a_bad_path_is_refused_with_400_and_the_file_is_unchanged(tmp_path, monkeypatch, body, status):
    system, cfgp, client, home = make(tmp_path, monkeypatch, text='# mine\nport = 1\n')
    before = cfgp.read_bytes()
    assert post(client, "/api/folders/add", body).status_code == status
    assert cfgp.read_bytes() == before
    assert post(client, "/api/folders/add", {"path": str(folder(tmp_path, "ok"))}).status_code == 200                           # control


def test_a_missing_path_a_file_and_an_unreadable_folder_are_422(tmp_path, monkeypatch):
    system, cfgp, client, home = make(tmp_path, monkeypatch, text='port = 1\n')
    before = cfgp.read_bytes()
    a_file = tmp_path / "f.txt"
    a_file.write_text("x")
    locked = folder(tmp_path, "locked")
    os.chmod(locked, 0)
    try:
        for p, reason in ((tmp_path / "nope", "doesn't exist"), (a_file, "is not a folder"), (locked, "can't be read")):
            r = post(client, "/api/folders/add", {"path": str(p)})
            assert r.status_code == 422 and reason in r.json()["error"], (p, r.text)         # each refused for its own reason
    finally:
        os.chmod(locked, 0o755)
    assert cfgp.read_bytes() == before
    assert post(client, "/api/folders/add", {"path": str(folder(tmp_path, "ok"))}).status_code == 200


def test_a_duplicate_in_any_spelling_and_a_folder_inside_or_around_a_configured_one_are_409(tmp_path, monkeypatch):
    a = folder(tmp_path, "a")
    inner = folder(tmp_path, "a/inner")
    alias = tmp_path / "alias"
    alias.symlink_to(a)
    outer = folder(tmp_path, "outer")
    system, cfgp, client, home = make(tmp_path, monkeypatch, text=f'music_folders = ["{a}", "{outer}/sub"]\n')
    (outer / "sub").mkdir()
    before = cfgp.read_bytes()
    for p, reason in ((a, "already a music folder"), (alias, "already a music folder"), (inner, "overlaps"), (outer, "overlaps")):
        r = post(client, "/api/folders/add", {"path": str(p)})
        assert r.status_code == 409 and reason in r.json()["error"], (p, r.text)
    assert cfgp.read_bytes() == before
    assert post(client, "/api/folders/add", {"path": str(folder(tmp_path, "unrelated"))}).status_code == 200                   # control


def test_an_unparsable_config_is_409_naming_the_file_and_is_not_overwritten(tmp_path, monkeypatch):
    system, cfgp, client, home = make(tmp_path, monkeypatch, text="port = 1\n")
    cfgp.write_text('music_folders = [\nport = = 1\n')
    before = cfgp.read_bytes()
    r = post(client, "/api/folders/add", {"path": str(folder(tmp_path, "ok"))})
    assert r.status_code == 409 and str(cfgp) in r.json()["error"] and cfgp.read_bytes() == before


def test_a_missing_config_file_is_created_by_an_add(tmp_path, monkeypatch):
    system, cfgp, client, home = make(tmp_path, monkeypatch, text="")
    cfgp.unlink()
    r = post(client, "/api/folders/add", {"path": str(folder(tmp_path, "ok"))})
    assert r.status_code == 200 and config.load(cfgp).music_folders == (folder(tmp_path, "ok"),)


# ---- remove ----------------------------------------------------------------------------------------------------------

def snapshot(root: Path):
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            p = Path(dirpath) / name
            st = p.lstat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, stat.S_IMODE(st.st_mode))
    return out


def test_remove_drops_the_folder_everywhere_changes_nothing_else_and_touches_no_file(tmp_path, monkeypatch):
    a, b = folder(tmp_path, "a"), folder(tmp_path, "b")
    make_tone(a / "x.wav")
    (a / ".hidden").write_text("h")
    (a / "sub").mkdir()
    system, cfgp, client, home = make(tmp_path, monkeypatch, text=f'# mine\nport = 9000\nmusic_folders = ["{a}", "{b}"]\n')
    media_path = client.get("/api/browse").json()                                           # both roots listed
    assert len(media_path["entries"]) == 2
    rid = __import__("sonos_cli.library", fromlist=["root_id"]).root_id(a)
    assert client.get(f"/media/{rid}/x.wav").status_code == 200
    snap = snapshot(a)
    r = post(client, "/api/folders/remove", {"path": str(a)})
    assert r.status_code == 200 and [f["path"] for f in r.json()["folders"]] == [str(b)]
    assert cfgp.read_text() == f'# mine\nport = 9000\nmusic_folders = ["{b}"]\n'
    assert [e["path"] for e in client.get("/api/browse").json()["entries"]] == [str(b)]
    assert client.get(f"/media/{rid}/x.wav").status_code == 404                              # at once
    assert snapshot(a) == snap and (a / "x.wav").exists()
    # control: the comparison does see a destructive remove
    (a / "x.wav").unlink()
    assert snapshot(a) != snap


def test_removing_the_last_folder_leaves_an_empty_list_and_the_no_folders_answer(tmp_path, monkeypatch):
    a = folder(tmp_path, "a")
    system, cfgp, client, home = make(tmp_path, monkeypatch, text=f'music_folders = ["{a}"]\n')
    assert post(client, "/api/folders/remove", {"path": str(a)}).status_code == 200
    assert "music_folders = []" in cfgp.read_text()
    assert client.get("/api/browse").status_code == 409


def test_removing_a_folder_that_is_not_configured_is_404_and_changes_nothing(tmp_path, monkeypatch):
    a = folder(tmp_path, "a")
    system, cfgp, client, home = make(tmp_path, monkeypatch, text=f'music_folders = ["{a}"]\n')
    before = cfgp.read_bytes()
    assert post(client, "/api/folders/remove", {"path": str(folder(tmp_path, "other"))}).status_code == 404
    assert post(client, "/api/folders/remove", {"path": "relative"}).status_code == 400
    assert cfgp.read_bytes() == before


def test_a_folder_that_was_deleted_from_disk_can_still_be_removed(tmp_path, monkeypatch):
    a = folder(tmp_path, "a")
    system, cfgp, client, home = make(tmp_path, monkeypatch, text=f'music_folders = ["{a}"]\n')
    a.rmdir()
    assert post(client, "/api/folders/remove", {"path": str(a)}).status_code == 200 and config.load(cfgp).music_folders == ()


def test_remove_also_removes_the_tilde_and_symlink_spellings_of_the_folder(tmp_path, monkeypatch):
    system, cfgp, client, home = make(tmp_path, monkeypatch, text="")
    real = (home / "Dropbox" / "Music")
    real.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    cfgp.write_text(f'music_folders = ["~/Dropbox/Music", "{alias}"]\n')
    client.app.state.library.swap_music_roots(config.load(cfgp).music_folders)
    assert post(client, "/api/folders/remove", {"path": str(alias)}).status_code == 200
    assert config.load(cfgp).music_folders == () and "music_folders = []" in cfgp.read_text()


# ---- a queue job when its folder is removed ----------------------------------------------------------------------------

def test_a_queue_job_skips_the_tracks_of_a_folder_removed_while_it_runs_without_an_error(tmp_path):
    system = FakeSystem()
    music = folder(tmp_path, "music")
    for i in range(4):
        tiny_wav(music / f"t{i}.wav")
    lib = LiveLibrary(Library.create([music]))
    cfg = config.Config(music_folders=(music,), path=tmp_path / "config.toml")
    calls = []

    def probe(path):
        calls.append(path.name)
        if len(calls) == 2:                                    # the first rest track: the folder is removed from under the job
            lib.swap_music_roots([])
        return FAST_INFO

    player = Player(lib, cfg, system.by_uid, lambda coord: BASE, probe=probe, batch=1)
    result = player.play(system.lounge.uid, [str(music)])
    assert player.wait_idle(10)
    job = player.job(result["job"])
    assert job.skipped and all("music folder was removed" in s["reason"] for s in job.skipped)
    assert not any("queueing stopped" in s["reason"] for s in job.skipped)                # no error escaped the job
    assert system.lounge.queue_size == 1                                                  # only the first track, queued before the removal


# ---- two changes at once ------------------------------------------------------------------------------------------------

def test_two_adds_at_the_same_time_both_land_and_an_add_with_a_remove_leaves_the_file_matching_both(tmp_path, monkeypatch):
    system, cfgp, client, home = make(tmp_path, monkeypatch, text="port = 1\n")
    dirs = [folder(tmp_path, f"d{i}") for i in range(8)]
    results = []
    ts = [threading.Thread(target=lambda d=d: results.append(post(client, "/api/folders/add", {"path": str(d)}).status_code)) for d in dirs]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results == [200] * 8 and set(config.load(cfgp).music_folders) == set(dirs)
    extra = folder(tmp_path, "extra")
    out = []
    t1 = threading.Thread(target=lambda: out.append(post(client, "/api/folders/add", {"path": str(extra)}).status_code))
    t2 = threading.Thread(target=lambda: out.append(post(client, "/api/folders/remove", {"path": str(dirs[0])}).status_code))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert out == [200, 200] or sorted(out) == [200, 200]
    assert set(config.load(cfgp).music_folders) == (set(dirs) - {dirs[0]}) | {extra}


# ---- browse -------------------------------------------------------------------------------------------------------------

def test_browse_with_no_path_gives_the_two_starting_places(tmp_path, monkeypatch):
    system, cfgp, client, home = make(tmp_path, monkeypatch, text="")
    body = client.get("/api/folders/browse").json()
    assert [(e["name"], e["path"]) for e in body["entries"]] == [("Home", str(home.resolve())), ("Volumes", "/Volumes")]


def test_browse_lists_only_visible_subfolder_names_and_marks_one_it_cannot_open(tmp_path, monkeypatch):
    system, cfgp, client, home = make(tmp_path, monkeypatch, text="")
    for n in ("Music", "Zebra", ".hidden", "Locked", "Flagged"):
        (home / n).mkdir()
    (home / "a file.txt").write_text("x")
    (home / "link").symlink_to(home / "Music")
    os.chmod(home / "Locked", 0)
    try:
        os.chflags(home / "Flagged", stat.UF_HIDDEN)
    except (AttributeError, OSError):
        pytest.skip("no chflags")
    try:
        body = client.get("/api/folders/browse", params={"path": str(home)}).json()
    finally:
        os.chmod(home / "Locked", 0o755)
        os.chflags(home / "Flagged", 0)
    got = {e["name"]: e["readable"] for e in body["entries"]}
    assert got == {"Locked": False, "Music": True, "Zebra": True, "link": True}               # no files, no dot names, no hidden flag
    assert body["parent"] is None and all(e["path"].startswith(str(home.resolve())) for e in body["entries"])


@pytest.mark.parametrize("make_path,status", [
    (lambda home, tmp: "/etc", 403), (lambda home, tmp: str(home / ".." / ".." / "etc"), 403), (lambda home, tmp: str(home / "out"), 403),
    (lambda home, tmp: str(tmp), 403), (lambda home, tmp: "/", 403), (lambda home, tmp: str(home / "a file"), 422),
    (lambda home, tmp: str(home / "missing"), 422), (lambda home, tmp: "relative", 400)])
def test_browse_refuses_a_path_outside_home_and_volumes_judged_by_its_real_path(tmp_path, monkeypatch, make_path, status):
    system, cfgp, client, home = make(tmp_path, monkeypatch, text="")
    (home / "a file").write_text("x")
    (home / "out").symlink_to("/etc")                                                     # a symlink under home pointing out
    r = client.get("/api/folders/browse", params={"path": make_path(home, tmp_path)})
    assert r.status_code == status and "error" in r.json()
    assert client.get("/api/folders/browse", params={"path": str(home)}).status_code == 200            # control


def test_a_slow_listing_is_answered_504(tmp_path, monkeypatch):
    system, cfgp, client, home = make(tmp_path, monkeypatch, text="")
    (home / "Music").mkdir()
    monkeypatch.setattr(service, "BROWSE_TIMEOUT_S", 0.2)
    real_scandir = os.scandir
    import time
    def slow(path):
        time.sleep(1.0)
        return real_scandir(path)
    monkeypatch.setattr(os, "scandir", slow)
    r = client.get("/api/folders/browse", params={"path": str(home)})
    assert r.status_code == 504 and "too long" in r.json()["error"]


# ---- found by audit round 1 ---------------------------------------------------------------------------------------------

def test_overlap_is_judged_after_case_folding_and_unicode_normalisation(tmp_path, monkeypatch):
    base = folder(tmp_path, "d")
    (base / "Case" / "Inner").mkdir(parents=True)
    probe = base / "CASEPROBE"
    probe.mkdir()
    if (base / "caseprobe").exists() is False:
        pytest.skip("a case-sensitive volume: `Case` and `case` are two folders here")
    nfd = unicodedata.normalize("NFD", "Café")
    (base / nfd / "sub").mkdir(parents=True)
    for configured, added in ((base / "Case", base / "case" / "Inner"), (base / "Case" / "Inner", base / "case"),
                              (base / nfd, base / unicodedata.normalize("NFC", "Café") / "sub")):
        system, cfgp, client, home = make(tmp_path, monkeypatch, text=f'music_folders = ["{configured}"]\n')
        before = cfgp.read_bytes()
        r = post(client, "/api/folders/add", {"path": str(added)})
        assert r.status_code == 409 and cfgp.read_bytes() == before, (configured, added, r.text)


def test_a_path_with_an_over_long_name_is_refused_not_a_500(tmp_path, monkeypatch):
    system, cfgp, client, home = make(tmp_path, monkeypatch, text="port = 1\n")
    client = TestClient(client.app, base_url=BASE, client=("127.0.0.1", 1), raise_server_exceptions=False)         # an unhandled error shows as a 500
    long_path = "/" + "a" * 300
    assert post(client, "/api/folders/add", {"path": long_path}).status_code in (400, 422)
    assert client.get("/api/folders/browse", params={"path": str(home / ("a" * 300))}).status_code in (400, 422)
    assert post(client, "/api/folders/remove", {"path": long_path}).status_code == 404


def test_a_path_that_cannot_be_encoded_is_a_400_not_a_500(tmp_path, monkeypatch):
    system, cfgp, client, home = make(tmp_path, monkeypatch, text="port = 1\n")
    client = TestClient(client.app, base_url=BASE, client=("127.0.0.1", 1), raise_server_exceptions=False)
    raw = '{"path": "/x\\ud800"}'
    r = client.post("/api/folders/add", content=raw, headers={"Content-Type": "application/json", "Origin": BASE})
    assert r.status_code == 400


def test_the_folder_listing_does_not_run_its_filesystem_calls_on_the_event_loop(tmp_path, monkeypatch):
    system, cfgp, client, home = make(tmp_path, monkeypatch, text=f'music_folders = ["{folder(tmp_path, "a")}"]\n')
    seen = []
    real = service.anyio.to_thread.run_sync
    async def spy(fn, *a, **k):
        seen.append(getattr(fn, "__name__", ""))
        return await real(fn, *a, **k)
    monkeypatch.setattr(service.anyio.to_thread, "run_sync", spy)
    assert client.get("/api/folders").status_code == 200
    assert "folder_view" in seen


def test_a_configured_folder_the_service_cannot_stat_does_not_break_the_listing_an_add_or_a_remove(tmp_path, monkeypatch):
    locked_parent = folder(tmp_path, "locked_parent")
    inside = locked_parent / "music"
    inside.mkdir()
    ok = folder(tmp_path, "ok")
    system, cfgp, client, home = make(tmp_path, monkeypatch, text=f'music_folders = ["{inside}", "{ok}"]\n')
    client = TestClient(client.app, base_url=BASE, client=("127.0.0.1", 1), raise_server_exceptions=False)       # an unhandled error shows as a 500
    os.chmod(locked_parent, 0)                                                                                    # `music` can no longer be looked at
    try:
        listed = client.get("/api/folders")
        assert listed.status_code == 200
        assert {f["path"]: f["exists"] for f in listed.json()["folders"]} == {str(inside): False, str(ok): True}
        added = post(client, "/api/folders/add", {"path": str(folder(tmp_path, "new"))})
        assert added.status_code == 200 and len(added.json()["folders"]) == 3
        assert post(client, "/api/folders/remove", {"path": str(ok)}).status_code == 200
    finally:
        os.chmod(locked_parent, 0o755)


def test_no_local_clients_makes_even_loopback_another_device_and_only_with_fake_rooms(tmp_path, monkeypatch, capsys):
    system, cfgp, client, home = make(tmp_path, monkeypatch, text="port = 1\n")
    cfg = config.load(cfgp)
    app = service.create_app(cfg, library=Library.create([]), allowed_hosts={"127.0.0.1"}, no_local_clients=True)
    c = TestClient(app, base_url=BASE, client=("127.0.0.1", 50000))
    assert c.get("/api/folders").json()["editable"] is False and c.get("/api/folders/browse").status_code == 403
    assert client.get("/api/folders").json()["editable"] is True                                # control: the same peer without the option
    from sonos_cli import cli
    assert cli.main(["service", "run", "--no-local-clients"]) == 2                              # refused without --fake-rooms
    assert "--fake-rooms" in capsys.readouterr().err
