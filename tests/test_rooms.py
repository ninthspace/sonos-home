"""RoomMonitor and the rooms API (spec FR4, NFR5, AD7, AD8)."""

import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from fakes import FakeSoCo, FakeSystem
from sonos_cli import config, player, rooms, service
from sonos_cli.library import Library
from sonos_cli.media import AudioInfo, Track
from sonos_cli.rooms import RoomMonitor, classify_source

BASE = "http://127.0.0.1:8737"
OUR_IP = "192.168.0.204"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def system():
    return FakeSystem()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def monitor(system, clock):
    m = RoomMonitor(system.discover, our_hosts=lambda: {OUR_IP, "127.0.0.1"}, our_port=8737, clock=clock)
    m.refresh()
    return m


def api(monitor, tmp_path):
    cfg = config.Config(path=tmp_path / "config.toml")
    app = service.create_app(cfg, library=Library.create([]), allowed_hosts={"127.0.0.1"}, monitor=monitor)
    return TestClient(app, base_url=BASE)


def track(url):
    item = player.queue_item(Track(Path("/m/a.flac"), AudioInfo("flac", 44100, 2, 16, 10.0, "Song", "Me", "LP")), url, 0)
    return item


def play_url(device, url):
    device.clear_queue()
    device.add_to_queue(track(url))
    device.play_from_queue(0)


def tripwire(monkeypatch):
    """Any read from a device after this raises: proves a request is answered from the cache."""
    hits = []

    def boom(*a, **k):
        hits.append(1)
        raise AssertionError("SoCo was called")

    for name in ("get_current_transport_info", "get_current_track_info", "get_current_media_info",
                 "get_speaker_info", "get_queue"):
        monkeypatch.setattr(FakeSoCo, name, boom)
    for name in ("group", "all_zones", "visible_zones", "player_name", "queue_size"):
        monkeypatch.setattr(FakeSoCo, name, property(boom))
    return hits


# ---- /api/rooms -------------------------------------------------------------------------------------

def test_api_rooms_matches_the_discovered_system_and_is_served_from_cache(system, monitor, tmp_path, monkeypatch):
    system.lounge.volume = 22
    system.bedroom.mute = True
    monitor.refresh()
    expected = {
        d.uid: {"name": d.player_name, "ip": d.ip_address, "model": d.model_name,
                "coordinator_uid": d.coordinator.uid,
                "members": sorted(m.ip_address for m in d.group.members),
                "state": d.coordinator.transport_state, "volume": d.group.volume, "mute": d.group.mute}
        for d in system.visible}
    hits = tripwire(monkeypatch)
    client = api(monitor, tmp_path)
    r = client.get("/api/rooms")
    assert hits == [] and r.status_code == 200
    got = {room["uid"]: room for room in r.json()}
    assert set(got) == set(expected) and len(got) == 4          # the satellite isn't a room of its own
    for uid, want in expected.items():
        room = got[uid]
        assert {k: room[k] for k in ("name", "ip", "model", "coordinator_uid", "state", "volume", "mute")} == \
               {k: want[k] for k in ("name", "ip", "model", "coordinator_uid", "state", "volume", "mute")}
        assert sorted(m["ip"] for m in room["members"]) == want["members"]
    office = got[system.office.uid]
    assert [m["visible"] for m in sorted(office["members"], key=lambda m: m["ip"])] == [True, False]
    assert got[system.lounge.uid]["volume"] == 22 and got[system.bedroom.uid]["mute"] is True
    assert client.get("/api/health").json()["rooms_seen"] == 4


def test_the_tripwire_does_catch_soco_calls(monitor, monkeypatch):
    """Control for the tests above: with the wire armed, a refresh (which does call SoCo) trips it."""
    hits = tripwire(monkeypatch)
    monitor.refresh()
    assert hits, "a refresh must trip the wire, otherwise the wire proves nothing"


# ---- a speaker changes IP or is renamed ---------------------------------------------------------------

def test_changed_ip_and_a_rename_are_picked_up_under_the_same_uid_within_one_discovery_cycle(system, monitor, clock):
    uid = system.kitchen.uid
    system.change_ip(system.kitchen, "192.168.0.99")
    system.kitchen.player_name = "Den"
    clock.advance(59)
    monitor.tick()
    assert (monitor.room(uid).ip, monitor.room(uid).name) == ("192.168.0.29", "Kitchen")     # not yet (control)
    clock.advance(1)
    monitor.tick()
    room = monitor.room(uid)
    assert (room.uid, room.ip, room.name) == (uid, "192.168.0.99", "Den")
    assert monitor.rooms_seen() == 4


def test_a_room_that_disappears_is_marked_offline_and_comes_back_when_it_is_seen_again(system, monitor, clock):
    kitchen = system.kitchen
    system.devices.remove(kitchen)                    # unplugged
    clock.advance(60)
    monitor.tick()
    assert monitor.room(kitchen.uid).online is False and monitor.rooms_seen() == 3
    system.devices.append(kitchen)                    # plugged back in
    clock.advance(60)
    monitor.tick()
    assert monitor.room(kitchen.uid).online is True and monitor.rooms_seen() == 4


def test_an_empty_discovery_marks_rooms_offline_but_keeps_their_last_known_details(system, monitor, clock):
    system.discover = lambda **k: set()
    monitor.discover = system.discover
    clock.advance(60)
    monitor.tick()
    assert monitor.rooms_seen() == 0
    assert {r["name"] for r in monitor.rooms()} == {"Office", "Bedroom", "Kitchen", "Lounge"}


# ---- /api/rooms/{uid}/state -----------------------------------------------------------------------------

def test_state_returns_cached_transport_state_track_position_and_source(system, monitor, tmp_path, monkeypatch):
    play_url(system.lounge, f"http://{OUR_IP}:8737/media/abc/a.flac")
    system.lounge.advance(12)
    monitor.refresh_group_of(system.lounge.uid)
    hits = tripwire(monkeypatch)
    r = api(monitor, tmp_path).get(f"/api/rooms/{system.lounge.uid}/state")
    assert hits == [] and r.status_code == 200
    st = r.json()
    assert st["state"] == "PLAYING" and st["source"] == "ours" and st["position"] == "0:00:12"
    assert st["track"]["title"] == "Song" and st["track"]["index"] == 0
    assert st["track"]["uri"].endswith("/media/abc/a.flac") and st["reachable"] is True
    assert st["uid"] == system.lounge.uid and st["volume"] == system.lounge.volume


@pytest.mark.parametrize("setup, expected", [
    (lambda d: play_url(d, f"http://{OUR_IP}:8737/media/abc/a.flac"), "ours"),
    (lambda d: play_url(d, "http://10.0.0.9:8737/media/abc/a.flac"), "other-host:10.0.0.9"),
    (lambda d: d.play_uri(f"x-rincon-mp3radio://{OUR_IP}:8737/live/t.mp3"), "ours"),
    (lambda d: d.play_uri(f"http://{OUR_IP}:8737/remote/tok"), "ours"),
    (lambda d: d.play_uri("x-sonos-vli:abc,spotify:xyz"), "other"),
    (lambda d: d.play_uri("http://radio.example.com/stream.mp3"), "other"),
    (lambda d: d.play_uri(f"http://{OUR_IP}:9999/media/abc/a.flac"), "other-host:" + OUR_IP),   # another port: not us
])
def test_source_classification(system, monitor, tmp_path, setup, expected):
    setup(system.bedroom)
    monitor.refresh_group_of(system.bedroom.uid)
    assert api(monitor, tmp_path).get(f"/api/rooms/{system.bedroom.uid}/state").json()["source"] == expected


def test_a_group_member_reports_its_coordinators_state_and_the_x_rincon_uri_is_not_ours(system, monitor, tmp_path):
    play_url(system.lounge, f"http://{OUR_IP}:8737/media/abc/a.flac")
    system.kitchen.join(system.lounge)
    system.lounge.play_uri(f"http://{OUR_IP}:8737/media/abc/a.flac")
    monitor.refresh()
    st = api(monitor, tmp_path).get(f"/api/rooms/{system.kitchen.uid}/state").json()
    assert st["uid"] == system.kitchen.uid and st["coordinator_uid"] == system.lounge.uid
    assert st["state"] == "PLAYING" and st["source"] == "ours"
    assert classify_source(f"x-rincon:{system.lounge.uid}", {OUR_IP}, 8737) == "other"


def test_unknown_room_is_404(monitor, tmp_path):
    assert api(monitor, tmp_path).get("/api/rooms/RINCON_NOPE/state").status_code == 404


def test_classify_source_edge_cases():
    hosts = {OUR_IP}
    for uri in (None, "", "garbage", "file:///x.mp3", "x-rincon:RINCON_1", "http://", "x-sonos-http:abc.mp3"):
        assert classify_source(uri, hosts, 8737) == "other", uri
    assert classify_source(f"HTTP://{OUR_IP.upper()}:8737/media/x", hosts, 8737) == "ours"
    assert classify_source(f"http://{OUR_IP}/media/x", hosts, 80) == "ours"
    assert classify_source(f"https://{OUR_IP}:8737/live/x.mp3", hosts, 8737) == "ours"


# ---- who gets polled ----------------------------------------------------------------------------------------

def reads(system):
    return {d.player_name + d.ip_address: sum(d.reads.values()) for d in system.devices}


def test_only_watched_rooms_or_rooms_playing_ours_are_polled(system, monitor, clock):
    base = reads(system)
    for _ in range(5):                                  # nobody watching, nothing of ours playing
        clock.advance(2)
        monitor.tick()
    assert reads(system) == base

    monitor.state(system.kitchen.uid)                   # a client asks about Kitchen
    clock.advance(2)
    monitor.tick()
    after = reads(system)
    assert after["Kitchen192.168.0.29"] > base["Kitchen192.168.0.29"]
    assert all(after[k] == base[k] for k in base if not k.startswith("Kitchen"))

    clock.advance(11)                                   # the watcher goes away
    monitor.tick()
    at_expiry = reads(system)["Kitchen192.168.0.29"]
    clock.advance(2)
    monitor.tick()
    assert reads(system)["Kitchen192.168.0.29"] == at_expiry


def test_a_room_playing_ours_is_polled_without_a_watcher_and_one_playing_something_else_is_not(system, monitor, clock):
    play_url(system.lounge, f"http://{OUR_IP}:8737/media/abc/a.flac")
    system.bedroom.play_uri("x-sonos-vli:abc")
    monitor.refresh_group_of(system.lounge.uid)
    monitor.refresh_group_of(system.bedroom.uid)
    clock.advance(11)                                   # watchers from the refresh calls have expired
    base = reads(system)
    clock.advance(2)
    monitor.tick()
    after = reads(system)
    assert after["Lounge192.168.0.26"] > base["Lounge192.168.0.26"]
    assert after["Bedroom192.168.0.31"] == base["Bedroom192.168.0.31"]


def test_subscribers_hear_about_changes_only(system, monitor, clock):
    seen = []
    monitor.subscribe(lambda uid, old, new: seen.append((uid, old["state"] if old else None, new["state"])))
    monitor.refresh_group_of(system.lounge.uid)          # nothing changed
    assert seen == []
    play_url(system.lounge, f"http://{OUR_IP}:8737/media/abc/a.flac")
    monitor.refresh_group_of(system.lounge.uid)
    assert seen == [(system.lounge.uid, "STOPPED", "PLAYING")]


def test_a_speaker_that_stops_answering_keeps_its_last_state_flagged_unreachable(system, monitor, monkeypatch):
    play_url(system.lounge, f"http://{OUR_IP}:8737/media/abc/a.flac")
    monitor.refresh_group_of(system.lounge.uid)
    monkeypatch.setattr(FakeSoCo, "get_current_transport_info", lambda self: (_ for _ in ()).throw(OSError("timeout")))
    st = monitor.refresh_group_of(system.lounge.uid)
    assert st["reachable"] is False and st["state"] == "PLAYING" and "timeout" in st["error"]
    assert monitor.state(system.lounge.uid)["reachable"] is False


def test_the_thread_discovers_keeps_going_and_stops(system):
    m = RoomMonitor(system.discover, poll_interval=0.05, discovery_interval=0.2)
    m.start()
    try:
        for _ in range(60):
            if m.rooms_seen() == 4:
                break
            time.sleep(0.05)
        assert m.rooms_seen() == 4
        system.kitchen.player_name = "Den"                  # a later discovery cycle must notice this
        for _ in range(60):
            if m.room(system.kitchen.uid).name == "Den":
                break
            time.sleep(0.05)
        assert m.room(system.kitchen.uid).name == "Den"
    finally:
        m.stop()
    assert m._thread is None


# ---- audit findings: locking, failing discovery, group changes, serialised polls ---------------------------------

def test_requests_are_not_blocked_while_a_discovery_is_talking_to_the_speakers(system, monitor, monkeypatch):
    import threading
    original = FakeSoCo.get_speaker_info

    def slow(self, refresh=False):
        time.sleep(0.3)
        return original(self, refresh)

    monkeypatch.setattr(FakeSoCo, "get_speaker_info", slow)
    t = threading.Thread(target=monitor.refresh)
    t.start()
    time.sleep(0.1)                                         # the refresh is now inside a slow SoCo call
    t0 = time.monotonic()
    assert len(monitor.rooms()) == 4 and monitor.rooms_seen() == 4 and monitor.state(system.lounge.uid)
    assert time.monotonic() - t0 < 0.15, "a request waited for the discovery"
    t.join()


def test_a_discovery_that_raises_is_retried_later_and_polling_goes_on(system, clock):
    calls = []

    def flaky():
        calls.append(clock())
        if len(calls) > 1:
            raise OSError(65, "No route to host")
        return system.discover()

    m = RoomMonitor(flaky, our_hosts=lambda: {OUR_IP}, clock=clock)
    m.refresh()
    assert m.rooms_seen() == 4
    clock.advance(60)
    m.tick()                                                # discovery due: it raises
    assert len(calls) == 2
    m.state(system.kitchen.uid)                             # a client watches Kitchen
    before = sum(system.kitchen.reads.values())
    for _ in range(8):                                      # ten seconds of ticks: no discovery storm, polling continues
        clock.advance(1.25)
        m.tick()
    assert len(calls) == 2 or len(calls) == 3                # at most the single retry after 10 s
    assert sum(system.kitchen.reads.values()) > before
    assert m.rooms_seen() == 4


def test_a_room_that_joins_another_group_is_seen_by_the_next_poll_not_the_next_discovery(system, monitor, clock):
    play_url(system.lounge, f"http://{OUR_IP}:8737/media/abc/a.flac")
    monitor.refresh_group_of(system.lounge.uid)
    system.kitchen.join(system.lounge)
    system.lounge.play_uri(f"http://{OUR_IP}:8737/media/abc/a.flac")
    st = monitor.refresh_group_of(system.kitchen.uid)                # polls Kitchen, finds it follows Lounge
    assert st["state"] == "PLAYING" and st["source"] == "ours"        # Lounge's reading, not Kitchen's x-rincon one
    assert monitor.room(system.kitchen.uid).coordinator_uid == system.lounge.uid
    assert monitor.state(system.kitchen.uid)["coordinator_uid"] == system.lounge.uid
    assert monitor.coordinator_device(system.kitchen.uid) is system.lounge
    assert {m["uid"] for m in monitor.room(system.lounge.uid).members} == {system.lounge.uid, system.kitchen.uid}


def test_a_coordinator_that_leaves_its_group_makes_the_monitor_rediscover_soon(system, monitor, clock):
    system.kitchen.join(system.lounge)
    monitor.refresh()
    assert monitor.room(system.kitchen.uid).coordinator_uid == system.lounge.uid
    system.lounge.unjoin()                                           # Kitchen takes over as coordinator
    monitor.state(system.lounge.uid)
    clock.advance(2)
    monitor.tick()                                                   # polls Lounge, sees Kitchen is no longer with it
    clock.advance(0.1)
    monitor.tick()                                                   # ... and rediscovers, well before 60 s
    assert monitor.room(system.kitchen.uid).coordinator_uid == system.kitchen.uid
    assert monitor.room(system.lounge.uid).coordinator_uid == system.lounge.uid


def test_two_polls_of_one_group_cannot_leave_the_older_reading_in_the_cache(system, monitor, monkeypatch):
    import threading
    play_url(system.lounge, f"http://{OUR_IP}:8737/media/abc/a.flac")
    original = FakeSoCo.get_current_transport_info
    first = threading.Event()

    def laggy(self):
        result = original(self)
        if self is system.lounge and not first.is_set():
            first.set()
            time.sleep(0.3)                                         # an old reading arrives late
        return result

    monkeypatch.setattr(FakeSoCo, "get_current_transport_info", laggy)
    a = threading.Thread(target=monitor.poll_group, args=(system.lounge.uid,))
    a.start()
    first.wait(2)
    system.lounge.pause()                                           # the world moves on
    monitor.poll_group(system.lounge.uid)
    a.join()
    assert monitor.state(system.lounge.uid)["state"] == "PAUSED_PLAYBACK"


def test_a_group_that_could_not_be_read_yet_has_a_full_state_and_is_not_a_404(system, clock, monkeypatch):
    m = RoomMonitor(system.discover, clock=clock)
    monkeypatch.setattr(FakeSoCo, "get_current_transport_info", lambda self: (_ for _ in ()).throw(OSError("timeout")))
    m.refresh()
    st = m.state(system.bedroom.uid)
    assert st is not None and st["reachable"] is False and st["state"] is None
    for key in ("track", "position", "duration", "volume", "mute", "source", "updated", "uid", "coordinator_uid"):
        assert key in st
    assert m.rooms_seen() == 0                                       # nothing answers, so nothing is "online"
    assert all(r["online"] is False for r in m.rooms())


def test_a_room_whose_own_poll_fails_is_listed_offline_until_it_answers_again(system, monitor, monkeypatch):
    original = FakeSoCo.get_current_transport_info
    monkeypatch.setattr(FakeSoCo, "get_current_transport_info",
                        lambda self: (_ for _ in ()).throw(OSError("timeout")) if self is system.kitchen else original(self))
    monitor.refresh_group_of(system.kitchen.uid)
    online = {r["name"]: r["online"] for r in monitor.rooms()}
    assert online == {"Kitchen": False, "Bedroom": True, "Lounge": True, "Office": True}
    monkeypatch.setattr(FakeSoCo, "get_current_transport_info", original)
    monitor.refresh_group_of(system.kitchen.uid)
    assert {r["name"]: r["online"] for r in monitor.rooms()}["Kitchen"] is True


def test_classify_source_survives_urls_with_bad_ports_or_brackets():
    for uri in ("http://h:99999/media/x", "http://h:abc/media/x", "http://[::1/media/x"):
        assert classify_source(uri, {OUR_IP}, 8737) == "other", uri


def test_a_listed_room_that_has_not_been_polled_yet_gives_an_empty_state_not_a_404(system, monitor):
    monitor._states.clear()                                          # the window between listing a room and polling it
    st = monitor.state(system.bedroom.uid)
    assert st is not None and st["state"] is None and st["reachable"] is None and st["error"] == "not polled yet"
    assert st["uid"] == system.bedroom.uid and "volume" in st and "track" in st


def test_a_grouped_room_that_goes_offline_does_not_make_the_monitor_rediscover_forever(system, clock):
    calls = []

    def counting():
        calls.append(clock())
        return system.discover()

    m = RoomMonitor(counting, our_hosts=lambda: {OUR_IP}, clock=clock)
    system.kitchen.join(system.bedroom)
    m.refresh()
    system.devices.remove(system.kitchen)                     # unplugged while grouped with Bedroom
    for _ in range(200):                                      # 200 simulated seconds
        clock.advance(1)
        m.tick()
    assert len(calls) <= 1 + 4, f"{len(calls)} discoveries in 200 s"      # the first, then about one a minute
    assert m.room(system.kitchen.uid).online is False and m.rooms_seen() == 3


def test_a_poll_whose_coordinator_answers_with_the_first_room_does_not_deadlock(system, monitor, monkeypatch):
    import threading
    from fakes import FakeGroup
    a, b = system.kitchen, system.bedroom
    monkeypatch.setattr(FakeSoCo, "group", property(lambda self: FakeGroup(system, b if self is a else a)))
    done = []
    t = threading.Thread(target=lambda: done.append(monitor.poll_group(a.uid)), daemon=True)
    t.start()
    t.join(3)
    assert not t.is_alive(), "poll_group deadlocked on its own lock"


def test_a_failed_discovery_after_a_regroup_still_backs_off_for_ten_seconds(system, monitor, clock):
    attempts = []

    def failing():
        attempts.append(1)
        raise OSError(65, "No route to host")

    monitor.discover = failing
    monitor._rediscover = True                                # a regroup asked for a rediscovery
    monitor.state(system.kitchen.uid)                         # and Kitchen is being watched
    before = sum(system.kitchen.reads.values())
    for _ in range(9):
        clock.advance(1)
        monitor.tick()
    assert len(attempts) == 1, f"{len(attempts)} discovery attempts in 9 s"
    assert sum(system.kitchen.reads.values()) > before        # polling carried on meanwhile


def test_a_pinned_room_is_polled_even_when_it_is_stopped_and_unwatched_until_it_is_unpinned(system, monitor, clock):
    monitor.refresh_group_of(system.bedroom.uid)
    clock.advance(11)                                                # the watcher from that refresh has expired; Bedroom is STOPPED
    clock.advance(2)
    base = reads(system)
    monitor.tick()
    assert reads(system)["Bedroom192.168.0.31"] == base["Bedroom192.168.0.31"]          # control: unpinned, it is left alone
    monitor.pin(system.bedroom.uid)
    clock.advance(2)
    monitor.tick()
    assert reads(system)["Bedroom192.168.0.31"] > base["Bedroom192.168.0.31"]
    monitor.pin(system.bedroom.uid)                                  # pins count: two things may need it
    monitor.unpin(system.bedroom.uid)
    mid = reads(system)["Bedroom192.168.0.31"]
    clock.advance(2)
    monitor.tick()
    assert reads(system)["Bedroom192.168.0.31"] > mid
    monitor.unpin(system.bedroom.uid)
    after = reads(system)["Bedroom192.168.0.31"]
    clock.advance(2)
    monitor.tick()
    assert reads(system)["Bedroom192.168.0.31"] == after


def test_topology_subscribers_hear_when_a_room_changes_group_and_not_otherwise(system, monitor, clock):
    heard = []
    monitor.subscribe_topology(lambda: heard.append(1))
    monitor.refresh_group_of(system.kitchen.uid)
    assert heard == []                                               # nothing moved
    system.kitchen.join(system.lounge)
    monitor.refresh_group_of(system.kitchen.uid)                     # a poll finds Kitchen follows Lounge
    assert len(heard) >= 1
    heard.clear()
    system.kitchen.unjoin()
    monitor.refresh()                                                # and a discovery finds it left again
    assert len(heard) >= 1


def test_a_live_stream_is_ours_in_both_forms_a_real_speaker_reports_its_uri():
    hosts = {OUR_IP}
    once = f"x-rincon-mp3radio://{OUR_IP}:8737/live/abc123.mp3"                  # the media URI
    twice = f"x-rincon-mp3radio://http://{OUR_IP}:8737/live/abc123.mp3"          # the track URI (seen on the Office, 2026-11-03)
    assert classify_source(once, hosts, 8737) == "ours" and classify_source(twice, hosts, 8737) == "ours"
    assert classify_source(f"x-rincon-mp3radio://http://other.example:8737/live/abc123.mp3", hosts, 8737) == "other-host:other.example"
    assert classify_source(f"x-rincon-mp3radio://http://{OUR_IP}:8737/elsewhere/abc.mp3", hosts, 8737) == "other"
    assert classify_source("x-rincon-mp3radio://http://", hosts, 8737) == "other"
