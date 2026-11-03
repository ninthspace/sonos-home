"""The queue endpoints behind the UI's queue panel (spec FR19)."""

import time

import pytest

import time as _t
from audio import make_tone
from sonos_cli import player
from test_play_api import post_json, post_play, setup_api
from test_play_api import JSON  # noqa: F401


def fill(system, music, client, n=3):
    for i in range(n):
        make_tone(music / f"t{i}.wav")
    post_play(client, room=system.lounge.uid, paths=[str(music)])
    for _ in range(100):
        if system.lounge.queue_size == n:
            break
        time.sleep(0.05)


def test_the_queue_comes_back_with_the_current_index(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    fill(system, music, client)
    r = client.get(f"/api/rooms/{system.lounge.uid}/queue")
    assert r.status_code == 200
    body = r.json()
    assert [i["title"] for i in body["items"]] == ["t0", "t1", "t2"] and body["current"] == 0
    assert all(i["host"] is None for i in body["items"])
    system.lounge.next()
    monitor.refresh_group_of(system.lounge.uid)
    assert client.get(f"/api/rooms/{system.lounge.uid}/queue").json()["current"] == 1


def test_items_served_by_another_host_carry_that_host_as_a_label(tmp_path):
    from sonos_cli.media import AudioInfo, Track
    from pathlib import Path
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    info = AudioInfo("flac", 44100, 2, 16, 10.0, None, None, None)
    for title, url in (("mine", "http://127.0.0.1:8737/media/abc/a.flac"), ("theirs", "http://10.0.0.9:8737/media/abc/b.flac"),
                       ("spotify", "x-sonos-vli:abc")):
        system.lounge.add_to_queue(player.queue_item(Track(Path(f"/m/{title}.flac"), info), url, 0))
    items = client.get(f"/api/rooms/{system.lounge.uid}/queue").json()["items"]
    assert [(i["title"], i["host"]) for i in items] == [("mine", None), ("theirs", "10.0.0.9"), ("spotify", None)]


def test_jump_plays_that_item_and_clear_empties_the_queue(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    fill(system, music, client)
    system.lounge.calls.clear()
    r = post_json(client, f"/api/rooms/{system.lounge.uid}/queue/jump", {"index": 2})
    assert r.status_code == 200 and ("play_from_queue", 2) in system.lounge.calls
    assert client.get(f"/api/rooms/{system.lounge.uid}/queue").json()["current"] == 2
    assert post_json(client, f"/api/rooms/{system.lounge.uid}/queue/clear", {}).status_code == 200
    body = client.get(f"/api/rooms/{system.lounge.uid}/queue").json()
    assert body == {"items": [], "current": None} and system.lounge.queue_size == 0


def test_a_member_acts_on_the_coordinators_queue(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    fill(system, music, client)
    post_json(client, f"/api/rooms/{system.kitchen.uid}/group", {"into": system.lounge.uid})
    assert len(client.get(f"/api/rooms/{system.kitchen.uid}/queue").json()["items"]) == 3
    assert post_json(client, f"/api/rooms/{system.kitchen.uid}/queue/jump", {"index": 1}).status_code == 200
    assert ("play_from_queue", 1) in system.lounge.calls and ("play_from_queue", 1) not in system.kitchen.calls


@pytest.mark.parametrize("body, status", [({"index": -1}, 400), ({"index": "1"}, 400), ({"index": True}, 400), ({}, 400),
                                          ({"index": 9}, 422)])
def test_bad_jumps(tmp_path, body, status):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    fill(system, music, client)
    system.lounge.calls.clear()
    r = post_json(client, f"/api/rooms/{system.lounge.uid}/queue/jump", body)
    assert r.status_code == status and "error" in r.json() and ("play_from_queue", 9) not in system.lounge.calls
    assert all(c[0] != "play_from_queue" for c in system.lounge.calls)


def test_unknown_room_and_speaker_errors(tmp_path, monkeypatch):
    from fakes import FakeSoCo
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    assert client.get("/api/rooms/RINCON_NOPE/queue").status_code == 404
    assert post_json(client, "/api/rooms/RINCON_NOPE/queue/clear", {}).status_code == 404
    monkeypatch.setattr(FakeSoCo, "get_queue", lambda self, *a, **k: (_ for _ in ()).throw(OSError("timed out")))
    r = client.get(f"/api/rooms/{system.lounge.uid}/queue")
    assert r.status_code == 502 and "timed out" in r.json()["error"]


def tiny_items(system, n):
    from pathlib import Path
    from sonos_cli.media import AudioInfo, Track
    info = AudioInfo("flac", 44100, 2, 16, 10.0, None, None, None)
    for i in range(n):
        system.lounge.add_to_queue(player.queue_item(Track(Path(f"/m/{i:03}.flac"), info), f"http://127.0.0.1:8737/media/abc/{i:03}.flac", i))


def test_a_queue_longer_than_one_browse_page_is_read_in_full_and_the_current_item_is_found(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    tiny_items(system, 230)
    assert len(system.lounge.get_queue()) == 100                       # the fake pages like SoCo: one Browse, 100 items
    system.lounge.play_from_queue(150)
    monitor.refresh_group_of(system.lounge.uid)
    body = client.get(f"/api/rooms/{system.lounge.uid}/queue").json()
    assert len(body["items"]) == 230 and body["items"][229]["title"] == "229" and body["current"] == 150


def test_only_the_play_cap_is_read_from_an_enormous_queue(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    tiny_items(system, 650)
    assert len(client.get(f"/api/rooms/{system.lounge.uid}/queue").json()["items"]) == 500


def test_no_item_is_current_when_the_room_plays_something_that_is_not_the_queue_item(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    tiny_items(system, 3)
    system.lounge.play_from_queue(1)
    system.lounge.set_source("x-sonos-vli:abc")                          # Spotify Connect, say; the queue is left over
    system.lounge.queue_index = 0                                        # and the speaker reports track 1
    monitor.refresh_group_of(system.lounge.uid)
    assert client.get(f"/api/rooms/{system.lounge.uid}/queue").json()["current"] is None


def test_an_unreachable_speaker_gets_a_quick_json_502_not_a_blocked_read(tmp_path, monkeypatch):
    from fakes import FakeSoCo
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    monkeypatch.setattr(FakeSoCo, "get_current_transport_info", lambda self: (_ for _ in ()).throw(OSError("timeout")))
    monitor.refresh_group_of(system.lounge.uid)
    called = []
    monkeypatch.setattr(FakeSoCo, "get_queue", lambda self, *a, **k: called.append(1) or [])
    r = client.get(f"/api/rooms/{system.lounge.uid}/queue")
    assert r.status_code == 502 and "isn't answering" in r.json()["error"] and called == []


def test_the_playing_uri_matches_the_queue_item_whatever_the_percent_encoding():
    from sonos_cli.service import _same_uri
    assert _same_uri("http://h:8737/media/abc/01%20Intro.wav", "http://h:8737/media/abc/01 Intro.wav")
    assert _same_uri("x-file-cifs://nas/a%2Cb.flac", "x-file-cifs://nas/a%2cb.flac")
    assert _same_uri("http://h/x.mp3", "http://h/x.mp3")
    assert not _same_uri("", "") and not _same_uri("http://h/a.mp3", "http://h/b.mp3")


def test_a_queue_read_never_asks_for_more_than_the_play_cap_even_from_a_short_paging_speaker(tmp_path, monkeypatch):
    from fakes import FakeSoCo, FakeQueue
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    tiny_items(system, 650)
    real = FakeSoCo.get_queue

    def short_pages(self, start=0, max_items=100, **k):
        q = real(self, start, min(max_items, 99))
        q.total_matches = 650
        return q

    monkeypatch.setattr(FakeSoCo, "get_queue", short_pages)
    assert len(client.get(f"/api/rooms/{system.lounge.uid}/queue").json()["items"]) <= 500
