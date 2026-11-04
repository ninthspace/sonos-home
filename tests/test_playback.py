"""Browse and play against the fake system (spec FR5, FR9, FR10)."""

import builtins
import time
import wave
from pathlib import Path

import pytest

from audio import make_tone
from fakes import FakeSystem
from sonos_cli import config, media, playback
from sonos_cli.library import Library
from sonos_cli.playback import Player

BASE = "http://10.0.0.5:8737"


def tiny_wav(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 16)
    return path


FAST_INFO = media.AudioInfo("pcm_s16le", 8000, 2, 16, 1.0, None, None, None)


@pytest.fixture
def system():
    return FakeSystem()


def build(tmp_path, system, files=(), probe=None, folders=True, **kw):
    music = tmp_path / "music"
    music.mkdir(exist_ok=True)
    for f in files:
        tiny_wav(music / f)
    cfg = config.Config(music_folders=(music.resolve(),) if folders else (), path=tmp_path / "config.toml")
    lib = Library.create(cfg.music_folders)
    player = Player(lib, cfg, system.by_uid, lambda coord: BASE, probe=probe, **kw)
    return music.resolve(), cfg, lib, player


@pytest.fixture
def mixed(tmp_path, system, monkeypatch):
    """A folder with a 16-bit WAV, a 24-bit/96k WAV, an MP3 and a file that is online-only."""
    music = tmp_path / "music"
    mix = music / "mix"
    make_tone(mix / "01-plain.wav")
    make_tone(mix / "02-hires.wav", rate=96000, bits=24)
    make_tone(mix / "03-song.mp3")
    tiny_wav(mix / "04-cloud.wav")
    monkeypatch.setattr(playback, "is_dataless", lambda p: p.name == "04-cloud.wav")
    return build(tmp_path, system) + (mix.resolve(),)


# ---- FR5 -----------------------------------------------------------------------------------------------

def test_a_mixed_folder_is_queued_in_name_order_with_mime_types_and_the_placeholder_skipped(system, mixed):
    music, cfg, lib, player, mix = mixed
    result = player.play(system.lounge.uid, [str(mix)])
    assert player.wait_idle()
    queue = system.lounge.queue
    assert [i.title for i in queue] == ["01-plain", "02-hires", "03-song"]
    assert [i.resources[0].uri for i in queue] == [lib.media_path(BASE, mix / "01-plain.wav"),
                                                    lib.media_path(BASE, mix / "02-hires.wav", converted=True),         # served as FLAC: named `.flac`
                                                    lib.media_path(BASE, mix / "03-song.mp3")]
    assert [i.resources[0].uri.rsplit(".", 1)[1] for i in queue] == ["wav", "flac", "mp3"]
    assert queue[1].resources[0].uri.endswith("02-hires.wav.flac")
    assert [i.resources[0].protocol_info for i in queue] == [
        "http-get:*:audio/wav:*", "http-get:*:audio/flac:*", "http-get:*:audio/mpeg:*"]    # hi-res is served as FLAC
    assert [s["path"] for s in result["skipped"]] == [str(mix / "04-cloud.wav")]
    assert "online-only" in result["skipped"][0]["reason"]
    assert result["skipped_total"] == 1 and result["truncated"] is None and result["total"] == 3
    assert system.lounge.get_current_track_info()["playlist_position"] == "1"
    assert system.lounge.transport_state == "PLAYING"


def test_a_hi_res_first_track_gets_the_flac_url_too(system, tmp_path):
    from audio import make_tone
    music = tmp_path / "music"
    make_tone(music / "01-master.wav", rate=96000, bits=24)
    make_tone(music / "02-plain.wav")
    _, cfg, lib, player = build(tmp_path, system)
    player.play(system.lounge.uid, [str(music)])
    assert player.wait_idle()
    uris = [i.resources[0].uri for i in system.lounge.queue]
    assert [u.rsplit(".", 1)[1] for u in uris] == ["flac", "wav"] and uris[0].endswith("01-master.wav.flac")     # the FIRST queued item


def test_the_placeholder_is_never_probed_or_opened(system, tmp_path, monkeypatch):
    seen = []
    music = tmp_path / "music"
    tiny_wav(music / "a.wav")
    tiny_wav(music / "cloud.wav")
    monkeypatch.setattr(playback, "is_dataless", lambda p: p.name == "cloud.wav")
    _, cfg, lib, player = build(tmp_path, system, probe=lambda p: seen.append(p.name) or FAST_INFO)
    player.play(system.lounge.uid, [str(music)])
    player.wait_idle()
    assert seen == ["a.wav"]


def test_urls_are_the_stable_media_urls(system, mixed):
    music, cfg, lib, player, mix = mixed
    player.play(system.lounge.uid, [str(mix / "01-plain.wav")])
    uri = system.lounge.queue[0].resources[0].uri
    assert uri == f"{BASE}/media/{library_root_id(music)}/mix/01-plain.wav"


def library_root_id(music):
    from sonos_cli.library import root_id
    return root_id(music)


def test_the_first_track_plays_before_the_rest_are_probed(system, tmp_path):
    def slow(path):
        time.sleep(1)
        return FAST_INFO

    music, cfg, lib, player = build(tmp_path, system, [f"t{i:02}.wav" for i in range(10)], probe=slow)
    t0 = time.monotonic()
    player.play(system.lounge.uid, [str(music)])
    took = time.monotonic() - t0
    assert took < 2
    assert system.lounge.queue_size == 1 and ("play_from_queue", 0) in system.lounge.calls
    assert player.wait_idle(30)
    assert [i.title for i in system.lounge.queue] == [f"t{i:02}" for i in range(10)]


def test_a_folder_over_500_tracks_queues_500_and_says_so(system, tmp_path):
    music, cfg, lib, player = build(tmp_path, system, [f"t{i:04}.wav" for i in range(520)], probe=lambda p: FAST_INFO)
    result = player.play(system.lounge.uid, [str(music)])
    assert player.wait_idle(30)
    assert system.lounge.queue_size == 500
    assert result["truncated"] == {"limit": 500, "found": 520}
    assert system.lounge.queue[499].title == "t0499"


def test_replace_clears_the_queue_first_and_append_does_not(system, tmp_path):
    music, cfg, lib, player = build(tmp_path, system, ["a.wav", "b.wav"], probe=lambda p: FAST_INFO)
    old = tiny_wav(tmp_path / "music" / "old.wav")
    player.play(system.lounge.uid, [str(old)])
    assert [i.title for i in system.lounge.queue] == ["old"]
    player.play(system.lounge.uid, [str(music / "a.wav")], mode="append")
    player.wait_idle()
    system.lounge.calls.clear()
    assert [i.title for i in system.lounge.queue] == ["old", "a"]
    assert ("play_from_queue", 1) not in system.lounge.calls and system.lounge.get_current_track_info()["title"] == "old"
    player.play(system.lounge.uid, [str(music / "b.wav")], mode="replace")
    player.wait_idle()
    assert [i.title for i in system.lounge.queue] == ["b"] and ("clear_queue",) in system.lounge.calls


def test_a_new_play_cancels_the_previous_ones_background_queueing(system, tmp_path):
    gate = []

    def gated(path):
        while not gate and path.name not in ("x.wav", "t0.wav"):     # t1..t4 wait until released
            time.sleep(0.01)
        return FAST_INFO

    music, cfg, lib, player = build(tmp_path, system, [f"t{i}.wav" for i in range(5)] + ["x.wav"], probe=gated)
    # a folder play: t0 starts at once, t1..t4 block in the background; then a second play replaces the queue
    first = player.play(system.lounge.uid, [str(music)])
    second = player.play(system.lounge.uid, [str(music / "x.wav")])
    gate.append(1)
    assert player.wait_idle(10)
    assert player.job(first["job"]).cancelled and not player.job(second["job"]).cancelled
    assert [i.title for i in system.lounge.queue] == ["x"]


def test_unreadable_files_are_skipped_with_a_reason_and_the_rest_play(system, tmp_path):
    def probe(path):
        if path.name.startswith("bad"):
            raise ValueError("ffprobe could not read it")
        return FAST_INFO

    music, cfg, lib, player = build(tmp_path, system, ["bad1.wav", "good1.wav", "bad2.wav", "good2.wav"], probe=probe)
    result = player.play(system.lounge.uid, [str(music)])
    player.wait_idle()
    assert [i.title for i in system.lounge.queue] == ["good1", "good2"]
    assert {Path(s["path"]).name for s in player.job(result["job"]).skipped + result["skipped"]} == {"bad1.wav", "bad2.wav"}


def test_play_on_a_group_member_queues_on_the_coordinator(system, tmp_path):
    music, cfg, lib, player = build(tmp_path, system, ["a.wav"], probe=lambda p: FAST_INFO)
    system.kitchen.join(system.lounge)
    player_for = Player(lib, cfg, lambda uid: system.by_uid(uid).coordinator, lambda c: BASE, probe=lambda p: FAST_INFO)
    player_for.play(system.kitchen.uid, [str(music / "a.wav")])
    assert system.lounge.queue_size == 1 and system.kitchen.queue_size == 0


def test_bad_requests(system, tmp_path):
    music, cfg, lib, player = build(tmp_path, system, ["a.wav"], probe=lambda p: FAST_INFO)
    (music / "notes.txt").write_text("x")
    for args, exc in ((("RINCON_NOPE", [str(music)]), playback.UnknownRoom),
                      ((system.lounge.uid, [str(music / "notes.txt")]), playback.NothingPlayable),
                      ((system.lounge.uid, [str(music / "missing.wav")]), playback.NothingPlayable),
                      ((system.lounge.uid, [str(music / "..")]), playback.OutsideMusicFolders),
                      ((system.lounge.uid, ["/etc/hosts"]), playback.OutsideMusicFolders)):
        with pytest.raises(exc):
            player.play(*args)
    with pytest.raises(playback.PlayError):
        player.play(system.lounge.uid, [str(music)], mode="sideways")
    assert system.lounge.queue_size == 0 and ("clear_queue",) not in system.lounge.calls


def test_an_outside_path_error_names_the_config_file(system, tmp_path):
    music, cfg, lib, player = build(tmp_path, system, ["a.wav"], probe=lambda p: FAST_INFO)
    with pytest.raises(playback.OutsideMusicFolders) as e:
        player.play(system.lounge.uid, ["/etc/hosts"])
    assert str(cfg.path) in str(e.value) and e.value.status == 403


# ---- no folders configured -------------------------------------------------------------------------------

def test_with_no_music_folders_browse_and_play_are_409_naming_the_config_and_nothing_is_queued(system, tmp_path):
    music, cfg, lib, player = build(tmp_path, system, folders=False, probe=lambda p: FAST_INFO)
    with pytest.raises(playback.NoMusicFolders) as e:
        playback.browse(lib, cfg, "")
    assert e.value.status == 409 and str(cfg.path) in str(e.value) and "music_folders" in str(e.value)
    with pytest.raises(playback.NoMusicFolders):
        player.play(system.lounge.uid, [str(tmp_path)])
    assert system.lounge.queue_size == 0 and system.lounge.calls == []


def test_control_the_same_requests_succeed_once_a_folder_is_configured(system, tmp_path):
    music, cfg, lib, player = build(tmp_path, system, ["a.wav"], probe=lambda p: FAST_INFO)
    assert playback.browse(lib, cfg, "")["entries"]
    player.play(system.lounge.uid, [str(music)])
    assert system.lounge.queue_size == 1


# ---- FR9 / FR10: browse ------------------------------------------------------------------------------------

def forbid_opening_audio(monkeypatch):
    real_open = builtins.open

    def guarded(file, *a, **k):
        if str(file).lower().endswith((".wav", ".mp3", ".flac", ".m4a", ".aiff")):
            raise AssertionError(f"opened audio file {file}")
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded)
    monkeypatch.setattr(media, "probe", lambda p: (_ for _ in ()).throw(AssertionError(f"probed {p}")))
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: (_ for _ in ()).throw(AssertionError(f"opened {self}")))
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(AssertionError(f"read {self}")))


def test_browse_lists_folders_before_files_by_name_without_opening_audio(system, tmp_path, monkeypatch):
    music, cfg, lib, _ = build(tmp_path, system, ["b.wav", "A.wav", "z/inner.wav", "Y/inner.wav", ".hidden.wav"])
    (music / "notes.txt").write_text("x")
    forbid_opening_audio(monkeypatch)
    top = playback.browse(lib, cfg, "")
    assert [e["name"] for e in top["entries"]] == [music.name] and top["entries"][0]["type"] == "dir"
    page = playback.browse(lib, cfg, str(music))
    assert [(e["type"], e["name"]) for e in page["entries"]] == [("dir", "Y"), ("dir", "z"), ("file", "A.wav"), ("file", "b.wav")]
    assert page["parent"] == "" and page["total"] == 4 and page["next_offset"] is None
    sub = playback.browse(lib, cfg, str(music / "Y"))
    assert sub["parent"] == str(music) and [e["name"] for e in sub["entries"]] == ["inner.wav"]


def test_browse_pages_at_200_entries(system, tmp_path):
    music, cfg, lib, _ = build(tmp_path, system, [f"t{i:03}.wav" for i in range(450)])
    p1 = playback.browse(lib, cfg, str(music))
    assert len(p1["entries"]) == 200 and p1["next_offset"] == 200 and p1["total"] == 450
    p2 = playback.browse(lib, cfg, str(music), offset=200)
    p3 = playback.browse(lib, cfg, str(music), offset=400)
    assert len(p2["entries"]) == 200 and len(p3["entries"]) == 50 and p3["next_offset"] is None
    names = [e["name"] for p in (p1, p2, p3) for e in p["entries"]]
    assert names == sorted(names) and len(set(names)) == 450
    assert len(playback.browse(lib, cfg, str(music), limit=5000)["entries"]) == 200      # the cap can't be raised


def test_browse_refuses_paths_outside_the_roots(system, tmp_path):
    music, cfg, lib, _ = build(tmp_path, system, ["a.wav"])
    for bad in ("/etc", str(music / ".."), str(tmp_path)):
        with pytest.raises(playback.OutsideMusicFolders):
            playback.browse(lib, cfg, bad)
    with pytest.raises(playback.NothingPlayable):
        playback.browse(lib, cfg, str(music / "a.wav"))


def test_a_2000_file_folder_lists_in_under_a_second_and_starts_playing_within_three(system, tmp_path, monkeypatch):
    music, cfg, lib, player = build(tmp_path, system, [f"t{i:04}.wav" for i in range(2000)], probe=lambda p: FAST_INFO)
    forbid_opening_audio(monkeypatch)
    t0 = time.monotonic()
    page = playback.browse(lib, cfg, str(music))
    assert time.monotonic() - t0 < 1 and len(page["entries"]) == 200 and page["total"] == 2000
    t0 = time.monotonic()
    player.play(system.lounge.uid, [str(music)])
    assert time.monotonic() - t0 < 3 and ("play_from_queue", 0) in system.lounge.calls
    assert player.wait_idle(30) and system.lounge.queue_size == 500


def test_folders_expand_in_case_insensitive_name_order_including_subfolders(system, tmp_path):
    names = ["b.wav", "A.wav", "c.wav", "sub/a2.wav", "Sub2/z.wav", "sub/B1.wav", "Zed.wav", "d10.wav", "d2.wav"]
    music, cfg, lib, player = build(tmp_path, system, names, probe=lambda p: FAST_INFO)
    player.play(system.lounge.uid, [str(music)])
    player.wait_idle()
    expected = [Path(n).stem for n in sorted(names, key=str.casefold)]       # by relative path, ignoring case
    assert [i.title for i in system.lounge.queue] == expected
    assert expected[0] == "A" and expected.index("a2") < expected.index("z")  # not plain ASCII order


# ---- audit findings: links out of the root, hidden folders, nothing touched before a track probes, concurrent plays ----

def test_a_link_out_of_the_music_folder_is_skipped_with_a_reason_and_the_rest_play(system, tmp_path):
    music, cfg, lib, player = build(tmp_path, system, ["a.wav", "c.wav", "d.wav"], probe=lambda p: FAST_INFO)
    outside = tiny_wav(tmp_path / "outside" / "secret.wav")
    (music / "b.wav").symlink_to(outside)
    result = player.play(system.lounge.uid, [str(music)])
    assert player.wait_idle()
    assert [i.title for i in system.lounge.queue] == ["a", "c", "d"]
    assert [Path(s["path"]).name for s in result["skipped"]] == ["b.wav"] and "outside" in result["skipped"][0]["reason"]
    assert not any("secret" in s["path"] for s in player.job(result["job"]).skipped)


def test_a_link_out_that_sorts_first_does_not_wipe_the_queue_or_break_the_play(system, tmp_path):
    music, cfg, lib, player = build(tmp_path, system, ["b.wav"], probe=lambda p: FAST_INFO)
    (music / "a.wav").symlink_to(tiny_wav(tmp_path / "outside" / "secret.wav"))
    old = tiny_wav(music / "old.wav")
    system.lounge.add_to_queue(player_item("PREVIOUS"))
    player.play(system.lounge.uid, [str(music / "b.wav")], mode="replace")
    assert [i.title for i in system.lounge.queue] == ["b"]
    system.lounge.clear_queue()
    system.lounge.add_to_queue(player_item("PREVIOUS"))
    result = player.play(system.lounge.uid, [str(music)])
    player.wait_idle()
    titles = [i.title for i in system.lounge.queue]
    assert "secret" not in titles and "a" not in titles and titles[0] == "b" and result["skipped"][0]["path"].endswith("a.wav")
    assert old.exists()


def player_item(title):
    from sonos_cli import player as p
    from sonos_cli.media import AudioInfo, Track
    return p.queue_item(Track(Path(f"/m/{title}.wav"), AudioInfo("pcm_s16le", 8000, 2, 16, 1.0, title, None, None)),
                        f"http://h/{title}.wav", 0)


def test_hidden_folders_are_not_played_just_as_browse_does_not_list_them(system, tmp_path):
    music, cfg, lib, player = build(tmp_path, system, ["alb/a.wav", ".dropbox.cache/old/z.wav", ".Trashes/t.wav", "alb/.hid.wav"],
                                    probe=lambda p: FAST_INFO)
    player.play(system.lounge.uid, [str(music)])
    player.wait_idle()
    assert [i.title for i in system.lounge.queue] == ["a"]
    assert [e["name"] for e in playback.browse(lib, cfg, str(music))["entries"]] == ["alb"]


def test_when_no_file_can_be_read_the_existing_queue_is_left_alone(system, tmp_path):
    def bad(path):
        raise ValueError("ffprobe could not read it")

    music, cfg, lib, player = build(tmp_path, system, ["a.wav", "b.wav"], probe=bad)
    system.lounge.add_to_queue(player_item("PREVIOUS"))
    system.lounge.calls.clear()
    with pytest.raises(playback.NothingPlayable):
        player.play(system.lounge.uid, [str(music)])
    assert [i.title for i in system.lounge.queue] == ["PREVIOUS"] and ("clear_queue",) not in system.lounge.calls


def test_a_new_play_cannot_be_followed_by_the_old_plays_batch(system, tmp_path, monkeypatch):
    from fakes import FakeSoCo
    original = FakeSoCo.add_multiple_to_queue

    def slow(self, items, container=None):
        time.sleep(0.3)
        return original(self, items, container)

    monkeypatch.setattr(FakeSoCo, "add_multiple_to_queue", slow)
    music, cfg, lib, player = build(tmp_path, system, [f"old{i:02}.wav" for i in range(20)] + ["new.wav"],
                                    probe=lambda p: FAST_INFO, batch=5)
    player.play(system.lounge.uid, [str(music / f"old{i:02}.wav") for i in range(20)])
    time.sleep(0.1)                                             # the first batch of 5 is in flight inside the speaker call
    player.play(system.lounge.uid, [str(music / "new.wav")])
    assert player.wait_idle(10)
    assert [i.title for i in system.lounge.queue] == ["new"]


def test_two_plays_at_once_on_one_room_leave_one_copy_not_two(system, tmp_path):
    import threading
    music, cfg, lib, player = build(tmp_path, system, [f"t{i:02}.wav" for i in range(60)], probe=lambda p: FAST_INFO, batch=7)
    threads = [threading.Thread(target=player.play, args=(system.lounge.uid, [str(music)])) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert player.wait_idle(10)
    assert [i.title for i in system.lounge.queue] == [f"t{i:02}" for i in range(60)]


def test_plays_through_two_members_of_one_group_cancel_each_other(system, tmp_path):
    music, cfg, lib, _ = build(tmp_path, system, [f"t{i:02}.wav" for i in range(60)] + ["one.wav"], probe=lambda p: FAST_INFO)
    system.kitchen.join(system.lounge)
    player = Player(lib, cfg, lambda uid: system.by_uid(uid).coordinator, lambda c: BASE, probe=lambda p: FAST_INFO, batch=7)
    player.play(system.kitchen.uid, [str(music)])                 # via the member
    player.play(system.lounge.uid, [str(music / "one.wav")])      # then via the coordinator
    assert player.wait_idle(10)
    assert [i.title for i in system.lounge.queue] == ["one"]


def test_a_speaker_error_is_a_502_not_a_crash(system, tmp_path, monkeypatch):
    from fakes import FakeSoCo
    music, cfg, lib, player = build(tmp_path, system, ["a.wav"], probe=lambda p: FAST_INFO)
    monkeypatch.setattr(FakeSoCo, "clear_queue", lambda self: (_ for _ in ()).throw(OSError("timed out")))
    with pytest.raises(playback.SpeakerError) as e:
        player.play(system.lounge.uid, [str(music)])
    assert e.value.status == 502 and "timed out" in str(e.value)


def test_shuffle_mixes_the_whole_selection_before_the_cap(system, tmp_path, monkeypatch):
    music, cfg, lib, player = build(tmp_path, system, [f"t{i:03}.wav" for i in range(520)], probe=lambda p: FAST_INFO)
    monkeypatch.setattr(playback.random, "shuffle", lambda items: items.reverse())
    result = player.play(system.lounge.uid, [str(music)], shuffle=True)
    player.wait_idle(30)
    titles = [i.title for i in system.lounge.queue]
    assert len(titles) == 500 and titles[0] == "t519" and "t019" not in titles and result["truncated"]["found"] == 520


def test_an_unreadable_folder_is_a_clean_error(system, tmp_path):
    music, cfg, lib, _ = build(tmp_path, system, ["locked/a.wav"])
    locked = music / "locked"
    locked.chmod(0)
    try:
        with pytest.raises(playback.FolderUnreadable) as e:
            playback.browse(lib, cfg, str(locked))
        assert e.value.status == 403 and "permission" in str(e.value)
    finally:
        locked.chmod(0o755)


def test_close_is_safe_to_call_twice(system, tmp_path):
    _, _, _, player = build(tmp_path, system)
    player.close()
    player.close()


def test_an_append_made_while_a_folder_is_still_queueing_goes_after_it_and_cuts_nothing_short(system, tmp_path):
    def slowish(path):
        time.sleep(0.1)
        return FAST_INFO

    music, cfg, lib, player = build(tmp_path, system, [f"t{i:02}.wav" for i in range(40)] + ["x.wav"], probe=slowish, batch=5)
    first = player.play(system.lounge.uid, [str(m) for m in sorted(music.glob("t*.wav"))])
    time.sleep(0.1)                                            # the first selection is still being queued
    second = player.play(system.lounge.uid, [str(music / "x.wav")], mode="append")
    assert player.wait_idle(10)
    assert [i.title for i in system.lounge.queue] == [f"t{i:02}" for i in range(40)] + ["x"]
    assert not player.job(first["job"]).cancelled and player.job(first["job"]).queued == 40
    assert player.job(second["job"]).queued == 1


def test_a_replace_that_fails_at_the_speaker_does_not_cancel_the_play_in_progress(system, tmp_path, monkeypatch):
    from fakes import FakeSoCo
    music, cfg, lib, player = build(tmp_path, system, [f"t{i:02}.wav" for i in range(30)] + ["x.wav"],
                                    probe=lambda p: (time.sleep(0.02), FAST_INFO)[1], batch=5)
    first = player.play(system.lounge.uid, [str(m) for m in sorted(music.glob("t*.wav"))])
    monkeypatch.setattr(FakeSoCo, "clear_queue", lambda self: (_ for _ in ()).throw(OSError("timed out")))
    with pytest.raises(playback.SpeakerError):
        player.play(system.lounge.uid, [str(music / "x.wav")])
    assert player.wait_idle(10)
    assert not player.job(first["job"]).cancelled and player.job(first["job"]).queued == 30


def test_a_first_track_that_needs_converting_is_converted_before_the_speaker_is_told_to_play(system, tmp_path):
    hires = media.AudioInfo("pcm_s24le", 96000, 2, 24, 5.0, None, None, None)
    events = []
    music = tmp_path / "music"
    tiny_wav(music / "a.wav")
    tiny_wav(music / "b.wav")
    cfg = config.Config(music_folders=(music.resolve(),), path=tmp_path / "config.toml")
    lib = Library.create(cfg.music_folders)
    player = Player(lib, cfg, system.by_uid, lambda c: BASE, probe=lambda p: hires,
                    convert=lambda track: events.append(("convert", track.path.name)) or Path("/x.flac"))
    original = type(system.lounge).play_from_queue
    type(system.lounge).play_from_queue = lambda self, i, start=True: (events.append(("play", i)), original(self, i, start))[1]
    try:
        player.play(system.lounge.uid, [str(music)])
        player.wait_idle()
    finally:
        type(system.lounge).play_from_queue = original
    assert events == [("convert", "a.wav"), ("play", 0)]            # only the first track, and before it plays


def test_a_conversion_that_fails_leaves_the_queue_alone_and_says_why(system, tmp_path):
    hires = media.AudioInfo("pcm_s24le", 96000, 2, 24, 5.0, None, None, None)
    music = tmp_path / "music"
    tiny_wav(music / "a.wav")
    cfg = config.Config(music_folders=(music.resolve(),), path=tmp_path / "config.toml")
    player = Player(Library.create(cfg.music_folders), cfg, system.by_uid, lambda c: BASE, probe=lambda p: hires,
                    convert=lambda t: (_ for _ in ()).throw(RuntimeError("ffmpeg failed")))
    system.lounge.add_to_queue(player_item("PREVIOUS"))
    with pytest.raises(playback.NothingPlayable, match="Couldn't convert a.wav"):
        player.play(system.lounge.uid, [str(music)])
    assert [i.title for i in system.lounge.queue] == ["PREVIOUS"]
