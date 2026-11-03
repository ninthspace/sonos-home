"""Playlists as extended M3U files in ~/Library/Application Support/sonos/playlists/ (FR12).

One UTF-8 `.m3u8` per playlist: `#EXTM3U`, then `#EXTINF:-1,<title>` and an absolute path per track. A playlist's id is
its file name without the extension. Names are made safe before they touch the file system, so a name can never point
outside the playlists directory. Reading tolerates plain `.m3u` files written by other apps (CRLF, a BOM, relative
paths, no EXTINF lines).
"""

from __future__ import annotations

import functools
import os
import re
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .library import Library, PathNotAllowed
from .media import AUDIO_EXTENSIONS

MAX_NAME_BYTES = 120
UNSAFE = re.compile(r'[\\/\x00-\x1f\x7f:*?"<>|]')


class PlaylistError(ValueError):
    status = 400


class NotFound(PlaylistError):
    status = 404


class Exists(PlaylistError):
    status = 409


def safe_id(name: str) -> str:
    """A file-system-safe playlist id from any text: no separators or control characters, no leading dots."""
    text = unicodedata.normalize("NFC", str(name)).strip()
    text = UNSAFE.sub("_", text).lstrip(". ").rstrip(". ")
    while len(text.encode("utf-8")) > MAX_NAME_BYTES:
        text = text[:-1]
    text = text.rstrip(". ")                       # the cut can leave a trailing space or dot
    return text or "Untitled"


LINE_BREAKS = "\n\r"                                  # the only characters our reader (and every M3U reader) ends a line on


def has_line_break(text: str) -> bool:
    return any(ch in LINE_BREAKS for ch in text)


@dataclass
class Track:
    path: str
    title: str
    available: bool = True
    reason: str = ""

    def as_dict(self) -> dict:
        return {"path": self.path, "title": self.title, "available": self.available, "reason": self.reason}


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        with self._lock:
            return fn(self, *a, **k)
    return wrapper


class PlaylistStore:
    def __init__(self, directory: Path, library: Library):
        self.directory = Path(directory)
        self.library = library
        self._lock = threading.RLock()                  # one read-modify-write at a time: two quick adds must both land

    def _index(self) -> dict[str, tuple[str, Path]]:
        """casefolded id -> (id, file) for every playlist in the folder. The default macOS volume ignores case, so two names that
        differ only in capitalisation are one playlist. A proper .m3u8 beats a plain .m3u of the same name."""
        found: dict[str, tuple[str, Path]] = {}
        if self.directory.is_dir():
            for f in sorted(self.directory.iterdir(), key=lambda p: p.name.casefold()):
                if f.suffix.lower() in (".m3u8", ".m3u") and not f.name.startswith(".") and f.is_file():
                    pid = safe_id(f.stem)
                    cur = found.get(pid.casefold())
                    if cur is None or (cur[1].suffix.lower() == ".m3u" and f.suffix.lower() == ".m3u8"):
                        found[pid.casefold()] = (pid, f)
        return found

    # ---- files ----------------------------------------------------------------------------------------
    def _file(self, pid: str, must_exist: bool = True) -> Path:
        if pid != safe_id(pid):
            raise NotFound(f"No playlist called {pid!r}")
        existing = self._index().get(pid.casefold())
        if existing is not None:
            return existing[1]
        if must_exist:
            raise NotFound(f"No playlist called {pid!r}")
        return self.directory / (pid + ".m3u8")

    def _read_raw(self, f: Path) -> list[tuple[str, str]]:
        text = f.read_bytes().decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        out, title = [], ""
        for line in text.split("\n"):             # only real line ends: Unicode separators are legal in file names
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                m = re.match(r"#EXTINF:[^,]*,(.*)$", line)
                title = m.group(1).strip() if m else title
                continue
            out.append((line, title or Path(line).stem))
            title = ""
        return out

    def _write(self, f: Path, tracks: list[tuple[str, str]]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        for path, _ in tracks:
            if has_line_break(path):
                raise PlaylistError(f"{path!r} has a line break in its name and can't be stored in a playlist")
        clean = lambda t: "".join(" " if ch in LINE_BREAKS else ch for ch in t)           # noqa: E731
        body = "#EXTM3U\n" + "".join(f"#EXTINF:-1,{clean(title)}\n{path}\n" for path, title in tracks)
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".tmp-", suffix=".m3u8")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
            target = f.with_suffix(".m3u8")
            os.replace(tmp, target)
            if f.suffix.lower() == ".m3u" and not (target.exists() and target.samefile(f) if f.exists() else False):
                f.unlink(missing_ok=True)               # a plain .m3u becomes a proper .m3u8 on its first edit
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ---- reading --------------------------------------------------------------------------------------
    def list(self) -> list[dict]:
        with self._lock:
            return [{"id": pid, "name": pid, "count": len(self._read_raw(f))} for pid, f in self._index().values()]

    def _check(self, path: str) -> tuple[bool, str]:
        if not os.path.isabs(path):
            return False, "not an absolute path"
        if Path(path).suffix.lower() not in AUDIO_EXTENSIONS:
            return False, "not an audio file"
        try:
            real = self.library.guard(path)
        except PathNotAllowed:
            return False, "outside the music folders"
        if self.library.music_root_of(real) is None:
            return False, "outside the music folders"
        if not real.is_file():
            return False, "file not found"
        return True, ""

    def get(self, pid: str) -> dict:
        with self._lock:
            f = self._file(pid)
            try:
                raw = self._read_raw(f)
            except FileNotFoundError:
                raise NotFound(f"No playlist called {pid!r}") from None
            pid = next((p for p, g in self._index().values() if g == f), pid)
        tracks = []
        for path, title in raw:
            ok, why = self._check(path)
            tracks.append(Track(path, title, ok, why))
        return {"id": pid, "name": pid, "tracks": [t.as_dict() for t in tracks]}

    def playable(self, pid: str) -> tuple[list[str], list[dict]]:
        """The paths that can be played, in order, and the ones that can't with their index and reason."""
        data = self.get(pid)
        ok = [t["path"] for t in data["tracks"] if t["available"]]
        bad = [{"index": i, "path": t["path"], "reason": t["reason"]} for i, t in enumerate(data["tracks"]) if not t["available"]]
        return ok, bad

    # ---- changing -------------------------------------------------------------------------------------
    @_locked
    def create(self, name: str, tracks: list[tuple[str, str]] | None = None) -> str:
        pid = safe_id(name)
        if pid.casefold() in self._index():
            raise Exists(f"A playlist called {pid!r} already exists")
        self._write(self.directory / (pid + ".m3u8"), tracks or [])
        return pid

    @_locked
    def rename(self, pid: str, new_name: str) -> str:
        f = self._file(pid)
        new = safe_id(new_name)
        other = self._index().get(new.casefold())
        if other is not None and other[1] != f:
            raise Exists(f"A playlist called {other[0]!r} already exists")
        target = self.directory / (new + ".m3u8")
        if target.exists() and not target.samefile(f):    # a file the index hides (same key as another) is still someone's playlist
            raise Exists(f"A file called {target.name!r} already exists in the playlist folder")
        if f.suffix.lower() == ".m3u8":
            if f.name != target.name:
                f.rename(target)                          # one atomic step: a plain rename, also for a change of capitalisation
        else:
            self._write(target, self._read_raw(f))        # a plain .m3u: convert it, then the old file goes
            f.unlink(missing_ok=True)
        return new

    @_locked
    def delete(self, pid: str) -> None:
        self._file(pid).unlink()

    @_locked
    def add(self, pid: str, entries: list[tuple[str, str]]) -> int:
        f = self._file(pid)
        tracks = self._read_raw(f) + entries
        self._write(f, tracks)
        return len(tracks)

    @_locked
    def remove(self, pid: str, index: int) -> None:
        f = self._file(pid)
        tracks = self._read_raw(f)
        if not 0 <= index < len(tracks):
            raise PlaylistError(f"The playlist has {len(tracks)} track(s); there is no track {index}")
        del tracks[index]
        self._write(f, tracks)

    @_locked
    def move(self, pid: str, src: int, dst: int) -> None:
        f = self._file(pid)
        tracks = self._read_raw(f)
        n = len(tracks)
        if not (0 <= src < n and 0 <= dst < n):
            raise PlaylistError(f"The playlist has {n} track(s); can't move {src} to {dst}")
        tracks.insert(dst, tracks.pop(src))
        self._write(f, tracks)
