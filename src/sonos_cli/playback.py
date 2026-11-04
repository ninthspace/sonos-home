"""Browse the music folders and queue what the user picks on a room (FR5, FR9, FR10).

This is the shared core (AD3): the service calls it, and the CLI's in-process fallback will too.
Listing never opens or probes an audio file. Playing probes only the first track before it starts;
the rest are probed and queued in the background, in order.
"""

from __future__ import annotations

import os
import random
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config as config_mod
from . import media
from .library import Library, PathNotAllowed, nfc
from .player import queue_item

SF_DATALESS = 0x40000000          # macOS: file content isn't on this disk (an online-only File Provider file)
MAX_TRACKS = 500
PAGE_SIZE = 200
MAX_REPORTED_SKIPS = 50


class PlayError(Exception):
    status = 400


class TooLarge(PlayError):
    status = 413


class NoMusicFolders(PlayError):
    status = 409

    def __init__(self, cfg: config_mod.Config):
        super().__init__(f"No music folders are configured. Add one to {cfg.path}, for example: "
                         f"{config_mod.EXAMPLE_ENTRY}")


class OutsideMusicFolders(PlayError):
    status = 403

    def __init__(self, path, cfg: config_mod.Config):
        super().__init__(f"{path} is outside the music folders. Add its folder to music_folders in {cfg.path}")


class UnknownRoom(PlayError):
    status = 404


class NothingPlayable(PlayError):
    status = 422


class NoBalance(PlayError):
    """The room's speaker has no balance setting (a single mono speaker)."""
    status = 422


class NameTaken(PlayError):
    status = 409


class FolderUnreadable(PlayError):
    status = 403


class NotSeekable(PlayError):
    """The room's current track can't be seeked: it has no length (radio, a live stream) or the position isn't inside it."""
    status = 409


class NotThisMac(PlayError):
    """Changing or browsing the music folders is allowed only from the Mac itself (FR23)."""
    status = 403


class FolderRefused(PlayError):
    """A folder that can't be added: it doesn't exist, isn't a folder or can't be read."""
    status = 422


class FolderConflict(PlayError):
    """A folder that is already configured or overlaps one, or a config file that can't be changed."""
    status = 409


class UnknownFolder(PlayError):
    status = 404


class SpeakerError(PlayError):
    status = 502


def is_dataless(path: Path) -> bool:
    try:
        return bool(getattr(path.stat(), "st_flags", 0) & SF_DATALESS)
    except OSError:
        return False


# ---- browsing ------------------------------------------------------------------------------------------

def browse(library: Library, cfg: config_mod.Config, path: str = "", offset: int = 0, limit: int = PAGE_SIZE) -> dict:
    if not library.music_roots:
        raise NoMusicFolders(cfg)
    limit = max(1, min(limit, PAGE_SIZE))
    offset = max(0, offset)
    if not path:
        entries = [{"name": r.name or str(r), "path": str(r), "type": "dir"} for r in library.music_roots]
        return _page("", None, entries, offset, limit)
    try:
        real = library.guard(path)
    except PathNotAllowed:
        raise OutsideMusicFolders(path, cfg) from None
    if library.music_root_of(real) is None or not real.is_dir():
        raise OutsideMusicFolders(path, cfg) if library.music_root_of(real) is None else NothingPlayable(f"{path} is not a folder")
    dirs: list[dict] = []
    files: list[dict] = []
    try:
        with os.scandir(real) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir():                           # a directory stat; no audio file is opened or probed
                        dirs.append({"name": e.name, "path": str(real / e.name), "type": "dir"})
                    elif Path(e.name).suffix.lower() in media.AUDIO_EXTENSIONS and e.is_file():
                        st = e.stat()
                        files.append({"name": e.name, "path": str(real / e.name), "type": "file", "size": st.st_size,
                                      "dataless": bool(getattr(st, "st_flags", 0) & SF_DATALESS)})
                except OSError:
                    continue                                 # vanished between listing and stat
    except PermissionError:
        raise FolderUnreadable(f"{path} can't be read (permission denied)") from None
    except OSError as e:
        raise FolderUnreadable(f"{path} can't be read: {e.strerror or e}") from None
    key = lambda x: nfc(x["name"]).casefold()               # noqa: E731
    entries = sorted(dirs, key=key) + sorted(files, key=key)
    root = library.music_root_of(real)
    parent = "" if real == root else str(real.parent)
    return _page(str(real), parent, entries, offset, limit)


def _page(path: str, parent: str | None, entries: list[dict], offset: int, limit: int) -> dict:
    end = offset + limit
    return {"path": path, "parent": parent, "entries": entries[offset:end], "offset": offset,
            "total": len(entries), "next_offset": end if end < len(entries) else None}


# ---- expanding what to play ------------------------------------------------------------------------------

def expand(library: Library, cfg: config_mod.Config, paths: list[str], max_tracks: int = MAX_TRACKS,
           shuffle: bool = False):
    """Files in play order, what was skipped (with reasons), and a truncation note if the cap bit."""
    if not library.music_roots:
        raise NoMusicFolders(cfg)
    files: list[Path] = []
    skipped: list[dict] = []
    skipped_total = 0

    def skip(path, reason: str) -> None:
        nonlocal skipped_total
        skipped_total += 1
        if len(skipped) < MAX_REPORTED_SKIPS:
            skipped.append({"path": str(path), "reason": reason})

    for raw in paths:
        try:
            real = library.guard(raw)
        except PathNotAllowed:
            raise OutsideMusicFolders(raw, cfg) from None
        if library.music_root_of(real) is None:
            raise OutsideMusicFolders(raw, cfg)
        if real.is_dir():
            candidates = sorted((p for p in real.rglob("*") if _wanted(p, real)),
                                key=lambda p: nfc(str(p.relative_to(real))).casefold())
        elif real.is_file():
            if real.suffix.lower() not in media.AUDIO_EXTENSIONS:
                raise NothingPlayable(f"Not a supported audio file: {raw}")
            candidates = [real]
        else:
            raise NothingPlayable(f"No such file or folder: {raw}")
        for p in candidates:
            try:                                             # a symlink inside a folder may lead outside the music folders
                library.guard(p)
                if library.music_root_of(p.resolve()) is None:
                    raise PathNotAllowed(str(p))
            except PathNotAllowed:
                skip(p, "a link that leads outside the music folders")
                continue
            if is_dataless(p):
                skip(p, "online-only (not downloaded to this Mac)")
                continue
            files.append(p)
    found = len(files)
    if shuffle:
        random.shuffle(files)                                # the whole selection, then the cap
    truncated = {"limit": max_tracks, "found": found} if found > max_tracks else None
    return files[:max_tracks], {"items": skipped, "total": skipped_total}, truncated


def _wanted(p: Path, root: Path) -> bool:
    """An audio file, not inside a hidden folder (`.dropbox.cache`, `.Trashes`…) — the same view as browse."""
    if p.suffix.lower() not in media.AUDIO_EXTENSIONS:
        return False
    if any(part.startswith(".") for part in p.relative_to(root).parts):
        return False
    return p.is_file()


# ---- playing ---------------------------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    room: str
    total: int
    queued: int = 0
    skipped: list[dict] = field(default_factory=list)
    done: bool = False
    cancelled: bool = False
    finished: threading.Event = field(default_factory=threading.Event, repr=False)

    def finish(self) -> None:
        self.done = True
        self.finished.set()

    def as_dict(self) -> dict:
        return {"id": self.id, "room": self.room, "total": self.total, "queued": self.queued,
                "skipped": self.skipped, "done": self.done, "cancelled": self.cancelled}


class Player:
    def __init__(self, library: Library, cfg: config_mod.Config, coordinator_for: Callable[[str], object],
                 base_url_for: Callable[[object], str], probe: Callable[[Path], media.AudioInfo] | None = None,
                 convert: Callable[[media.Track], Path] | None = None,
                 max_tracks: int = MAX_TRACKS, workers: int = 8, batch: int = 20):
        self.library = library
        self.cfg = cfg
        self.coordinator_for = coordinator_for
        self.base_url_for = base_url_for
        self.probe = probe or media.probe
        self.convert = convert or media.convert
        self.max_tracks = max_tracks
        self.batch = batch
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe")
        self._background = ThreadPoolExecutor(max_workers=2, thread_name_prefix="queue")
        self._jobs: dict[str, Job] = {}
        self._by_room: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._room_locks: dict[str, threading.RLock] = {}

    def job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def play(self, room_uid: str, paths: list[str], mode: str = "replace", shuffle: bool = False) -> dict:
        if mode not in ("replace", "append"):
            raise PlayError("mode must be 'replace' or 'append'")
        try:
            coordinator = self.coordinator_for(room_uid)
        except KeyError:
            raise UnknownRoom(f"No such room: {room_uid}") from None
        files, skipped, truncated = expand(self.library, self.cfg, paths, self.max_tracks, shuffle)
        if not files:
            raise NothingPlayable("Nothing playable: " + ("every file is online-only or a link out of the music folders"
                                                         if skipped["total"] else "no audio files found"))
        # The first track that probes. Nothing on the speaker is touched until we know there is something to play.
        first, rest, bad = None, list(files), []
        while rest and first is None:
            candidate = rest.pop(0)
            try:
                first = media.Track(candidate, self.probe(candidate))
            except (ValueError, OSError) as e:
                bad.append({"path": str(candidate), "reason": f"can't read it as audio ({e})"[:200]})
        if first is None:
            raise NothingPlayable("None of the files could be read as audio")
        if first.needs_conversion:                               # so the speaker's first request isn't waiting on ffmpeg
            try:
                self.convert(first)
            except (RuntimeError, OSError) as e:
                raise NothingPlayable(f"Couldn't convert {first.path.name} for the speaker: {e}"[:300]) from None
        key = getattr(coordinator, "uid", room_uid)              # one lock and one job slot per group, not per request room
        if mode == "append":                                     # an append goes after what is still being queued, never cuts it short
            pending = self._by_room.get(key)
            if pending is not None and not pending.done:
                pending.finished.wait(120)
        with self._room_lock(key):
            old = self._by_room.get(key)
            try:
                base = self.base_url_for(coordinator)
                if mode == "replace":
                    coordinator.clear_queue()
                first_index = coordinator.queue_size
                coordinator.add_to_queue(queue_item(first, self.library.media_path(base, first.path, converted=first.needs_conversion), first_index))
                if mode == "replace":
                    coordinator.play_from_queue(first_index)
            except PlayError:
                raise
            except Exception as e:
                raise SpeakerError(f"The speaker didn't take the request: {e}") from e
            if mode == "replace" and old and not old.done:
                old.cancelled = True                             # only now that the new play has taken over the queue
            job = Job(uuid.uuid4().hex[:12], room_uid, total=len(files) - len(bad), queued=1, skipped=bad)
            with self._lock:
                self._jobs[job.id] = job
                self._by_room[key] = job
                if len(self._jobs) > 50:
                    for k in [k for k, j in self._jobs.items() if j.done][:25]:
                        del self._jobs[k]
        if rest:
            self._background.submit(self._queue_rest, job, coordinator, key, base, rest, first_index + 1)
        else:
            job.finish()
        return {"room": room_uid, "mode": mode, "job": job.id, "total": job.total, "started": mode == "replace",
                "skipped": skipped["items"] + bad, "skipped_total": skipped["total"] + len(bad),
                "truncated": truncated}

    def play_urls(self, room_uid: str, entries: list[tuple[str, str, str]], mode: str = "replace") -> dict:
        """Queue ready-made URLs (url, title, mime) on a room, e.g. /remote/ proxy URLs for a web page's tracks."""
        from soco.data_structures import DidlMusicTrack, DidlResource
        if mode not in ("replace", "append"):
            raise PlayError("mode must be 'replace' or 'append'")
        if not entries:
            raise NothingPlayable("Nothing to queue")
        try:
            coordinator = self.coordinator_for(room_uid)
        except KeyError:
            raise UnknownRoom(f"No such room: {room_uid}") from None
        key = getattr(coordinator, "uid", room_uid)
        if mode == "append":
            pending = self._by_room.get(key)
            if pending is not None and not pending.done:
                pending.finished.wait(120)
        with self._room_lock(key):
            old = self._by_room.get(key)
            try:
                if mode == "replace":
                    coordinator.clear_queue()
                first_index = coordinator.queue_size
                items = [DidlMusicTrack(title=title, parent_id="", item_id=f"sonos-cli:{first_index + i}",
                                        resources=[DidlResource(uri=url, protocol_info=f"http-get:*:{mime}:*")])
                         for i, (url, title, mime) in enumerate(entries)]
                coordinator.add_to_queue(items[0])
                if mode == "replace":
                    coordinator.play_from_queue(first_index)
                for i in range(1, len(items), self.batch):
                    coordinator.add_multiple_to_queue(items[i:i + self.batch])
            except PlayError:
                raise
            except Exception as e:
                raise SpeakerError(f"The speaker didn't take the request: {e}") from e
            if mode == "replace" and old and not old.done:
                old.cancelled = True
            job = Job(uuid.uuid4().hex[:12], room_uid, total=len(entries), queued=len(entries))
            job.finish()
            with self._lock:
                self._jobs[job.id] = job
                self._by_room[key] = job
        return {"room": room_uid, "mode": mode, "job": job.id, "total": len(entries), "started": mode == "replace"}

    def stream_base(self, room_uid: str) -> str:
        """This Mac's base URL (http://ip:port) as the room's coordinator reaches it."""
        try:
            return self.base_url_for(self.coordinator_for(room_uid))
        except KeyError:
            raise UnknownRoom(f"No such room: {room_uid}") from None

    def play_stream(self, room_uid: str, url: str, title: str = "Browser tab") -> None:
        """Point a room at a live stream URL. May take seconds: the speaker answers once it has started fetching the stream."""
        try:
            coordinator = self.coordinator_for(room_uid)
        except KeyError:
            raise UnknownRoom(f"No such room: {room_uid}") from None
        key = getattr(coordinator, "uid", room_uid)
        with self._room_lock(key):
            old = self._by_room.get(key)
            try:
                coordinator.play_uri(url, title=title)
            except Exception as e:
                raise SpeakerError(f"The speaker didn't take the request: {e}") from e
            if old and not old.done:
                old.cancelled = True                             # queueing for the old play must not carry on over the stream

    def _room_lock(self, key: str) -> threading.RLock:
        with self._lock:
            return self._room_locks.setdefault(key, threading.RLock())

    def _queue_rest(self, job: Job, coordinator, key: str, base: str, rest: list[Path], index: int) -> None:
        try:
            tracks = self._pool.map(self._try_probe, rest)       # parallel, results in order
            pending: list = []
            for path, result in zip(rest, tracks):
                if job.cancelled:
                    return
                if isinstance(result, Exception):
                    job.skipped.append({"path": str(path), "reason": f"can't read it as audio ({result})"[:200]})
                    job.total -= 1
                    continue
                try:
                    url = self.library.media_path(base, path, converted=result.needs_conversion)
                except PathNotAllowed:                           # its music folder was removed while this job ran (FR23)
                    job.skipped.append({"path": str(path), "reason": "its music folder was removed"})
                    job.total -= 1
                    continue
                pending.append(queue_item(result, url, index + len(pending)))
                if len(pending) >= self.batch:
                    index += self._flush(job, coordinator, key, pending)
                    pending = []
            if pending and not job.cancelled:
                self._flush(job, coordinator, key, pending)
        except Exception as e:                                   # the speaker went away, say
            job.skipped.append({"path": "", "reason": f"queueing stopped: {e}"[:200]})
        finally:
            job.finish()

    def _try_probe(self, path: Path):
        try:
            return media.Track(path, self.probe(path))
        except (ValueError, OSError) as e:
            return e

    def _flush(self, job: Job, coordinator, key: str, items: list) -> int:
        with self._room_lock(key):                               # a new play's clear_queue can't land between our check and our add
            if job.cancelled:
                return 0
            coordinator.add_multiple_to_queue(items)
            job.queued += len(items)
        return len(items)

    def close(self) -> None:
        self._background.shutdown(wait=False, cancel_futures=True)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if all(j.done for j in list(self._jobs.values())):
                return True
            time.sleep(0.02)
        return False
