"""Service configuration: ~/Library/Application Support/sonos/config.toml.

There are no default music folders: the list is empty until the user adds one.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DEFAULT_PORT = 8737
DEFAULT_ROOM = "Office"
FALLBACK_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")

EXAMPLE_ENTRY = 'music_folders = ["~/Dropbox/Music"]'


class ConfigError(ValueError):
    """The config file can't be read; the message names the file (and line for TOML errors)."""


def config_dir() -> Path:
    override = os.environ.get("SONOS_CLI_CONFIG_DIR")
    return Path(override) if override else Path.home() / "Library/Application Support/sonos"


def config_path() -> Path:
    return config_dir() / "config.toml"


@dataclass(frozen=True)
class Config:
    music_folders: tuple[Path, ...] = ()
    port: int = DEFAULT_PORT
    room: str = DEFAULT_ROOM
    ffmpeg: str | None = None
    ffprobe: str | None = None
    extension_ids: tuple[str, ...] = ()     # Chrome extension IDs allowed to call the API (NFR6)
    pause_grace_minutes: float = 30.0       # how long a pause on our audio still keeps the Mac awake (FR17)
    path: Path = field(default_factory=config_path)


def load(path: Path | None = None) -> Config:
    """Read the config file. A missing file means defaults; an invalid one raises ConfigError."""
    path = path or config_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Config(path=path)
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror or e}") from e
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e

    defaults = Config(path=path)
    folders = data.get("music_folders", [])
    if not isinstance(folders, list) or not all(isinstance(f, str) for f in folders):
        raise ConfigError(f"{path}: music_folders must be a list of strings")
    port = data.get("port", defaults.port)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise ConfigError(f"{path}: port must be an integer from 1 to 65535")
    room = data.get("room", defaults.room)
    if not isinstance(room, str) or not room:
        raise ConfigError(f"{path}: room must be a non-empty string")
    grace = data.get("pause_grace_minutes", defaults.pause_grace_minutes)
    if not isinstance(grace, (int, float)) or isinstance(grace, bool) or not 0 <= grace < 1e6:
        raise ConfigError(f"{path}: pause_grace_minutes must be a number of minutes, 0 or more")
    ext_ids = data.get("extension_ids", [])
    if not isinstance(ext_ids, list) or not all(isinstance(x, str) for x in ext_ids):
        raise ConfigError(f"{path}: extension_ids must be a list of strings")
    for key in ("ffmpeg", "ffprobe"):
        if key in data and not isinstance(data[key], str):
            raise ConfigError(f"{path}: {key} must be a string")
    resolved: list[Path] = []
    for f in folders:
        try:
            folder = Path(f).expanduser()
            if not folder.is_absolute():          # relative to the config file, not the working directory (launchd runs in /)
                folder = path.parent / folder
            folder = folder.resolve()             # realpath: a symlinked folder is stored as its target
        except (RuntimeError, OSError, ValueError) as e:
            raise ConfigError(f"{path}: music_folders entry {f!r}: {e}") from e
        if folder not in resolved:
            resolved.append(folder)
    return Config(music_folders=tuple(resolved), port=port, room=room,
                  ffmpeg=data.get("ffmpeg"), ffprobe=data.get("ffprobe"),
                  extension_ids=tuple(ext_ids), pause_grace_minutes=float(grace), path=path)


def managed_binary(name: str) -> str | None:
    """The copy of a tool that ships with this package, or None. Only ffmpeg is bundled (the `imageio-ffmpeg` wheel; its build is
    GPL-licensed); ffprobe is not, and probing falls back to ffmpeg itself (media.probe)."""
    if name != "ffmpeg":
        return None
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:                                      # wheel missing for this platform, or its binary removed
        return None
    # get_ffmpeg_exe() also honours IMAGEIO_FFMPEG_EXE and falls back to whatever `ffmpeg` is on PATH: only the wheel's own file counts here
    ours = Path(imageio_ffmpeg.__file__).resolve().parent / "binaries"
    try:
        inside = Path(exe).resolve().parent == ours
    except OSError:
        return None
    return exe if inside and Path(exe).is_file() and os.access(exe, os.X_OK) else None


def find_binary(name: str, configured: str | None = None, path_env: str | None = None,
                fallback_dirs: tuple[str, ...] | None = None,
                managed: Callable[[str], str | None] | None = None) -> str | None:
    """Config path, then the copy bundled with this package, then PATH, /opt/homebrew/bin and /usr/local/bin. None if nowhere.

    A configured path that doesn't work is final (None): the user asked for that one."""
    if configured:
        candidate = Path(configured).expanduser()
        return str(candidate) if os.access(candidate, os.X_OK) and candidate.is_file() else None
    bundled = (managed or managed_binary)(name)           # looked up now, not at import, so a test or a caller can swap them
    if bundled:
        return bundled
    found = shutil.which(name, path=path_env if path_env is not None else os.environ.get("PATH", ""))
    if found:
        return found
    for d in (FALLBACK_BIN_DIRS if fallback_dirs is None else fallback_dirs):
        candidate = Path(d) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


_cached: Config | None = None


def get() -> Config:
    """The process-wide config, loaded once. Tests call reload()."""
    global _cached
    if _cached is None:
        _cached = load()
    return _cached


def reload() -> Config:
    global _cached
    _cached = None
    return get()


def binary(name: str) -> str:
    """Resolved ffmpeg/ffprobe path for this process; falls back to the bare name so errors name the tool."""
    return find_binary(name, getattr(get(), name)) or name
