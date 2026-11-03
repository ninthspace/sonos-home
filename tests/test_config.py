import os
import subprocess
from pathlib import Path

import pytest

from sonos_cli import config


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_no_file_gives_defaults(tmp_path):
    cfg = config.load(tmp_path / "missing.toml")
    assert cfg.music_folders == ()
    assert cfg.port == 8737
    assert cfg.room == "Office"
    assert cfg.ffmpeg is None and cfg.ffprobe is None


def test_no_file_means_no_music_folders_even_when_dropbox_and_music_exist(tmp_path, monkeypatch):
    (tmp_path / "Dropbox").mkdir()
    (tmp_path / "Music").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SONOS_CLI_CONFIG_DIR", raising=False)
    cfg = config.load()
    assert cfg.music_folders == ()
    assert cfg.path == tmp_path / "Library/Application Support/sonos/config.toml"


def test_partial_file_merges_with_defaults(tmp_path):
    folder = tmp_path / "music"
    folder.mkdir()
    cfg = config.load(write(tmp_path, f'port = 9000\nmusic_folders = ["{folder}"]\n'))
    assert cfg.port == 9000
    assert cfg.room == "Office"
    assert cfg.music_folders == (folder.resolve(),)


def test_invalid_toml_error_names_file_and_line(tmp_path):
    p = write(tmp_path, 'port = 9000\nroom = "Office\n')
    with pytest.raises(config.ConfigError) as e:
        config.load(p)
    msg = str(e.value)
    assert str(p) in msg
    assert "line 2" in msg


@pytest.mark.parametrize("text, fragment", [
    ('port = "x"\n', "port"),
    ('port = 70000\n', "port"),
    ('music_folders = "~/x"\n', "music_folders"),
    ('room = 3\n', "room"),
    ('ffmpeg = 4\n', "ffmpeg"),
])
def test_wrong_types_are_errors_naming_the_key(tmp_path, text, fragment):
    p = write(tmp_path, text)
    with pytest.raises(config.ConfigError) as e:
        config.load(p)
    assert fragment in str(e.value) and str(p) in str(e.value)


def test_symlinked_music_folder_is_stored_as_its_target(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "Dropbox"
    link.symlink_to(real)
    cfg = config.load(write(tmp_path, f'music_folders = ["{link}"]\n'))
    assert cfg.music_folders == (real.resolve(),)
    assert cfg.music_folders[0] != link


def make_exe(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return p


NO_BUNDLED = lambda name: None                                    # a machine whose package has no bundled copy


def test_binary_found_in_fallback_dir_when_path_is_empty(tmp_path):
    fake = make_exe(tmp_path / "homebrew-bin", "ffmpeg")
    assert config.find_binary("ffmpeg", path_env="", fallback_dirs=(str(tmp_path / "homebrew-bin"),), managed=NO_BUNDLED) == str(fake)


def test_binary_lookup_order_config_then_bundled_then_path_then_fallbacks(tmp_path):
    configured = make_exe(tmp_path / "cfg", "my-ffmpeg")
    bundled = make_exe(tmp_path / "bundled", "ffmpeg-bundled")
    on_path = make_exe(tmp_path / "path", "ffmpeg")
    first = make_exe(tmp_path / "f1", "ffmpeg")
    kw = dict(path_env=str(tmp_path / "path"), fallback_dirs=(str(tmp_path / "f1"),))
    managed = lambda name: str(bundled)
    assert config.find_binary("ffmpeg", str(configured), managed=managed, **kw) == str(configured)       # 1: the user's own choice
    assert config.find_binary("ffmpeg", None, managed=managed, **kw) == str(bundled)                     # 2: the copy that ships with the package
    assert config.find_binary("ffmpeg", None, managed=NO_BUNDLED, **kw) == str(on_path)                  # 3: PATH
    assert config.find_binary("ffmpeg", None, path_env="", fallback_dirs=(str(tmp_path / "f1"),), managed=NO_BUNDLED) == str(first)       # 4: Homebrew
    second = make_exe(tmp_path / "f2", "ffmpeg")
    assert config.find_binary("ffmpeg", None, path_env="", managed=NO_BUNDLED,
                              fallback_dirs=(str(tmp_path / "f1"), str(tmp_path / "f2"))) == str(first)                                 # 5: /usr/local, after the first folder
    assert second.exists()


def test_a_configured_binary_that_does_not_work_is_final_even_when_a_bundled_one_exists(tmp_path):
    bundled = make_exe(tmp_path / "bundled", "ffmpeg-bundled")
    assert config.find_binary("ffmpeg", str(tmp_path / "nope"), managed=lambda n: str(bundled)) is None


def test_binary_missing_everywhere_is_none(tmp_path):
    assert config.find_binary("ffmpeg", None, path_env="", fallback_dirs=(str(tmp_path),), managed=NO_BUNDLED) is None
    assert config.find_binary("ffmpeg", str(tmp_path / "nope"), managed=NO_BUNDLED) is None


def test_the_package_bundles_ffmpeg_but_not_ffprobe():
    ffmpeg = config.managed_binary("ffmpeg")
    assert ffmpeg and os.access(ffmpeg, os.X_OK) and "imageio_ffmpeg" in ffmpeg
    assert config.managed_binary("ffprobe") is None
    r = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.startswith("ffmpeg version")


def test_ffmpeg_resolves_with_nothing_on_the_path_and_nowhere_else(tmp_path):
    assert config.find_binary("ffmpeg", None, path_env="", fallback_dirs=()) == config.managed_binary("ffmpeg")      # no Homebrew needed


def test_a_missing_or_unrunnable_bundled_copy_is_none_not_an_error(monkeypatch, tmp_path):
    """The wheel's own `binaries/` folder, stood in for by a temporary one, holding a missing file, a plain file and a real executable."""
    import imageio_ffmpeg
    pkg = tmp_path / "imageio_ffmpeg"
    (pkg / "binaries").mkdir(parents=True)
    monkeypatch.setattr(imageio_ffmpeg, "__file__", str(pkg / "__init__.py"))
    gone = pkg / "binaries" / "ffmpeg-gone"
    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: str(gone))
    assert config.managed_binary("ffmpeg") is None                                                                     # not there
    plain = pkg / "binaries" / "ffmpeg-plain"
    plain.write_text("x")
    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: str(plain))
    assert config.managed_binary("ffmpeg") is None                                                                     # there, not executable
    good = make_exe(pkg / "binaries", "ffmpeg-good")
    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: str(good))
    assert config.managed_binary("ffmpeg") == str(good)                                                                # control: the same folder, runnable

    def boom():
        raise RuntimeError("no binary for this platform")
    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", boom)
    assert config.managed_binary("ffmpeg") is None


def test_the_bundled_ffmpeg_is_a_declared_dependency_hash_locked_for_macos():
    import tomllib
    root = Path(__file__).resolve().parent.parent
    deps = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    pinned = [d for d in deps if d.startswith("imageio-ffmpeg")]
    assert pinned and "==" in pinned[0]                                                                             # `uv tool install .` ignores uv.lock, so the version is pinned here
    lock = tomllib.loads((root / "uv.lock").read_text())
    pkg = next(p for p in lock["package"] if p["name"] == "imageio-ffmpeg")
    assert pinned[0].split("==")[1] == pkg["version"]                                                               # the lock and pyproject agree
    wheels = {w["url"].rsplit("/", 1)[-1]: w for w in pkg["wheels"]}
    mac = [n for n in wheels if "macosx" in n]
    assert any("arm64" in n for n in mac) and any("x86_64" in n for n in mac)
    assert all(wheels[n]["hash"].startswith("sha256:") and len(wheels[n]["hash"]) == 71 for n in mac)               # uv checks these on install


def test_relative_music_folder_is_resolved_against_the_config_file(tmp_path):
    (tmp_path / "tunes").mkdir()
    cfg = config.load(write(tmp_path, 'music_folders = ["tunes"]\n'))
    assert cfg.music_folders == ((tmp_path / "tunes").resolve(),)


def test_config_error_is_a_value_error_so_the_cli_reports_it(tmp_path, monkeypatch, capsys):
    from sonos_cli import cli
    (tmp_path / "config.toml").write_text('room = "Office\n')
    monkeypatch.setenv("SONOS_CLI_CONFIG_DIR", str(tmp_path))
    config._cached = None
    assert cli.main(["rooms"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("sonos: ") and str(tmp_path / "config.toml") in err


def test_media_probe_and_convert_run_the_resolved_binaries(tmp_path, monkeypatch):
    import subprocess
    from sonos_cli import media
    ffprobe = make_exe(tmp_path / "bin", "ffprobe")
    ffmpeg = make_exe(tmp_path / "bin", "ffmpeg")
    (tmp_path / "config.toml").write_text(f'ffprobe = "{ffprobe}"\nffmpeg = "{ffmpeg}"\n')
    monkeypatch.setenv("SONOS_CLI_CONFIG_DIR", str(tmp_path))
    config._cached = None
    (tmp_path / "x.wav").write_bytes(b"")
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd[0])
        raise RuntimeError("stop here")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        media.probe(tmp_path / "x.wav")
    info = media.AudioInfo(codec="pcm_s24le", sample_rate=96000, channels=2, bits=24, duration=1.0,
                           title=None, artist=None, album=None)
    monkeypatch.setattr(media, "CACHE_DIR", tmp_path / "cache")
    with pytest.raises(RuntimeError):
        media.convert(media.Track(tmp_path / "x.wav", info))
    assert seen == [str(ffprobe), str(ffmpeg)]


@pytest.mark.parametrize("entry", ["~nosuchuser/m", "a\\u0000b"])
def test_unusable_music_folder_entries_are_config_errors_naming_the_file(tmp_path, entry):
    p = write(tmp_path, f'music_folders = ["{entry}"]\n')
    with pytest.raises(config.ConfigError) as e:
        config.load(p)
    assert str(p) in str(e.value) and "music_folders" in str(e.value)


def test_symlink_loop_in_music_folders_is_a_config_error(tmp_path):
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    p = write(tmp_path, f'music_folders = ["{tmp_path / "loop"}"]\n')
    with pytest.raises(config.ConfigError) as e:
        config.load(p)
    assert str(p) in str(e.value) and "music_folders" in str(e.value)


def test_an_invalid_real_config_does_not_break_the_suite(tmp_path):
    """The autouse isolation fixture must neither read nor, on teardown, reload the developer's real file."""
    import subprocess
    import sys
    home = tmp_path / "home"
    d = home / "Library/Application Support/sonos"
    d.mkdir(parents=True)
    (d / "config.toml").write_text('room = "Office\n')      # invalid, as when it is half edited
    env = {k: v for k, v in __import__("os").environ.items() if k != "SONOS_CLI_CONFIG_DIR"}
    env["HOME"] = str(home)
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_media.py"],
                       cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-800:]
    assert "error" not in r.stdout.lower().replace("0 errors", "")


def test_an_ffmpeg_chosen_by_the_environment_is_not_the_bundled_one(monkeypatch, tmp_path):
    """imageio_ffmpeg honours IMAGEIO_FFMPEG_EXE and falls back to PATH; neither is "bundled" and neither may jump the PATH order."""
    import imageio_ffmpeg
    elsewhere = make_exe(tmp_path / "env", "ffmpeg")
    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: str(elsewhere))
    assert config.managed_binary("ffmpeg") is None
    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: str(tmp_path / "env" / "ffmpeg-link"))   # a symlink into another folder is not the wheel's file either
    (tmp_path / "env" / "ffmpeg-link").symlink_to(elsewhere)
    assert config.managed_binary("ffmpeg") is None
