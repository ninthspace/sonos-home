"""The fake system's real-time mode (`sonos service run --fake-rooms`) and the fake-only position route (epic 01-10 Story 1)."""

import time

import pytest
from starlette.testclient import TestClient
from soco.data_structures import DidlMusicTrack, DidlResource

from fakes import FakeSystem
from sonos_cli import config, service
from sonos_cli.library import Library
from sonos_cli.rooms import RoomMonitor
from test_play_api import JSON, post_json


def track(title, seconds):
    h, rest = divmod(seconds, 3600)
    return DidlMusicTrack(title=title, parent_id="", item_id=title, resources=[DidlResource(
        uri=f"http://127.0.0.1:8737/media/x/{title}.flac", protocol_info="http-get:*:audio/flac:*", duration=f"{h}:{rest // 60:02d}:{rest % 60:02d}")])


def playing(system, *durations):
    for i, d in enumerate(durations):
        system.lounge.add_to_queue(track(f"t{i}", d))
    system.lounge.play_from_queue(0)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    return now


def position(system):
    return system.lounge.get_current_track_info()["position"]


def test_a_playing_room_follows_the_clock_and_a_paused_or_stopped_one_does_not(clock):
    system = FakeSystem(realtime=True)
    playing(system, 240)
    clock[0] += 3
    assert position(system) == "0:00:03"
    system.lounge.pause()
    clock[0] += 50                                       # paused for 50 s: no movement
    assert position(system) == "0:00:03"
    system.lounge.play()
    clock[0] += 2
    assert position(system) == "0:00:05"
    system.lounge.stop()
    clock[0] += 9
    assert system.lounge.transport_state == "STOPPED" and position(system) == "0:00:05"


def test_a_transport_call_first_brings_the_playhead_up_to_now_so_paused_time_is_never_counted(clock):
    system = FakeSystem(realtime=True)
    playing(system, 240)
    clock[0] += 3
    system.lounge.pause()                                # no read in between: pause() itself must count the 3 s played
    clock[0] += 50
    system.lounge.play()                                 # and play() must not count the 50 s paused
    assert position(system) == "0:00:03"


def test_it_never_passes_the_duration_and_moves_to_the_next_item_or_stops(clock):
    system = FakeSystem(realtime=True)
    playing(system, 10, 20)
    clock[0] += 14                                       # 4 s into the second item
    info = system.lounge.get_current_track_info()
    assert (info["playlist_position"], info["position"]) == ("2", "0:00:04")
    clock[0] += 100                                      # far past the end of the last item
    info = system.lounge.get_current_track_info()
    assert system.lounge.transport_state == "STOPPED" and info["position"] == "0:00:00"


def test_a_track_with_no_duration_keeps_counting(clock):
    system = FakeSystem(realtime=True)
    system.lounge.add_to_queue(track("live", 0))
    system.lounge.play_from_queue(0)
    clock[0] += 7
    assert position(system) == "0:00:07"


def test_the_pytest_fake_still_moves_only_when_a_test_advances_it_and_the_clock_mode_is_the_control(clock):
    manual = FakeSystem()
    playing(manual, 240)
    clock[0] += 5
    assert position(manual) == "0:00:00"                 # real time passed, the playhead did not
    manual.lounge.advance(2)
    assert position(manual) == "0:00:02"
    timed = FakeSystem(realtime=True)                    # control: the same fixture with the clock mode on moves
    playing(timed, 240)
    clock[0] += 5
    assert position(timed) == "0:00:05"


# ---- the fake-only position route -----------------------------------------------------------------------------

def app_with(fake_system, tmp_path):
    cfg = config.Config(path=tmp_path / "config.toml")
    monitor = RoomMonitor(fake_system.discover)
    monitor.refresh()
    app = service.create_app(cfg, library=Library.create([]), allowed_hosts={"127.0.0.1"}, monitor=monitor, port=8737,
                             fake_system=fake_system)
    return TestClient(app, base_url="http://127.0.0.1:8737"), monitor


def test_the_fake_position_route_moves_the_playhead_and_clamps_it_to_the_duration(tmp_path):
    system = FakeSystem()
    playing(system, 240)
    client, monitor = app_with(system, tmp_path)
    uid = system.lounge.uid
    assert post_json(client, f"/api/fake/rooms/{uid}/position", {"position": 238}).status_code == 200
    assert system.lounge.position == 238 and client.get(f"/api/rooms/{uid}/state").json()["position"] == "0:03:58"
    r = post_json(client, f"/api/fake/rooms/{uid}/position", {"position": 9999})
    assert r.status_code == 200 and system.lounge.position == 239          # never at or past the end


@pytest.mark.parametrize("body", [{}, {"position": "5"}, {"position": -1}, {"position": True}, {"position": None}])
def test_the_fake_position_route_refuses_bad_positions_and_leaves_the_playhead(tmp_path, body):
    system = FakeSystem()
    playing(system, 240)
    system.lounge.position = 12
    client, monitor = app_with(system, tmp_path)
    r = post_json(client, f"/api/fake/rooms/{system.lounge.uid}/position", body)
    assert r.status_code == 400 and "error" in r.json() and system.lounge.position == 12
    assert post_json(client, f"/api/fake/rooms/{system.lounge.uid}/position", {"position": 30}).status_code == 200      # control
    assert post_json(client, "/api/fake/rooms/RINCON_NOPE/position", {"position": 5}).status_code == 404


def test_without_a_fake_system_the_position_route_does_not_exist(tmp_path):
    system = FakeSystem()
    cfg = config.Config(path=tmp_path / "config.toml")
    monitor = RoomMonitor(system.discover)
    monitor.refresh()
    app = service.create_app(cfg, library=Library.create([]), allowed_hosts={"127.0.0.1"}, monitor=monitor, port=8737)
    client = TestClient(app, base_url="http://127.0.0.1:8737", raise_server_exceptions=False)      # a handler that ran would show as a 500
    playing(system, 240)
    r = post_json(client, f"/api/fake/rooms/{system.lounge.uid}/position", {"position": 5})
    assert r.status_code == post_json(client, "/api/no-such-route", {}).status_code != 200          # as any unknown path
    assert system.lounge.position == 0                                                               # and no handler ran


def test_in_real_time_the_position_set_by_the_route_is_not_added_to_by_time_played_before_it(tmp_path, clock):
    system = FakeSystem(realtime=True)
    playing(system, 240, 240)
    client, monitor = app_with(system, tmp_path)
    clock[0] += 30                                       # 30 s played with no read in between
    r = post_json(client, f"/api/fake/rooms/{system.lounge.uid}/position", {"position": 230})
    assert r.status_code == 200 and r.json() == {"position": 230}
    info = system.lounge.get_current_track_info()
    assert (info["playlist_position"], info["position"]) == ("1", "0:03:50")


def test_run_with_fake_rooms_builds_a_real_time_fake_and_the_position_route(tmp_path, monkeypatch):
    """`sonos service run --fake-rooms` through `service.run`, with uvicorn stubbed so the app can be driven in-process."""
    import uvicorn
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.setdefault("app", app))
    cfg = config.Config(path=tmp_path / "config.toml")
    assert service.run(cfg, port=8799, fake_rooms=True) == 0
    with TestClient(captured["app"], base_url="http://127.0.0.1:8799") as client:
        rooms = client.get("/api/rooms").json()
        lounge = next(r for r in rooms if r["name"] == "Lounge")
        playing_fake = [d for d in service_fake(captured["app"]).devices if d.player_name == "Lounge"][0]
        playing_fake.add_to_queue(track("t0", 240))
        playing_fake.play_from_queue(0)
        seen = []
        for _ in range(50):                                       # the monitor polls a watched room every 2 s; wait for readings
            pos = client.get(f"/api/rooms/{lounge['uid']}/state").json()["position"]
            if pos and (not seen or pos != seen[-1]):
                seen.append(pos)
            if len(seen) >= 2:
                break
            time.sleep(0.2)
        assert len(seen) >= 2 and seen[0] < seen[1], seen          # the real clock moved it between two readings
        own = {"Origin": "http://127.0.0.1:8799", "Content-Type": "application/json"}
        url = f"/api/fake/rooms/{lounge['uid']}/position"
        assert client.post(url, json={"position": 100}, headers=own).status_code == 200
        assert client.post(url, json={"position": 100}, headers={**own, "Origin": "http://evil.test"}).status_code == 403      # Hardening applies


def service_fake(app):
    """The FakeSystem `run()` built, found through the route's closure."""
    for route in app.app.routes if hasattr(app, "app") else app.routes:
        if getattr(route, "path", "") == "/api/fake/rooms/{uid}/position":
            return next(c.cell_contents for c in route.endpoint.__closure__ if hasattr(c.cell_contents, "realtime"))
    raise AssertionError("no fake position route")
