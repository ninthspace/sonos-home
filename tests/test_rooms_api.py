"""Room management: rename, group, ungroup, group all, from the API and the CLI (spec FR11)."""

import pytest
from starlette.testclient import TestClient

from audio import make_tone
from fakes import FakeSystem
from sonos_cli import cli, config, service
from sonos_cli.library import Library
from sonos_cli.rooms import RoomMonitor
from test_cli import point_cli_at, Recorder, run_cli  # noqa: F401  (the fixtures below reuse its helpers)
from test_play_api import JSON, post_json, post_play, setup_api

import threading
import time
import uvicorn


def test_rename_group_ungroup_and_group_all_call_the_right_devices(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    r = post_json(client, f"/api/rooms/{system.office.uid}/rename", {"name": "Studio"})
    assert r.status_code == 200 and r.json() == {"uid": system.office.uid, "name": "Studio"}
    assert ("player_name", "Studio") in system.office.calls
    assert not any(c[0] == "player_name" for c in system.office_satellite.calls)
    assert {x["name"] for x in monitor.rooms()} == {"Studio", "Bedroom", "Kitchen", "Lounge"}      # shown at once

    r = post_json(client, f"/api/rooms/{system.kitchen.uid}/group", {"into": system.lounge.uid})
    assert r.status_code == 200 and ("join", system.lounge.uid) in system.kitchen.calls
    assert system.kitchen.coordinator is system.lounge
    assert monitor.room(system.kitchen.uid).coordinator_uid == system.lounge.uid        # cache follows within the call

    r = post_json(client, f"/api/rooms/{system.kitchen.uid}/ungroup", {})
    assert r.status_code == 200 and ("unjoin",) in system.kitchen.calls and system.kitchen.is_coordinator
    assert monitor.room(system.kitchen.uid).coordinator_uid == system.kitchen.uid

    r = post_json(client, "/api/rooms/group-all", {"into": system.lounge.uid})
    assert r.status_code == 200 and ("partymode",) in system.lounge.calls
    assert {d.coordinator for d in system.devices} == {system.lounge}
    assert system.office_satellite.calls == [] or all(c[0] != "join" for c in system.office_satellite.calls)
    assert system.office_satellite.coordinator is system.lounge          # it went with its partner, never told to join


def test_after_a_rename_the_same_uid_still_plays_on_the_same_device(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    make_tone(music / "a.wav")
    uid = system.office.uid
    post_json(client, f"/api/rooms/{uid}/rename", {"name": "Studio"})
    monitor.refresh()
    assert monitor.room(uid).name == "Studio"
    assert post_play(client, room=uid, paths=[str(music / "a.wav")]).status_code == 200
    assert system.office.queue_size == 1 and system.office.player_name == "Studio"


def test_transport_queue_and_volume_for_a_member_act_on_the_coordinator(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    make_tone(music / "a.wav")
    post_json(client, f"/api/rooms/{system.kitchen.uid}/group", {"into": system.lounge.uid})
    assert post_play(client, room=system.kitchen.uid, paths=[str(music / "a.wav")]).status_code == 200
    assert system.lounge.queue_size == 1 and system.kitchen.queue_size == 0
    assert post_json(client, f"/api/rooms/{system.kitchen.uid}/transport", {"action": "pause"}).status_code == 200
    assert system.lounge.transport_state == "PAUSED_PLAYBACK"
    assert post_json(client, f"/api/rooms/{system.kitchen.uid}/volume", {"level": 12}).json() == {"volume": 12}
    assert system.lounge.volume == 12 and system.kitchen.volume == 12                 # the group's volume
    assert ("pause",) not in system.kitchen.calls and ("play_from_queue", 0) not in system.kitchen.calls


@pytest.mark.parametrize("path, body, status", [
    ("rename", {"name": ""}, 400), ("rename", {"name": "   "}, 400), ("rename", {"name": "x" * 65}, 400),
    ("rename", {"name": "bad\nname"}, 400), ("rename", {"name": 5}, 400), ("rename", {}, 400),
    ("group", {}, 400), ("group", {"into": 3}, 400), ("group", {"into": "SELF"}, 400), ("group", {"into": "RINCON_NOPE"}, 404),
    ("ungroup", {}, 200),
])
def test_bad_room_management_requests(tmp_path, path, body, status):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    body = {k: (system.kitchen.uid if v == "SELF" else v) for k, v in body.items()}
    r = post_json(client, f"/api/rooms/{system.kitchen.uid}/{path}", body)
    assert r.status_code == status, r.text
    if status != 200:
        assert "error" in r.json() and system.kitchen.calls == [] and monitor.room(system.kitchen.uid).name == "Kitchen"


def test_unknown_rooms_and_group_all_validation(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    assert post_json(client, "/api/rooms/RINCON_NOPE/rename", {"name": "x"}).status_code == 404
    assert post_json(client, "/api/rooms/RINCON_NOPE/ungroup", {}).status_code == 404
    assert post_json(client, "/api/rooms/group-all", {}).status_code == 400
    assert post_json(client, "/api/rooms/group-all", {"into": "RINCON_NOPE"}).status_code == 404
    assert all(d.calls == [] for d in system.devices)


def test_a_speaker_that_refuses_is_a_json_502(tmp_path, monkeypatch):
    from fakes import FakeSoCo
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    monkeypatch.setattr(FakeSoCo, "join", lambda self, master: (_ for _ in ()).throw(OSError("timed out")))
    r = post_json(client, f"/api/rooms/{system.kitchen.uid}/group", {"into": system.lounge.uid})
    assert r.status_code == 502 and "timed out" in r.json()["error"]


# ---- the CLI ----------------------------------------------------------------------------------------------------

@pytest.fixture
def running(tmp_path, monkeypatch):
    system = FakeSystem().install(monkeypatch)
    music = tmp_path / "music"
    music.mkdir()
    cfg = config.Config(music_folders=(music.resolve(),), path=tmp_path / "service" / "config.toml")
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
    point_cli_at(tmp_path, monkeypatch, server.servers[0].sockets[0].getsockname()[1], [music.resolve()])
    yield type("R", (), dict(app=app, system=system, monitor=monitor))
    server.should_exit = True
    t.join(5)


def test_cli_rename_group_ungroup_and_group_all_resolve_names_and_call_the_endpoints(running, capsys):
    s = running.system
    assert run_cli("rename", "office", "Studio") == 0 and ("player_name", "Studio") in s.office.calls
    assert run_cli("group", "Kitchen", "Lounge") == 0 and s.kitchen.coordinator is s.lounge
    assert run_cli("ungroup", "kitchen") == 0 and s.kitchen.is_coordinator
    assert run_cli("-r", "Bedroom", "group", "--all") == 0 and ("partymode",) in s.bedroom.calls
    calls = [(m, p.split("/")[-1], b) for m, p, b in running.app.calls() if m == "POST"]
    assert calls == [("POST", "rename", {"name": "Studio"}), ("POST", "group", {"into": s.lounge.uid}),
                     ("POST", "ungroup", {}), ("POST", "group-all", {"into": s.bedroom.uid})]
    out = capsys.readouterr().out
    assert "Renamed office to Studio" in out and "Kitchen now plays with Lounge" in out


@pytest.mark.parametrize("argv", [("rename", "Garage", "x"), ("group", "Kitchen", "Garage"), ("group", "Garage", "Lounge"),
                                  ("ungroup", "Garage")])
def test_an_unknown_room_lists_the_available_rooms(running, capsys, argv):
    assert run_cli(*argv) == 1
    err = capsys.readouterr().err
    assert "Garage" in err and "Kitchen" in err and "Lounge" in err and "Office" in err
    assert not any(m == "POST" for m, p, b in running.app.calls())


@pytest.mark.parametrize("argv", [("group",), ("group", "Kitchen"), ("group", "--all", "Kitchen", "Lounge")])
def test_group_needs_two_rooms_or_all(running, capsys, argv):
    assert run_cli(*argv) == 1
    assert "sonos group" in capsys.readouterr().err and not any(m == "POST" for m, p, b in running.app.calls())


def test_the_cli_commands_work_without_the_service_too(monkeypatch, capsys):
    system = FakeSystem().install(monkeypatch)
    assert run_cli("rename", "Kitchen", "Den") == 0 and system.kitchen.player_name == "Den"
    assert run_cli("group", "Den", "Lounge") == 0 and system.kitchen.coordinator is system.lounge
    assert run_cli("ungroup", "Den") == 0 and system.kitchen.is_coordinator
    assert run_cli("--ip", "192.168.0.26", "group", "--all") == 0 and ("partymode",) in system.lounge.calls


# ---- audit round 1 ---------------------------------------------------------------------------------------------

def test_a_rename_to_another_rooms_name_is_refused_in_any_case_and_a_name_is_trimmed(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    r = post_json(client, f"/api/rooms/{system.office.uid}/rename", {"name": "kitchen"})
    assert r.status_code == 409 and "already called" in r.json()["error"]
    assert system.office.player_name == "Office" and system.office.calls == []
    r = post_json(client, f"/api/rooms/{system.office.uid}/rename", {"name": "  Studio  "})
    assert r.status_code == 200 and r.json()["name"] == "Studio" and system.office.player_name == "Studio"
    assert post_json(client, f"/api/rooms/{system.office.uid}/rename", {"name": "Studio"}).status_code == 200   # its own name


@pytest.mark.parametrize("bad", ["a\x7fb", "a b", "a​b", "日" * 64, "x" * 65])
def test_names_with_invisible_characters_or_too_long_are_refused(tmp_path, bad):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    r = post_json(client, f"/api/rooms/{system.kitchen.uid}/rename", {"name": bad})
    assert r.status_code == 400 and system.kitchen.calls == []


def test_a_rename_the_speakers_topology_has_not_caught_up_with_is_not_undone_by_a_discovery(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    monitor.set_room_name(system.kitchen.uid, "Den")              # the speaker still reports "Kitchen"
    monitor.refresh()
    assert monitor.room(system.kitchen.uid).name == "Den"
    system.kitchen.player_name = "Den"                             # now it agrees: the pending name is dropped
    monitor.refresh()
    system.kitchen._name = "Den2"
    monitor.refresh()
    assert monitor.room(system.kitchen.uid).name == "Den2"


def test_joining_a_group_you_are_already_in_does_not_bother_the_speaker(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    post_json(client, f"/api/rooms/{system.kitchen.uid}/group", {"into": system.lounge.uid})
    system.lounge.calls.clear()
    system.kitchen.calls.clear()
    for room, into in ((system.lounge, system.kitchen), (system.kitchen, system.lounge)):    # either order
        assert post_json(client, f"/api/rooms/{room.uid}/group", {"into": into.uid}).status_code == 200
    assert system.lounge.calls == [] and system.kitchen.calls == []


def test_a_partly_done_party_still_asks_for_a_rediscovery(tmp_path, monkeypatch):
    from fakes import FakeSoCo
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    monkeypatch.setattr(FakeSoCo, "partymode", lambda self: (_ for _ in ()).throw(OSError("dropped out")))
    monitor._rediscover = False
    assert post_json(client, "/api/rooms/group-all", {"into": system.lounge.uid}).status_code == 502
    assert monitor._rediscover is True


def test_the_direct_cli_applies_the_same_checks(system_direct, capsys):
    s = system_direct
    assert run_cli("rename", "Kitchen", "  ") == 1 and s.kitchen.player_name == "Kitchen"
    assert run_cli("rename", "Kitchen", "lounge") == 1 and "already called" in capsys.readouterr().err
    assert run_cli("rename", "Kitchen", "  Den  ") == 0 and s.kitchen.player_name == "Den"
    assert run_cli("group", "Lounge", "Lounge") == 0 and all(c[0] != "join" for c in s.lounge.calls)
    s.kitchen.join(s.lounge)
    s.kitchen.calls.clear()
    s.lounge.calls.clear()
    assert run_cli("group", "Den", "Lounge") == 0 and s.kitchen.calls == [] and s.lounge.calls == []


@pytest.fixture
def system_direct(monkeypatch):
    return FakeSystem().install(monkeypatch)


def test_already_together_is_decided_by_the_speaker_not_by_the_cache(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    post_json(client, f"/api/rooms/{system.kitchen.uid}/group", {"into": system.lounge.uid})
    system.kitchen.unjoin()                                  # done in the Sonos app; the cache hasn't caught up
    assert monitor.room(system.kitchen.uid).coordinator_uid == system.lounge.uid
    system.kitchen.calls.clear()
    assert post_json(client, f"/api/rooms/{system.kitchen.uid}/group", {"into": system.lounge.uid}).status_code == 200
    assert ("join", system.lounge.uid) in system.kitchen.calls and system.kitchen.coordinator is system.lounge


def test_names_may_use_zero_width_joiners_but_not_other_invisible_characters(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    for ok in ("🧑‍🍳 Kitchen", "می‌خواهم"):
        assert post_json(client, f"/api/rooms/{system.kitchen.uid}/rename", {"name": ok}).status_code == 200, ok


def test_when_a_groups_coordinator_leaves_the_rooms_it_led_are_reported_with_their_new_group_at_once(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    for room in (system.kitchen, system.bedroom):
        assert post_json(client, f"/api/rooms/{room.uid}/group", {"into": system.lounge.uid}).status_code == 200
    assert {monitor.room(u.uid).coordinator_uid for u in (system.kitchen, system.bedroom, system.lounge)} == {system.lounge.uid}
    assert post_json(client, f"/api/rooms/{system.lounge.uid}/ungroup", {}).status_code == 200
    rooms = {r["uid"]: r for r in client.get("/api/rooms").json()}
    leader = rooms[system.lounge.uid]
    assert leader["coordinator_uid"] == system.lounge.uid and [m["name"] for m in leader["members"] if m["visible"]] == ["Lounge"]
    for room in (system.kitchen, system.bedroom):
        entry = rooms[room.uid]
        assert entry["coordinator_uid"] != system.lounge.uid                           # nobody still claims the one who left
        assert system.lounge.uid not in [m["uid"] for m in entry["members"]]
        assert sorted(m["name"] for m in entry["members"] if m["visible"]) == ["Bedroom", "Kitchen"]


# ---- the reading's age (epic 01-10 Story 2) ---------------------------------------------------------------------------

def test_the_room_state_carries_when_it_was_read_and_when_it_was_answered(tmp_path):
    import time as _t
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    uid = system.lounge.uid
    before = _t.time()
    monitor.refresh_group_of(uid)
    body = client.get(f"/api/rooms/{uid}/state").json()
    after = _t.time()
    assert isinstance(body.get("updated"), float) and isinstance(body.get("now"), float)
    assert before <= body["updated"] <= body["now"] <= after
    _t.sleep(0.05)
    again = client.get(f"/api/rooms/{uid}/state").json()
    assert again["updated"] == body["updated"] and again.get("now", 0) > body["now"]          # the cached reading is the same one, answered later
