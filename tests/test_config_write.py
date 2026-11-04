"""config.update_music_folders: change only the `music_folders` value of the config file (spec FR23, epic 01-10 Story 8)."""

import os
import stat
from pathlib import Path

import pytest

from sonos_cli import config


def cfg_file(tmp_path, text, newline=None):
    p = tmp_path / "config.toml"
    p.write_bytes(text.encode("utf-8") if newline is None else text.replace("\n", newline).encode("utf-8"))
    return p


# Each case: the file as the user wrote it, the entry added (a real folder is used for resolution), and the exact text expected back.
CASES = {
    "comments_and_order": (
        '# my config\nport = 8737   # fixed\n\nmusic_folders = [\n  "~/Dropbox/Music",  # main\n  # "/old"\n]\nroom = "Office"\n',
        '# my config\nport = 8737   # fixed\n\nmusic_folders = [\n  "~/Dropbox/Music",  # main\n  # "/old"\n  "{new}"\n]\nroom = "Office"\n'),
    "single_line_with_inline_comment": (
        'music_folders = ["~/Music"]   # one\nport = 9000\n',
        'music_folders = ["~/Music", "{new}"]   # one\nport = 9000\n'),
    "trailing_comma_multiline": (
        'music_folders = [\n    "/a",\n    "/b",\n]\n',
        'music_folders = [\n    "/a",\n    "/b",\n    "{new}",\n]\n'),
    "string_with_bracket_and_hash": (
        'music_folders = ["/odd]name#1"]\nroom = "A]B"\n',
        'music_folders = ["/odd]name#1", "{new}"]\nroom = "A]B"\n'),
    "no_trailing_newline": ('port = 8737\nroom = "Office"', 'port = 8737\nroom = "Office"\nmusic_folders = ["{new}"]\n'),
    "empty_file": ("", 'music_folders = ["{new}"]\n'),
    "only_a_comment": ("# nothing yet\n", '# nothing yet\nmusic_folders = ["{new}"]\n'),
    "other_keys_in_a_custom_order": (
        'extension_ids = ["abcdefgh"]\nroom = "Office"\nport = 9000\nmusic_folders = ["/a"]\npause_grace_minutes = 5\n',
        'extension_ids = ["abcdefgh"]\nroom = "Office"\nport = 9000\nmusic_folders = ["/a", "{new}"]\npause_grace_minutes = 5\n'),
    "key_before_first_table": (
        'port = 8737\n\n[extra]\nmusic_folders = ["/not/ours"]\n',
        'port = 8737\n\nmusic_folders = ["{new}"]\n[extra]\nmusic_folders = ["/not/ours"]\n'),
}


@pytest.mark.parametrize("name", list(CASES))
def test_only_the_music_folders_value_changes_in_every_form_of_file(tmp_path, name):
    before, expected = CASES[name]
    new = tmp_path / "new folder"
    new.mkdir()
    p = cfg_file(tmp_path, before)
    after = config.update_music_folders(p, add=new.resolve())
    assert p.read_text(encoding="utf-8") == expected.replace("{new}", str(new.resolve()))
    assert after[-1] == str(new.resolve())
    assert config.load(p).music_folders[-1] == new.resolve()                          # the service reads it back
    assert not list(tmp_path.glob(".config-*"))                                       # no temporary file left


def test_a_table_with_its_own_music_folders_key_is_left_alone(tmp_path):
    new = tmp_path / "n"
    new.mkdir()
    p = cfg_file(tmp_path, 'music_folders = ["/a"]\n\n[extra]\nmusic_folders = ["/not/ours"]\n')
    config.update_music_folders(p, add=new.resolve())
    text = p.read_text()
    assert '[extra]\nmusic_folders = ["/not/ours"]' in text and text.count(str(new.resolve())) == 1


def test_crlf_line_endings_survive(tmp_path):
    new = tmp_path / "n"
    new.mkdir()
    p = cfg_file(tmp_path, 'port = 8737\nmusic_folders = ["~/Music"]\nroom = "Office"\n', newline="\r\n")
    config.update_music_folders(p, add=new.resolve())
    raw = p.read_bytes()
    assert raw.startswith(b'port = 8737\r\nmusic_folders = ["~/Music", "') and raw.endswith(b'\r\nroom = "Office"\r\n')


def test_a_path_with_quote_backslash_and_newline_round_trips(tmp_path):
    weird = tmp_path / 'we"ird\\na\nme'
    weird.mkdir()
    p = cfg_file(tmp_path, "port = 8737\n")
    config.update_music_folders(p, add=weird.resolve())
    assert config.load(p).music_folders == (weird.resolve(),)


def test_existing_entries_keep_their_spelling_and_a_remove_deletes_every_spelling_of_the_folder(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Dropbox" / "Music").mkdir(parents=True)
    (tmp_path / "cfgdir" / "rel").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(home / "Dropbox" / "Music")
    monkeypatch.setenv("HOME", str(home))
    p = tmp_path / "cfgdir" / "config.toml"
    other = tmp_path / "other"
    other.mkdir()
    p.write_text(f'# keep me\nmusic_folders = ["~/Dropbox/Music", "{alias}", "{other}", "rel"]\nport = 9000\n')
    # an add leaves the user's own spellings as written
    extra = tmp_path / "extra"
    extra.mkdir()
    config.update_music_folders(p, add=extra.resolve())
    assert '"~/Dropbox/Music", ' in p.read_text() and "# keep me" in p.read_text()
    # removing the folder drops the `~` spelling and the symlink spelling, and nothing else
    after = config.update_music_folders(p, remove=(home / "Dropbox" / "Music").resolve())
    assert after == [str(other), "rel", str(extra.resolve())]
    assert p.read_text().startswith("# keep me\nmusic_folders = [") and "port = 9000" in p.read_text()
    # a relative entry is removed by the folder it resolves to (the config file's own folder)
    after = config.update_music_folders(p, remove=(tmp_path / "cfgdir" / "rel").resolve())
    assert after == [str(other), str(extra.resolve())]
    # the last folders: an empty list, not a missing key
    config.update_music_folders(p, remove=other.resolve())
    config.update_music_folders(p, remove=extra.resolve())
    assert "music_folders = []" in p.read_text() and config.load(p).music_folders == ()


def test_a_file_that_does_not_parse_is_not_touched_and_the_error_names_the_file(tmp_path):
    p = cfg_file(tmp_path, 'music_folders = ["/a"\nport = = 1\n')
    before = p.read_bytes()
    with pytest.raises(config.ConfigError) as e:
        config.update_music_folders(p, add=tmp_path)
    assert str(p) in str(e.value) and "line" in str(e.value).lower()
    assert p.read_bytes() == before and not list(tmp_path.glob(".config-*"))


def test_music_folders_of_the_wrong_type_is_refused_without_writing(tmp_path):
    p = cfg_file(tmp_path, 'music_folders = "not a list"\n')
    before = p.read_bytes()
    with pytest.raises(config.ConfigError):
        config.update_music_folders(p, add=tmp_path)
    assert p.read_bytes() == before


def test_a_missing_file_and_its_folder_are_created(tmp_path):
    p = tmp_path / "does" / "not" / "exist" / "config.toml"
    new = tmp_path / "n"
    new.mkdir()
    config.update_music_folders(p, add=new.resolve())
    assert p.read_text() == f'music_folders = ["{new.resolve()}"]\n'


def test_the_write_is_atomic_when_the_rename_fails_the_original_is_byte_identical_and_no_temporary_file_is_left(tmp_path, monkeypatch):
    p = cfg_file(tmp_path, '# keep\nmusic_folders = ["/a"]\n')
    before = p.read_bytes()
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        config.update_music_folders(p, add=tmp_path)
    assert p.read_bytes() == before and not list(tmp_path.glob(".config-*"))


def test_a_symlinked_config_is_written_through_and_the_mode_is_kept(tmp_path):
    real = tmp_path / "dotfiles" / "config.toml"
    real.parent.mkdir()
    real.write_text('music_folders = ["/a"]\n')
    os.chmod(real, 0o640)                                                              # not the 0o600 a temporary file is created with
    link = tmp_path / "cfg" / "config.toml"
    link.parent.mkdir()
    link.symlink_to(real)
    new = tmp_path / "n"
    new.mkdir()
    config.update_music_folders(link, add=new.resolve())
    assert link.is_symlink() and str(new.resolve()) in real.read_text()                # the link stays, its target changed
    assert stat.S_IMODE(real.stat().st_mode) == 0o640


def test_two_threads_adding_at_once_through_the_writer_alone_can_lose_an_update_so_the_service_serialises_them(tmp_path):
    """Documents why `folders_lock` exists in the service: the writer itself is a read-modify-write."""
    import threading
    p = cfg_file(tmp_path, "port = 8737\n")
    dirs = []
    for i in range(2):
        d = tmp_path / f"d{i}"
        d.mkdir()
        dirs.append(d.resolve())
    lock = threading.Lock()
    def add(d):
        with lock:
            config.update_music_folders(p, add=d)
    ts = [threading.Thread(target=add, args=(d,)) for d in dirs]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert set(config.load(p).music_folders) == set(dirs)


def test_when_tomlkit_cannot_insert_before_a_table_the_file_is_left_alone_with_a_config_error(tmp_path, monkeypatch):
    import tomlkit.container
    p = cfg_file(tmp_path, 'port = 1\n\n[extra]\nx = 1\n')
    before = p.read_bytes()
    monkeypatch.setattr(tomlkit.container.Container, "_insert_at", lambda *a, **k: (_ for _ in ()).throw(AttributeError("gone")), raising=False)
    with pytest.raises(config.ConfigError) as e:
        config.update_music_folders(p, add=tmp_path)
    assert str(p) in str(e.value) and p.read_bytes() == before
