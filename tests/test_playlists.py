"""Playlist storage and API (spec FR12, FR6)."""

import os
import time
from pathlib import Path

import pytest

from audio import make_tone
from sonos_cli import library as library_mod
from sonos_cli.library import Library
from sonos_cli.playlists import Exists, NotFound, PlaylistError, PlaylistStore, safe_id
from test_play_api import JSON, post_json, post_play, setup_api

UNICODE = "Café – Ünïcode 日本.wav"


@pytest.fixture
def world(tmp_path):
    music = tmp_path / "music"
    for n in ("a.wav", "b.wav", "c.wav", UNICODE):
        make_tone(music / n)
    lib = Library.create([music], [tmp_path / "playlists"])
    store = PlaylistStore(tmp_path / "playlists", lib)
    return type("W", (), dict(tmp=tmp_path, music=music.resolve(), lib=lib, store=store, dir=tmp_path / "playlists"))


def entry(w, name):
    return (str(w.music / name), Path(name).stem)


# ---- storage ------------------------------------------------------------------------------------------------

def test_every_edit_writes_a_utf8_m3u8_with_extm3u_and_absolute_paths(world):
    w = world
    pid = w.store.create("Evening")
    assert (w.dir / "Evening.m3u8").read_text(encoding="utf-8") == "#EXTM3U\n"
    w.store.add(pid, [entry(w, "a.wav"), entry(w, "b.wav"), entry(w, "c.wav")])
    text = (w.dir / "Evening.m3u8").read_text(encoding="utf-8")
    assert text.startswith("#EXTM3U\n") and f"#EXTINF:-1,a\n{w.music}/a.wav\n" in text
    assert all(Path(l).is_absolute() for l in text.splitlines() if not l.startswith("#"))
    w.store.move(pid, 2, 0)
    assert [t["title"] for t in w.store.get(pid)["tracks"]] == ["c", "a", "b"]
    w.store.remove(pid, 1)
    assert [t["title"] for t in w.store.get(pid)["tracks"]] == ["c", "b"]
    new = w.store.rename(pid, "Night")
    assert new == "Night" and not (w.dir / "Evening.m3u8").exists() and (w.dir / "Night.m3u8").exists()
    w.store.delete("Night")
    assert list(w.dir.iterdir()) == []


def test_a_playlist_with_a_non_ascii_track_reads_back_identically_after_a_restart(world):
    w = world
    pid = w.store.create("Ünï 日本")
    w.store.add(pid, [entry(w, UNICODE), entry(w, "a.wav")])
    again = PlaylistStore(w.dir, Library.create([w.music]))          # a new process: nothing remembered
    data = again.get(pid)
    assert [t["path"] for t in data["tracks"]] == [str(w.music / UNICODE), str(w.music / "a.wav")]
    assert data["tracks"][0]["title"] == "Café – Ünïcode 日本" and all(t["available"] for t in data["tracks"])
    assert [p["name"] for p in again.list()] == ["Ünï 日本"] and again.list()[0]["count"] == 2
    raw = (w.dir / "Ünï 日本.m3u8").read_bytes()
    assert raw.decode("utf-8") and b"\r" not in raw


@pytest.mark.parametrize("name", ["../../x", "a/b", "..", "../", "/etc/passwd", ".hidden", "a\x00b", "a\\b", "x" * 400, "   ", "", "CON:", "a\nb"])
def test_any_name_is_stored_under_a_safe_filename_inside_the_playlists_directory(world, name):
    w = world
    w.tmp.joinpath("sibling").mkdir()
    before = sorted(p.name for p in w.tmp.iterdir())
    pid = w.store.create(name)
    after = sorted(p.name for p in w.tmp.iterdir())
    assert after == sorted(before + (["playlists"] if "playlists" not in before else []))     # nothing beside the folder
    files = list(w.dir.iterdir())
    assert len(files) == 1 and files[0].parent == w.dir and files[0].name == pid + ".m3u8"
    assert "/" not in pid and "\\" not in pid and not pid.startswith(".") and len(pid.encode()) <= 120 and pid == safe_id(pid)
    assert w.store.get(pid)["tracks"] == []


def test_lookups_by_an_unsafe_id_are_not_found_never_a_path(world):
    w = world
    w.store.create("Real")                                  # the playlists directory exists, so ".." really leads somewhere
    (w.tmp / "secret.m3u8").write_text("#EXTM3U\n/etc/hosts\n")
    for bad in ("../secret", "..", "a/b", "/etc"):
        with pytest.raises(NotFound):
            w.store.get(bad)
        with pytest.raises(NotFound):
            w.store.delete(bad)
    assert (w.tmp / "secret.m3u8").exists()


def test_duplicates_and_bad_indexes_are_refused_and_change_nothing(world):
    w = world
    w.store.create("One")
    w.store.create("Two")
    w.store.add("One", [entry(w, "a.wav"), entry(w, "b.wav")])
    before = (w.dir / "One.m3u8").read_bytes()
    with pytest.raises(Exists):
        w.store.create("One")
    with pytest.raises(Exists):
        w.store.rename("One", "Two")
    with pytest.raises(NotFound):
        w.store.rename("Nope", "X")
    for call in (lambda: w.store.remove("One", 2), lambda: w.store.remove("One", -1), lambda: w.store.move("One", 0, 2),
                 lambda: w.store.move("One", -1, 0)):
        with pytest.raises(PlaylistError):
            call()
    assert (w.dir / "One.m3u8").read_bytes() == before
    assert w.store.rename("One", "One") == "One"


def test_plain_m3u_files_from_other_apps_are_read_and_become_m3u8_on_first_edit(world):
    w = world
    w.dir.mkdir()
    (w.dir / "Old.m3u").write_bytes(("﻿#EXTM3U\r\n#EXTINF:123,Named Track\r\n" + f"{w.music}/a.wav\r\n" + f"{w.music}/b.wav\r\nrelative/c.wav\r\n").encode("utf-8"))
    data = w.store.get("Old")
    assert [(t["title"], t["available"]) for t in data["tracks"]] == [("Named Track", True), ("b", True), ("c", False)]
    assert data["tracks"][2]["reason"] == "not an absolute path"
    assert [p["id"] for p in w.store.list()] == ["Old"]
    w.store.remove("Old", 2)
    assert (w.dir / "Old.m3u8").exists() and not (w.dir / "Old.m3u").exists()
    assert [t["title"] for t in w.store.get("Old")["tracks"]] == ["Named Track", "b"]


def test_a_failed_write_leaves_the_old_file_intact_and_no_temp_files(world, monkeypatch):
    w = world
    pid = w.store.create("Safe")
    w.store.add(pid, [entry(w, "a.wav")])
    before = (w.dir / "Safe.m3u8").read_bytes()
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        w.store.add(pid, [entry(w, "b.wav")])
    monkeypatch.undo()
    assert (w.dir / "Safe.m3u8").read_bytes() == before and [p.name for p in w.dir.iterdir()] == ["Safe.m3u8"]


# ---- playing -------------------------------------------------------------------------------------------------

def test_playing_skips_a_deleted_file_and_an_entry_outside_the_music_folders_and_reports_both(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    for n in ("a.wav", "b.wav", "gone.wav"):
        make_tone(music / n)
    pid = client.post("/api/playlists", json={"name": "Mix"}, headers=JSON).json()["id"]
    store = PlaylistStore(cfg.path.parent / "playlists", lib)
    store.add(pid, [(str(music / "a.wav"), "a"), (str(music / "gone.wav"), "gone"), ("/etc/hosts", "hosts"), (str(music / "b.wav"), "b")])
    (music / "gone.wav").unlink()
    r = post_json(client, f"/api/playlists/{pid}/play", {"room": system.lounge.uid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted((u["index"], u["reason"]) for u in body["unavailable"]) == [(1, "file not found"), (2, "not an audio file")]
    for _ in range(100):
        if system.lounge.queue_size == 2:
            break
        time.sleep(0.05)
    assert [i.title for i in system.lounge.queue] == ["a", "b"]
    shown = client.get(f"/api/playlists/{pid}").json()["tracks"]
    assert [t["available"] for t in shown] == [True, False, False, True]


def test_control_with_the_guard_removed_an_entry_outside_the_music_folders_is_queued(tmp_path, monkeypatch):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    outside = make_tone(tmp_path / "outside" / "x.wav")
    pid = client.post("/api/playlists", json={"name": "Mix"}, headers=JSON).json()["id"]
    PlaylistStore(cfg.path.parent / "playlists", lib).add(pid, [(str(outside), "x"), ("/etc/hosts", "hosts")])
    r = post_json(client, f"/api/playlists/{pid}/play", {"room": system.lounge.uid})
    assert r.status_code == 422 and system.lounge.queue_size == 0                  # guarded: nothing playable
    assert [(t["path"], t["reason"]) for t in client.get(f"/api/playlists/{pid}").json()["tracks"]] == [
        (str(outside), "outside the music folders"), ("/etc/hosts", "not an audio file")]
    monkeypatch.setattr(Library, "guard", lambda self, c: Path(c).resolve())
    monkeypatch.setattr(Library, "music_root_of", lambda self, real: self.music_roots[0])
    r = post_json(client, f"/api/playlists/{pid}/play", {"room": system.lounge.uid})
    assert r.status_code == 200, r.text
    assert [i.title for i in system.lounge.queue] == ["x"]     # the outside file got queued


def test_an_entry_outside_the_music_folders_with_a_music_extension_is_unavailable_in_the_listing(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    outside = make_tone(tmp_path / "outside" / "y.wav")
    pid = client.post("/api/playlists", json={"name": "Mix"}, headers=JSON).json()["id"]
    PlaylistStore(cfg.path.parent / "playlists", lib).add(pid, [(str(outside), "y")])
    assert client.get(f"/api/playlists/{pid}").json()["tracks"][0]["reason"] == "outside the music folders"


def test_a_playlist_with_nothing_playable_is_a_422_and_leaves_the_queue_alone(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    pid = client.post("/api/playlists", json={"name": "Empty"}, headers=JSON).json()["id"]
    r = post_json(client, f"/api/playlists/{pid}/play", {"room": system.lounge.uid})
    assert r.status_code == 422 and "Nothing in this playlist" in r.json()["error"] and system.lounge.calls == []


# ---- the API ------------------------------------------------------------------------------------------------

def test_the_api_creates_edits_lists_and_deletes(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    for n in ("a.wav", "b.wav"):
        make_tone(music / n)
    (music / "album").mkdir()
    make_tone(music / "album" / "z.wav")
    assert client.get("/api/playlists").json() == []
    pid = post_json(client, "/api/playlists", {"name": "../../Road trip"}).json()["id"]
    assert "/" not in pid
    r = post_json(client, f"/api/playlists/{pid}/add", {"paths": [str(music / "b.wav"), str(music / "a.wav"), str(music / "album")]})
    assert r.status_code == 200 and r.json()["added"] == 3 and r.json()["total"] == 3
    assert [t["title"] for t in client.get(f"/api/playlists/{pid}").json()["tracks"]] == ["b", "a", "z"]
    assert post_json(client, f"/api/playlists/{pid}/move", {"from": 2, "to": 0}).status_code == 200
    assert post_json(client, f"/api/playlists/{pid}/remove", {"index": 1}).status_code == 200
    assert [t["title"] for t in client.get(f"/api/playlists/{pid}").json()["tracks"]] == ["z", "a"]
    new = post_json(client, f"/api/playlists/{pid}/rename", {"name": "Home"}).json()["id"]
    assert [p["id"] for p in client.get("/api/playlists").json()] == [new]
    assert post_json(client, f"/api/playlists/{new}/delete", {}).status_code == 200
    assert client.get("/api/playlists").json() == [] and client.get(f"/api/playlists/{new}").status_code == 404


@pytest.mark.parametrize("path, body, status", [
    ("", {"name": ""}, 400), ("", {}, 400), ("", {"name": 5}, 400),
    ("/X/add", {"paths": []}, 400), ("/X/add", {"paths": [5]}, 400), ("/X/add", {"paths": ["/etc/hosts"]}, 403),
    ("/X/add", {"paths": ["/tmp/definitely/not/here.wav"]}, 403),
    ("/X/remove", {"index": "0"}, 400), ("/X/remove", {"index": 3}, 400), ("/X/move", {"from": 0}, 400), ("/X/move", {"from": 0, "to": 5}, 400),
    ("/X/rename", {}, 400), ("/X/play", {}, 400), ("/X/explode", {}, 404), ("/Nope/rename", {"name": "y"}, 404), ("/Nope/delete", {}, 404),
])
def test_bad_requests_get_json_errors_and_change_nothing(tmp_path, path, body, status):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    make_tone(music / "a.wav")
    post_json(client, "/api/playlists", {"name": "X"})
    post_json(client, "/api/playlists/X/add", {"paths": [str(music / "a.wav")]})
    before = (cfg.path.parent / "playlists" / "X.m3u8").read_bytes()
    r = post_json(client, "/api/playlists" + path, body)
    assert r.status_code == status and "error" in r.json(), r.text
    assert (cfg.path.parent / "playlists" / "X.m3u8").read_bytes() == before


def test_a_duplicate_name_is_a_409(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    post_json(client, "/api/playlists", {"name": "Same"})
    assert post_json(client, "/api/playlists", {"name": "Same"}).status_code == 409
    post_json(client, "/api/playlists", {"name": "Other"})
    assert post_json(client, "/api/playlists/Other/rename", {"name": "Same"}).status_code == 409


def test_an_audio_file_in_the_playlists_or_cache_folder_is_not_playable_from_a_playlist(world):
    """The guard allows those folders for the service's own use; a playlist may only play from the music folders."""
    w = world
    w.dir.mkdir(exist_ok=True)
    stray = make_tone(w.dir / "stray.wav")
    pid = w.store.create("P")
    w.store.add(pid, [(str(stray), "stray")])
    assert w.store.get(pid)["tracks"][0]["available"] is False
    assert w.store.get(pid)["tracks"][0]["reason"] == "outside the music folders"


# ---- audit round 1 ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["x" * 119 + " yyyy", "y" * 119 + ".z", "日" * 39 + " 本本", "z" * 200])
def test_a_name_that_gets_cut_to_the_length_limit_still_works(world, name):
    pid = world.store.create(name)
    assert pid == safe_id(pid) and len(pid.encode()) <= 120 and not pid.endswith((" ", "."))
    assert world.store.get(pid)["tracks"] == [] and world.store.rename(pid, name) == pid


def test_playlists_written_by_other_apps_with_awkward_names_are_listed_and_usable(world):
    import unicodedata
    w = world
    w.dir.mkdir()
    for fname in ("AC:DC.m3u", unicodedata.normalize("NFD", "Café.m3u"), "Mix.M3U", "Trailing .m3u"):
        (w.dir / fname).write_text(f"#EXTM3U\n{w.music}/a.wav\n", encoding="utf-8")
    ids = [p["id"] for p in w.store.list()]
    assert sorted(ids) == sorted(["AC_DC", "Café", "Mix", "Trailing"])
    for pid in ids:
        assert [t["available"] for t in w.store.get(pid)["tracks"]] == [True]            # every listed id can be opened
    w.store.add("AC_DC", [entry(w, "b.wav")])
    assert [t["title"] for t in w.store.get("AC_DC")["tracks"]] == ["a", "b"]
    w.store.rename("Café", "Cafe au lait")
    w.store.delete("Mix")
    assert sorted(p["id"] for p in w.store.list()) == sorted(["AC_DC", "Cafe au lait", "Trailing"])


def test_two_quick_edits_to_one_playlist_both_land(world):
    import threading
    w = world
    pid = w.store.create("Race")
    names = ["a.wav", "b.wav", "c.wav", UNICODE]
    threads = [threading.Thread(target=w.store.add, args=(pid, [entry(w, n)])) for n in names for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(w.store.get(pid)["tracks"]) == 20
    other = w.store.create("Other")
    ts = [threading.Thread(target=w.store.add, args=(other, [entry(w, "a.wav")])) for _ in range(10)]
    ts.append(threading.Thread(target=w.store.rename, args=(other, "Renamed")))
    [t.start() for t in ts]
    [t.join() for t in ts]
    survivors = [p for p in w.store.list() if p["id"] in ("Other", "Renamed")]
    assert len(survivors) == 1 and survivors[0]["count"] == 10


@pytest.mark.parametrize("ch", ["\r", "\n"])
def test_the_characters_that_end_a_line_in_titles_or_paths_cannot_inject_or_split_entries(world, ch):
    w = world
    pid = w.store.create("Inject")
    w.store.add(pid, [(str(w.music / "a.wav"), f"evil{ch}/etc/hosts"), (str(w.music / "b.wav"), "b")])
    tracks = w.store.get(pid)["tracks"]
    assert [t["path"] for t in tracks] == [str(w.music / "a.wav"), str(w.music / "b.wav")]
    assert tracks[0]["title"].startswith("evil")
    with pytest.raises(PlaylistError, match="line break"):
        w.store.add(pid, [(str(w.music / f"we{ch}ird.wav"), "x")])
    assert len(w.store.get(pid)["tracks"]) == 2


@pytest.mark.parametrize("ch", ["\x0b", "\x0c", "\x1c", "\x85", "\u2028", "\u2029"])
def test_other_separators_are_ordinary_filename_characters_and_never_split_an_entry(world, ch):
    w = world
    odd = make_tone(w.music / f"we{ch}ird.wav")
    pid = w.store.create("Odd")
    w.store.add(pid, [(str(odd), f"t{ch}/etc/hosts"), (str(w.music / "b.wav"), "b")])
    again = PlaylistStore(w.dir, Library.create([w.music]))
    tracks = again.get(pid)["tracks"]
    assert [t["path"] for t in tracks] == [str(odd), str(w.music / "b.wav")] and tracks[0]["available"] is True
    again.remove(pid, 1)                                                    # and such a playlist stays editable
    assert len(again.get(pid)["tracks"]) == 1


def test_a_file_with_a_line_break_in_its_name_is_skipped_by_the_add_api_with_a_reason(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    make_tone(music / "plain.wav")
    make_tone(music / "we\nird.wav")
    post_json(client, "/api/playlists", {"name": "L"})
    r = post_json(client, "/api/playlists/L/add", {"paths": [str(music)]})
    assert r.status_code == 200 and r.json()["added"] == 1 and "line break" in r.json()["skipped"][0]["reason"]
    assert [t["title"] for t in client.get("/api/playlists/L").json()["tracks"]] == ["plain"]


def test_a_rename_that_only_changes_capitalisation_is_allowed(world):
    w = world
    pid = w.store.create("road trip")
    w.store.add(pid, [entry(w, "a.wav")])
    assert w.store.rename(pid, "Road Trip") == "Road Trip"
    assert [p["id"] for p in w.store.list()] == ["Road Trip"] and w.store.get("Road Trip")["tracks"][0]["title"] == "a"
    assert sorted(f.name for f in w.dir.iterdir()) == ["Road Trip.m3u8"]


def test_files_from_other_apps_are_split_only_on_real_line_ends(world):
    w = world
    w.dir.mkdir()
    odd = w.music / "a.wav"
    (w.dir / "Mac.m3u").write_bytes(f"#EXTM3U\r#EXTINF:1,First\r{odd}\r{w.music}/b.wav\r".encode())          # classic Mac line ends
    assert [t["title"] for t in w.store.get("Mac")["tracks"]] == ["First", "b"]
    (w.dir / "Sep.m3u8").write_text(f"#EXTM3U\n#EXTINF:-1,t /etc/hosts\n{w.music}/c.wav\n", encoding="utf-8")
    tracks = w.store.get("Sep")["tracks"]
    assert len(tracks) == 1 and tracks[0]["path"] == str(w.music / "c.wav") and tracks[0]["title"].startswith("t")


def test_names_that_differ_only_in_capitalisation_are_one_playlist_and_never_overwrite_each_other(world):
    w = world
    w.store.create("Road Trip")
    w.store.add("Road Trip", [entry(w, "a.wav"), entry(w, "b.wav")])
    for variant in ("road trip", "ROAD TRIP", "Road trip"):
        with pytest.raises(Exists):
            w.store.create(variant)
    w.store.create("Other")
    with pytest.raises(Exists):
        w.store.rename("Other", "ROAD TRIP")
    assert [t["title"] for t in w.store.get("Road Trip")["tracks"]] == ["a", "b"] and sorted(f.name for f in w.dir.iterdir()) == ["Other.m3u8", "Road Trip.m3u8"]
    assert [t["title"] for t in w.store.get("road trip")["tracks"]] == ["a", "b"]           # found whatever the capitalisation


def test_an_m3u_and_an_m3u8_of_the_same_name_in_different_case_do_not_destroy_each_other(world):
    w = world
    w.dir.mkdir()
    (w.dir / "Mix.m3u8").write_text(f"#EXTM3U\n{w.music}/a.wav\n{w.music}/b.wav\n{w.music}/c.wav\n", encoding="utf-8")
    (w.dir / "mix.m3u").write_text(f"#EXTM3U\n{w.music}/a.wav\n", encoding="utf-8")
    assert [p["id"] for p in w.store.list()] == ["Mix"]
    w.store.add("mix", [entry(w, UNICODE)])
    assert len(w.store.get("Mix")["tracks"]) == 4
    assert (w.dir / "Mix.m3u8").exists() and (w.dir / "mix.m3u").exists()                  # the other app's file is left alone


def test_editing_then_deleting_an_uppercase_m3u_removes_it_for_good(world):
    w = world
    w.dir.mkdir()
    (w.dir / "Mix.M3U").write_text(f"#EXTM3U\n{w.music}/a.wav\n", encoding="utf-8")
    w.store.add("Mix", [entry(w, "b.wav")])
    assert sorted(f.name for f in w.dir.iterdir()) == ["Mix.m3u8"]
    w.store.delete("Mix")
    assert list(w.dir.iterdir()) == [] and w.store.list() == []


def test_a_failed_rename_leaves_the_playlist_exactly_where_it_was(world, monkeypatch):
    w = world
    pid = w.store.create("road trip")
    w.store.add(pid, [entry(w, "a.wav")])
    before = (w.dir / "road trip.m3u8").read_bytes()
    real_rename = Path.rename
    monkeypatch.setattr(Path, "rename", lambda self, t: (_ for _ in ()).throw(OSError("disk error")))
    with pytest.raises(OSError):
        w.store.rename(pid, "Road Trip")
    monkeypatch.setattr(Path, "rename", real_rename)
    assert sorted(f.name for f in w.dir.iterdir()) == ["road trip.m3u8"] and (w.dir / "road trip.m3u8").read_bytes() == before
    assert w.store.get("road trip")["tracks"][0]["title"] == "a"


def test_converting_a_plain_m3u_by_renaming_it_keeps_its_tracks(world):
    w = world
    w.dir.mkdir()
    (w.dir / "Old.m3u").write_text(f"#EXTM3U\n#EXTINF:1,Named\n{w.music}/a.wav\n", encoding="utf-8")
    assert w.store.rename("Old", "New") == "New"
    assert sorted(f.name for f in w.dir.iterdir()) == ["New.m3u8"] and w.store.get("New")["tracks"][0]["title"] == "Named"


def test_reading_a_playlist_that_vanishes_is_a_not_found_not_a_crash(world, monkeypatch):
    w = world
    w.store.create("Gone")
    monkeypatch.setattr(PlaylistStore, "_read_raw", lambda self, f: (_ for _ in ()).throw(FileNotFoundError(f)))
    with pytest.raises(NotFound):
        w.store.get("Gone")


def test_renaming_a_playlist_onto_a_name_a_hidden_file_already_holds_is_refused(world):
    # Finder stores "AC/DC" as "AC:DC.m3u8"; both names make the id "AC_DC", so the index shows only one of the two files.
    w = world
    w.dir.mkdir(exist_ok=True)
    (w.dir / "AC:DC.m3u8").write_text("#EXTM3U\n" + f"{w.music}/a.wav\n", encoding="utf-8")
    (w.dir / "AC_DC.m3u8").write_text("#EXTM3U\n" + f"{w.music}/a.wav\n{w.music}/b.wav\n", encoding="utf-8")
    shown = w.store.list()
    assert len(shown) == 1
    with pytest.raises(Exists):
        w.store.rename(shown[0]["id"], "AC_DC")
    assert sorted(p.name for p in w.dir.glob("*.m3u8")) == ["AC:DC.m3u8", "AC_DC.m3u8"]
    assert len((w.dir / "AC_DC.m3u8").read_text(encoding="utf-8").splitlines()) == 3


def test_a_change_of_capitalisation_alone_is_still_allowed(world):
    w = world
    w.store.create("evening")
    assert w.store.rename("evening", "Evening") == "Evening"
    assert [p.name for p in w.dir.glob("*.m3u8")] == ["Evening.m3u8"]
