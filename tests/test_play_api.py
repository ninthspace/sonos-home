"""POST /api/play, GET /api/browse and their error paths, end to end against fake rooms (spec FR5, FR9, FR10)."""

import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from audio import make_tone
from fakes import FakeSystem
from sonos_cli import config, library, playback, service
from sonos_cli.library import Library
from sonos_cli.rooms import RoomMonitor

BASE = "http://127.0.0.1:8737"
JSON = {"content-type": "application/json", "origin": BASE}


def setup_api(tmp_path, folders=True, monkeypatch=None):
    system = FakeSystem()
    music = tmp_path / "music"
    music.mkdir(exist_ok=True)
    cfg = config.Config(music_folders=(music.resolve(),) if folders else (), path=tmp_path / "config.toml")
    lib = Library.create(cfg.music_folders, [tmp_path / "cache"])
    monitor = RoomMonitor(system.discover)
    monitor.refresh()
    app = service.create_app(cfg, library=lib, allowed_hosts={"127.0.0.1"}, monitor=monitor, port=8737)
    return system, music.resolve(), cfg, lib, monitor, TestClient(app, base_url=BASE)


def post_play(client, **body):
    return client.post("/api/play", json=body, headers=JSON)


def test_play_a_mixed_folder_through_the_api_and_fetch_what_was_queued(tmp_path, monkeypatch):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    mix = music / "mix"
    make_tone(mix / "01-plain.wav")
    make_tone(mix / "02-hires.wav", rate=96000, bits=24)
    make_tone(mix / "03-song.mp3")
    (mix / "04-cloud.wav").write_bytes(b"")
    monkeypatch.setattr(playback, "is_dataless", lambda p: p.name == "04-cloud.wav")
    r = post_play(client, room=system.lounge.uid, paths=[str(mix)], mode="replace")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3 and body["started"] is True and body["skipped"][0]["path"].endswith("04-cloud.wav")
    for _ in range(100):
        if client.get(f"/api/play/jobs/{body['job']}").json()["done"]:
            break
        time.sleep(0.05)
    job = client.get(f"/api/play/jobs/{body['job']}").json()
    assert job["done"] and job["queued"] == 3
    queue = system.lounge.queue
    assert [i.title for i in queue] == ["01-plain", "02-hires", "03-song"]
    # the speaker would now fetch these: path part of each queued URL, through the same app
    types = []
    for item in queue:
        url = item.resources[0].uri
        assert url.startswith("http://") and ":8737/media/" in url
        resp = client.get(url.split(":8737", 1)[1])
        assert resp.status_code == 200
        types.append((resp.headers["content-type"], item.resources[0].protocol_info.split(":")[2]))
    assert types == [("audio/wav", "audio/wav"), ("audio/flac", "audio/flac"), ("audio/mpeg", "audio/mpeg")]


def test_with_no_music_folders_browse_and_play_are_409_naming_config_toml_and_nothing_is_queued(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path, folders=False)
    r = client.get("/api/browse")
    assert r.status_code == 409 and str(cfg.path) in r.json()["error"] and "music_folders" in r.json()["error"]
    r = post_play(client, room=system.lounge.uid, paths=[str(tmp_path)])
    assert r.status_code == 409 and str(cfg.path) in r.json()["error"]
    assert system.lounge.queue_size == 0 and system.lounge.calls == []


def test_control_with_one_folder_configured_the_same_requests_succeed(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    make_tone(music / "a.wav")
    assert client.get("/api/browse").status_code == 200
    assert post_play(client, room=system.lounge.uid, paths=[str(music)]).status_code == 200
    assert system.lounge.queue_size >= 1


def test_browse_through_the_api_pages_and_validates(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    for i in range(250):
        (music / f"t{i:03}.wav").write_bytes(b"")
    (music / "sub").mkdir()
    first = client.get("/api/browse", params={"path": str(music)}).json()
    assert len(first["entries"]) == 200 and first["entries"][0]["name"] == "sub" and first["next_offset"] == 200
    rest = client.get("/api/browse", params={"path": str(music), "offset": 200}).json()
    assert len(rest["entries"]) == 51 and rest["next_offset"] is None
    assert client.get("/api/browse", params={"path": str(music), "offset": "x"}).status_code == 400
    assert client.get("/api/browse", params={"path": "/etc"}).status_code == 403
    assert client.get("/api/browse", params={"path": str(music / "t000.wav")}).status_code == 422


@pytest.mark.parametrize("body, status", [
    ({"room": "RINCON_NOPE", "paths": ["PLACEHOLDER"]}, 404),
    ({"room": "ROOM", "paths": ["/etc/hosts"]}, 403),
    ({"room": "ROOM", "paths": ["PLACEHOLDER/nope.wav"]}, 422),
    ({"room": "ROOM", "paths": ["PLACEHOLDER"], "mode": "sideways"}, 400),
    ({"room": "ROOM", "paths": []}, 400),
    ({"room": "ROOM"}, 400),
    ({"paths": ["PLACEHOLDER"]}, 400),
    ({"room": 5, "paths": ["PLACEHOLDER"]}, 400),
    ({"room": "ROOM", "paths": [3]}, 400),
])
def test_play_errors_have_the_right_status_and_queue_nothing(tmp_path, body, status):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    make_tone(music / "a.wav")
    body = {k: ([str(music) if x == "PLACEHOLDER" else (x.replace("PLACEHOLDER", str(music)) if isinstance(x, str) else x) for x in v]
                if k == "paths" else (system.lounge.uid if v == "ROOM" else v)) for k, v in body.items()}
    r = post_play(client, **body)
    assert r.status_code == status, r.text
    assert "error" in r.json() and system.lounge.queue_size == 0 and ("clear_queue",) not in system.lounge.calls


def test_a_body_that_is_not_json_or_not_an_object_is_a_400(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    assert client.post("/api/play", content=b"not json", headers=JSON).status_code == 400
    assert client.post("/api/play", content=b"[1,2]", headers=JSON).status_code == 400


def test_an_unknown_job_is_404(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    assert client.get("/api/play/jobs/nope").status_code == 404


def test_play_via_a_group_member_queues_on_the_coordinator(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    make_tone(music / "a.wav")
    system.kitchen.join(system.lounge)
    monitor.refresh()
    r = post_play(client, room=system.kitchen.uid, paths=[str(music / "a.wav")])
    assert r.status_code == 200, r.text
    assert system.lounge.queue_size == 1 and system.kitchen.queue_size == 0


def test_playing_through_the_api_updates_the_monitors_cache_at_once(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    make_tone(music / "a.wav")
    assert monitor.state(system.lounge.uid)["state"] == "STOPPED"
    assert post_play(client, room=system.lounge.uid, paths=[str(music / "a.wav")]).status_code == 200
    st = monitor.state(system.lounge.uid)
    assert st["state"] == "PLAYING" and st["track"]["title"] == "a"


def test_a_speaker_error_through_the_api_is_json_502(tmp_path, monkeypatch):
    from fakes import FakeSoCo
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    make_tone(music / "a.wav")
    monkeypatch.setattr(FakeSoCo, "clear_queue", lambda self: (_ for _ in ()).throw(OSError("timed out")))
    r = post_play(client, room=system.lounge.uid, paths=[str(music / "a.wav")])
    assert r.status_code == 502 and "timed out" in r.json()["error"]


def test_a_play_after_a_regroup_the_monitor_has_not_noticed_goes_to_the_new_coordinator(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    make_tone(music / "a.wav")
    system.lounge.join(system.kitchen)                              # regrouped in the Sonos app; no refresh since
    r = post_play(client, room=system.lounge.uid, paths=[str(music / "a.wav")])
    assert r.status_code == 200, r.text
    assert system.kitchen.queue_size == 1 and system.lounge.queue_size == 0


def test_shuffle_must_be_a_boolean_and_an_unreadable_folder_is_json(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    make_tone(music / "a.wav")
    assert post_play(client, room=system.lounge.uid, paths=[str(music)], shuffle="false").status_code == 400
    assert post_play(client, room=system.lounge.uid, paths=[str(music)], shuffle=True).status_code == 200
    (music / "locked").mkdir()
    (music / "locked").chmod(0)
    try:
        r = client.get("/api/browse", params={"path": str(music / "locked")})
        assert r.status_code == 403 and "error" in r.json()
    finally:
        (music / "locked").chmod(0o755)


# ---- transport and volume endpoints (used by the CLI and the UI) ---------------------------------------------------

def post_json(client, path, body):
    return client.post(path, json=body, headers=JSON)


def test_transport_actions_act_on_the_groups_coordinator(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    make_tone(music / "a.wav")
    make_tone(music / "b.wav")
    system.kitchen.join(system.lounge)
    monitor.refresh()
    post_play(client, room=system.lounge.uid, paths=[str(music)])
    for _ in range(100):
        if system.lounge.queue_size == 2:
            break
        time.sleep(0.05)
    for action, expect in (("pause", "PAUSED_PLAYBACK"), ("resume", "PLAYING"), ("next", "PLAYING"), ("prev", "PLAYING"), ("stop", "STOPPED")):
        r = post_json(client, f"/api/rooms/{system.kitchen.uid}/transport", {"action": action})   # via the member
        assert r.status_code == 200 and r.json() == {"ok": True, "action": action}
        assert system.lounge.transport_state == expect, action
    assert system.kitchen.calls.count(("pause",)) == 0                 # nothing was sent to the member itself
    assert monitor.state(system.lounge.uid)["state"] == "STOPPED"      # and the cache is fresh


@pytest.mark.parametrize("path_uid, body, status", [
    ("LOUNGE", {"action": "explode"}, 400), ("LOUNGE", {}, 400), ("RINCON_NOPE", {"action": "pause"}, 404),
])
def test_transport_errors(tmp_path, path_uid, body, status):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    uid = system.lounge.uid if path_uid == "LOUNGE" else path_uid
    r = post_json(client, f"/api/rooms/{uid}/transport", body)
    assert r.status_code == status and "error" in r.json()


def test_a_speaker_that_refuses_a_transport_action_is_a_json_502(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    r = post_json(client, f"/api/rooms/{system.lounge.uid}/transport", {"action": "next"})     # empty queue: SoCo 711
    assert r.status_code == 502 and "next" in r.json()["error"]


def test_volume_sets_the_group_and_validates_the_level(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    r = post_json(client, f"/api/rooms/{system.office.uid}/volume", {"level": 25})
    assert r.status_code == 200 and r.json() == {"volume": 25}
    assert system.office.volume == 25 and system.office_satellite.volume == 25
    assert monitor.state(system.office.uid)["volume"] == 25
    for bad in (-1, 101, "30", 30.5, True, None):
        r = post_json(client, f"/api/rooms/{system.office.uid}/volume", {"level": bad})
        assert r.status_code == 400, bad
    assert post_json(client, "/api/rooms/RINCON_NOPE/volume", {"level": 5}).status_code == 404
    assert system.office.volume == 25
