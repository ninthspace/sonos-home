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


# ---- a page's tracks as an S1 speaker reports them: tags for the playing and next track, the URL's file name for the rest ----

def _s1_item(title, creator, uri):
    from soco.data_structures import DidlMusicTrack, DidlResource
    return DidlMusicTrack(title=title, parent_id="", item_id="x", creator=creator or None,
                          resources=[DidlResource(uri=uri, protocol_info="http-get:*:audio/mpeg:*")])


def test_a_pages_tracks_the_speaker_has_not_read_yet_show_the_title_we_sent_not_the_proxy_file_name(tmp_path):
    from sonos_cli import remote
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    base = "http://127.0.0.1:8737/remote/TOKEN"
    rows = [("Slack Water", "SHTL", f"{base}/{remote.display_name('Slack Water', 'audio/mpeg')}"),     # read: the speaker's tags win
            ("Tideway.mp3", "", f"{base}/{remote.display_name('Tideway', 'audio/mpeg')}"),
            ("Neap.mp3", "", f"{base}/{remote.display_name('Neap', 'audio/mpeg')}"),
            ("Café – Ünïcode 100 日本.mp3", "", f"{base}/{remote.display_name('Café – Ünïcode 100% 日本', 'audio/mpeg')}"),      # "%" can't sit in a URL name
            ("Drift.flac", "", f"{base}/{remote.display_name('Drift', 'audio/flac')}")]
    for t, a, u in rows:
        system.lounge.add_to_queue(_s1_item(t, a, u))
    items = client.get(f"/api/rooms/{system.lounge.uid}/queue").json()["items"]
    assert [(i["title"], i["artist"]) for i in items] == [("Slack Water", "SHTL"), ("Tideway", ""), ("Neap", ""),
                                                          ("Café – Ünïcode 100 日本", ""), ("Drift", "")]


def test_queue_titles_that_are_not_the_proxy_file_name_are_left_alone(tmp_path):
    from sonos_cli import remote
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    proxy = f"http://127.0.0.1:8737/remote/TOKEN/{remote.display_name('Tideway', 'audio/mpeg')}"
    rows = [("Tideway.mp3", "", "http://127.0.0.1:8737/media/abc/Tideway.mp3"),     # a local file is not a proxy URL
            ("Tideway.mp3", "", "http://127.0.0.1:8737/remote/Tideway.mp3"),         # a proxy URL without a name segment
            ("Tide way", "", proxy),                                                    # the speaker has its own title
            ("Tideway.mp3", "Someone", proxy)]                                          # an artist came with it: the name's stem as the title
    for t, a, u in rows:
        system.lounge.add_to_queue(_s1_item(t, a, u))
    items = client.get(f"/api/rooms/{system.lounge.uid}/queue").json()["items"]
    assert [(i["title"], i["artist"]) for i in items] == [("Tideway.mp3", ""), ("Tideway.mp3", ""), ("Tide way", ""), ("Tideway", "Someone")]


@pytest.mark.parametrize("title,mime", [("Tideway", "audio/mpeg"), ("Tideway by SHTL", "audio/mpeg"), ("a.b.c", "audio/mp4"), ("x" * 300, "audio/mpeg"), ("", "audio/mpeg"), ("///", "audio/aac")])
def test_an_unread_label_reads_back_as_the_names_stem_for_any_title(title, mime):
    from sonos_cli import remote
    name = remote.display_name(title, mime)
    from urllib.parse import unquote
    t, a = remote.track_label(f"http://h:1/remote/T/{name}", unquote(name), "")
    shown = t + (f" by {a}" if a else "")                       # as the page shows it
    assert shown == unquote(name).rsplit(".", 1)[0] and not t.endswith((".mp3", ".m4a", ".aac"))


def test_the_jump_refreshes_the_cached_state_before_it_answers(tmp_path):
    """The page asks for the room's state the moment the jump returns (epic 01-10 Story 7): it must already show the new track."""
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    fill(system, music, client)
    monitor.refresh_group_of(system.lounge.uid)
    before = client.get(f"/api/rooms/{system.lounge.uid}/state").json()
    assert before["track"]["index"] == 0
    r = post_json(client, f"/api/rooms/{system.lounge.uid}/queue/jump", {"index": 2})
    assert r.status_code == 200
    after = client.get(f"/api/rooms/{system.lounge.uid}/state").json()          # no refresh in between
    assert after["track"]["index"] == 2 and after["updated"] > before["updated"]


# ---- docs/quick/08: a page track keeps its artist when its file has no artist tag ----------------------------------------

LABEL_CASES = [("Z0NE2", "Z0NE"), ("Tideway", "SHTL"), ("Song by Night", "Band"), ("Song", "Smith by Jones"), ("Café 日本", "Ünïcode"),
               ("Tideway", ""), ("Song by Night", ""), ("x" * 300, "Long"),
               ("日本語の曲名です" * 3, "アーティスト名前"),                   # the cap cuts into the artist: "… by ア" must not pass for it
               ("x" * 220, "Artist"),                                         # long but not cut: still at the cap's edge, so no artist
               ("x" * 210, "Bjork 🎵🎵")]                                      # cut to 227 encoded characters, mid-artist (audit round 2)


@pytest.mark.parametrize("title,artist", LABEL_CASES)
def test_a_page_track_is_labelled_from_its_url_whether_or_not_the_speaker_read_its_tags(title, artist):
    from urllib.parse import unquote
    from sonos_cli import remote
    name = remote.display_name(f"{title} by {artist}" if artist else title, "audio/mpeg")
    uri = f"http://h:1/remote/T/{name}"
    stem = unquote(name)[:-4]
    # not read yet: the speaker titles it by the file name; shown as the name's stem either way, and an artist only if whole
    t, a = remote.track_label(uri, unquote(name), "")
    assert t + (f" by {a}" if a else "") == stem
    assert a in ("", artist) or (not artist and f"{t} by {a}" == title)          # a page title with one " by " reads the same either way
    if artist and stem == f"{title} by {artist}" and (f"{title} by {artist}").count(" by ") == 1 and len(name) <= remote.NAME_CAP - 15:
        assert (t, a) == (title, artist)
    # read, with the tag title and no artist tag: the page's artist itself, or none, never a part of it
    t, a = remote.track_label(uri, title, "")
    assert t == title and a in ("", artist)
    if artist and stem == f"{title} by {artist}" and len(name) <= remote.NAME_CAP - 15:
        assert a == artist
    if len(name) > remote.NAME_CAP - 15:
        assert a == ""
    # read, with an artist tag: the speaker's artist wins
    assert remote.track_label(uri, title, "Tagged") == (title, "Tagged")


def test_other_urls_are_left_as_the_speaker_reported_them():
    from sonos_cli import remote
    for uri in ("http://h:1/media/abc/Z0NE2 by Z0NE.mp3", "http://h:1/remote/Z0NE2 by Z0NE.mp3", "", "x-rincon-mp3radio://h/live/1.mp3"):
        assert remote.track_label(uri, "Z0NE2", "") == ("Z0NE2", "")


def test_the_z0ne_queue_shows_the_artist_on_every_row(tmp_path):
    """The case the user saw: the release's MP3s tag only the album artist, so read tracks come back without one."""
    from sonos_cli import remote
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    base = "http://127.0.0.1:8737/remote/TOKEN"
    url = lambda t: f"{base}/{remote.display_name(f'{t} by Z0NE', 'audio/mpeg')}"
    rows = [("Z0NE2", "", url("Z0NE2")), ("Z0NE1", "", url("Z0NE1")), ("Z0NE3 by Z0NE.mp3", "", url("Z0NE3")),
            ("Tagged", "Someone", f"{base}/{remote.display_name('Tagged by Z0NE', 'audio/mpeg')}"),           # an artist tag wins
            ("Other", "", url("Z0NE4")),                                                                        # the tag title isn't the name's
            ("Local", "", "http://127.0.0.1:8737/media/abc/Local by Z0NE.mp3")]                                 # not a page track
    for t, a, u in rows:
        system.lounge.add_to_queue(_s1_item(t, a, u))
    items = client.get(f"/api/rooms/{system.lounge.uid}/queue").json()["items"]
    assert [(i["title"], i["artist"]) for i in items] == [("Z0NE2", "Z0NE"), ("Z0NE1", "Z0NE"), ("Z0NE3", "Z0NE"),
                                                          ("Tagged", "Someone"), ("Other", ""), ("Local", "")]


def test_a_name_the_cap_cut_never_gives_a_fragment_as_the_artist():
    """Many random titles and artists (ASCII, spaces, accents, CJK, emoji): whenever `display_name` had to cut the name, the
    label has no artist, read or unread; when it didn't, an artist is the whole artist or none."""
    import random
    from urllib.parse import quote, unquote
    from sonos_cli import remote
    rng = random.Random(8)
    alphabet = "abcxyz  éü日本🎵"
    for _ in range(4000):
        title = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 120))).strip() or "t"
        artist = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30))).strip() or "a"
        full = " ".join(f"{title} by {artist}".split())
        name = remote.display_name(full, "audio/mpeg")
        cut = unquote(name)[:-4] != full
        uri = f"http://h:1/remote/T/{name}"
        t_unread, a_unread = remote.track_label(uri, unquote(name), "")
        t_read, a_read = remote.track_label(uri, " ".join(title.split()), "")
        if cut:
            assert a_unread == "" and a_read == "", (full, unquote(name), a_unread, a_read)
        else:
            assert t_unread + (f" by {a_unread}" if a_unread else "") == full          # shown as the page named it
            assert a_read in ("", " ".join(artist.split())), (full, a_read)         # the page's whole artist or none
