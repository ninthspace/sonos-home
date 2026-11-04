"""POST /api/rooms/{uid}/seek (spec FR24, epic 01-10 Story 3)."""

import pytest
from soco.data_structures import DidlMusicTrack, DidlResource
from soco.exceptions import SoCoUPnPException

from fakes import FakeSoCo
from starlette.testclient import TestClient

from test_play_api import BASE, post_json, setup_api


def track(title, duration):
    return DidlMusicTrack(title=title, parent_id="", item_id=title, resources=[DidlResource(
        uri=f"http://127.0.0.1:8737/media/x/{title}.flac", protocol_info="http-get:*:audio/flac:*", duration=duration)])


def playing(system, duration="0:04:00"):
    system.lounge.add_to_queue(track("t0", duration))
    system.lounge.play_from_queue(0)


def soft(client):
    """The same app, answering a handler's exception with a 500 instead of raising it in the test."""
    return TestClient(client.app, base_url=BASE, raise_server_exceptions=False)


def seek(client, uid, body):
    return post_json(client, f"/api/rooms/{uid}/seek", body)


def seeks(system):
    return [c for c in system.lounge.calls if c[0] == "seek"]


def test_a_seek_calls_the_coordinator_with_whole_seconds_rounded_down_and_the_state_reports_it(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    playing(system)
    r = seek(client, system.lounge.uid, {"position": 65.7})
    assert r.status_code == 200 and r.json() == {"position": 65}
    assert seeks(system) == [("seek", "0:01:05", None)]
    assert client.get(f"/api/rooms/{system.lounge.uid}/state").json()["position"] == "0:01:05"          # the cache was refreshed
    assert seek(client, system.lounge.uid, {"position": 0}).status_code == 200 and seeks(system)[-1] == ("seek", "0:00:00", None)
    assert seek(client, system.lounge.uid, {"position": 239}).status_code == 200 and seeks(system)[-1] == ("seek", "0:03:59", None)


def test_a_member_room_seeks_its_coordinator(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    system.kitchen.join(system.lounge)
    monitor.refresh()
    playing(system)
    r = seek(client, system.kitchen.uid, {"position": 30})
    assert r.status_code == 200 and seeks(system) == [("seek", "0:00:30", None)]
    assert not any(c[0] == "seek" for c in system.kitchen.calls)


def test_the_length_is_read_from_the_speaker_now_not_from_the_cache(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    playing(system, "0:04:00")
    monitor.refresh_group_of(system.lounge.uid)                       # the cache holds the 4 minute track
    system.lounge.queue[0] = track("short", "0:00:30")                # the speaker moved on to a 30 s track
    r = seek(client, system.lounge.uid, {"position": 100})
    assert r.status_code == 409 and seeks(system) == []               # judged against 30 s, not the cached 240 s


@pytest.mark.parametrize("body,status", [
    ({}, 400), ({"position": None}, 400), ({"position": True}, 400), ({"position": False}, 400), ({"position": "65"}, 400),
    ({"position": [65]}, 400), ({"position": -1}, 400), ({"position": -0.5}, 400),
    ({"position": 10 ** 400}, 400), ({"position": -(10 ** 400)}, 400),                          # too large for a float
    ({"position": 240}, 409), ({"position": 241}, 409), ({"position": 240.5}, 409), ({"position": 1e9}, 409), ({"position": 1e308}, 409),
])
def test_a_bad_position_is_refused_and_the_speaker_is_not_asked(tmp_path, body, status):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    playing(system)
    r = seek(soft(client), system.lounge.uid, body)
    assert r.status_code == status and "error" in r.json() and seeks(system) == []
    assert seek(client, system.lounge.uid, {"position": 239}).status_code == 200 and len(seeks(system)) == 1       # control: the last second is fine


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "[65]", "65", '"x"', "not json", ""])
def test_non_finite_numbers_and_non_object_bodies_are_400(tmp_path, raw):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    playing(system)
    payload = '{"position": %s}' % raw if raw in ("NaN", "Infinity", "-Infinity") else raw
    r = soft(client).post(f"/api/rooms/{system.lounge.uid}/seek", content=payload, headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:8737"})
    assert r.status_code == 400 and seeks(system) == []


@pytest.mark.parametrize("duration", ["0:00:00", "", None, "NOT_IMPLEMENTED"])
def test_a_track_with_no_length_is_refused_with_409(tmp_path, duration):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    playing(system, duration)
    r = seek(client, system.lounge.uid, {"position": 5})
    assert r.status_code == 409 and "no length" in r.json()["error"] and seeks(system) == []


def test_nothing_playing_and_an_unknown_room(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    assert seek(client, system.lounge.uid, {"position": 5}).status_code == 409           # an empty queue has no length
    assert seek(client, "RINCON_NOPE", {"position": 5}).status_code == 404


def test_when_the_speaker_refuses_the_seek_the_answer_is_502_with_its_reason_and_the_cache_is_unchanged(tmp_path, monkeypatch):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    playing(system)
    monitor.refresh_group_of(system.lounge.uid)
    before = client.get(f"/api/rooms/{system.lounge.uid}/state").json()
    monkeypatch.setattr(FakeSoCo, "seek", lambda self, position=None, track=None: (_ for _ in ()).throw(SoCoUPnPException("fake: seek unsupported", "701", "")))
    r = post_json(soft(client), f"/api/rooms/{system.lounge.uid}/seek", {"position": 30})
    assert r.status_code == 502 and "unsupported" in r.json()["error"]
    after = client.get(f"/api/rooms/{system.lounge.uid}/state").json()
    assert after["updated"] == before["updated"] and after["position"] == before["position"]


def test_a_non_json_content_type_is_refused_like_every_state_changing_request(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    playing(system)
    r = client.post(f"/api/rooms/{system.lounge.uid}/seek", content='{"position": 5}', headers={"Content-Type": "text/plain", "Origin": "http://127.0.0.1:8737"})
    assert r.status_code == 403 and seeks(system) == []                       # NFR6: state-changing requests must be application/json
