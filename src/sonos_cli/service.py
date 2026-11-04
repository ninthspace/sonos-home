"""The always-on service: JSON API, media files for the speakers, and (later) the web UI and live streams.

One Starlette app (AD1). Blocking work (ffprobe, ffmpeg, SoCo) runs in worker threads.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
import re
import stat as stat_mod
import socket
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import anyio
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.staticfiles import StaticFiles
from starlette.responses import Response, StreamingResponse, FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute

from . import balance as balance_mod
from . import config as config_mod
from . import media
from .library import Library, LiveLibrary, PathNotAllowed
from . import playback
from .playback import PlayError, Player
from . import live as live_mod
from . import pages as pages_mod
from . import remote as remote_mod
from .playlists import PlaylistError, PlaylistStore, has_line_break
from .rooms import RoomMonitor, classify_source, validate_room_name
from .sleep import SleepAssertion
from .server import local_ip_towards

WEB_DIR = Path(__file__).with_name("web")


class WebFiles(StaticFiles):
    """The UI's files, always revalidated, so a tab never runs old JavaScript against an upgraded API."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response

STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
BROWSE_TIMEOUT_S = 5          # a folder listing (FR23) that takes longer, e.g. a disconnected network share, is answered 504
MAX_BODY_BYTES = 12_000_000     # a page's HTML (capped at 5 MB) in a JSON body, with room for escaping, is the biggest request any route takes


# ---- hardening (NFR6) ---------------------------------------------------------------------------

def local_addresses() -> set[str]:
    """This Mac's IPv4 addresses (loopback, hostname lookup, and the address on the default route)."""
    addrs = {"127.0.0.1"}
    try:
        addrs |= {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:    # the address on the default route; sends nothing
            s.connect(("203.0.113.1", 9))
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    return addrs


def default_allowed_hosts() -> set[str]:
    """Host names the service answers to: localhost, loopback, this Mac's name and its addresses."""
    hosts = {"localhost", "127.0.0.1", "[::1]"} | local_addresses()
    name = socket.gethostname()
    hosts |= {name.lower(), name.lower().split(".")[0], name.lower().split(".")[0] + ".local"}
    return hosts


def _split_host(value: str) -> str | None:
    """The lower-cased host of a Host header value, or None if it isn't a plain `host` or `host:port`."""
    m = re.fullmatch(r"(\[[0-9a-f:.]+\]|[a-z0-9._-]+)(?::([0-9]*))?", value.strip().lower())
    if m is None or (m.group(2) is not None and m.group(2) == ""):
        return None
    return m.group(1)


def _is_ip_literal(host: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host))


class Hardening:
    """Stops other websites in the user's own browser from driving the service (no login exists):

    - a `Host` that isn't ours is refused (DNS rebinding);
    - state-changing requests and WebSocket upgrades must carry our own `Origin`, or
      `chrome-extension://<configured id>`;
    - state-changing requests must be `Content-Type: application/json`.
    Speakers fetching media send no `Origin` and use GET/HEAD, so they're unaffected.
    """

    def __init__(self, app, allowed_hosts: set[str], extension_ids: tuple[str, ...] = (),
                 refresh_hosts: Callable[[], set[str]] | None = None, refresh_every: float = 5.0):
        self.app = app
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.extension_ids = set(extension_ids)
        self.refresh_hosts = refresh_hosts          # re-reads this Mac's addresses when an unknown IP shows up
        self.refresh_every = refresh_every
        self._refreshed_at = time.monotonic()

    def _host_allowed(self, host: str | None) -> bool:
        if host is None:
            return False
        if host in self.allowed_hosts:
            return True
        # The Mac may have got its address after the service started (login before Wi-Fi). An IP literal that is
        # one of this Mac's current addresses is fine: DNS rebinding needs a name, not an address.
        if self.refresh_hosts and _is_ip_literal(host) and time.monotonic() - self._refreshed_at >= self.refresh_every:
            self._refreshed_at = time.monotonic()
            self.allowed_hosts |= {h.lower() for h in self.refresh_hosts()}
            return host in self.allowed_hosts
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers: dict[str, str] = {}
        duplicated = False
        for k, v in scope["headers"]:
            name = k.decode("latin1").lower()
            if name in headers and name in ("host", "origin", "content-type"):
                duplicated = True                # the check and the app must not read different copies
            headers.setdefault(name, v.decode("latin1"))
        if duplicated:
            return await self._refuse(scope, receive, send, "duplicate Host, Origin or Content-Type header")
        problem = self._problem(scope, headers)
        if problem is None:
            return await self.app(scope, receive, send)
        await self._refuse(scope, receive, send, problem)

    async def _refuse(self, scope, receive, send, problem: str):
        if scope["type"] == "websocket":
            await receive()                      # the connect message
            return await send({"type": "websocket.close", "code": 1008, "reason": problem})
        await PlainTextResponse(problem, status_code=403)(scope, receive, send)

    def _problem(self, scope, headers) -> str | None:
        host = headers.get("host", "")
        if not self._host_allowed(_split_host(host)):
            return f"Host {host!r} is not allowed"
        is_ws = scope["type"] == "websocket"
        if not (is_ws or scope["method"] in STATE_CHANGING):
            return None
        origin = headers.get("origin")
        if origin is None or not self._origin_ok(origin, host):
            return f"Origin {origin!r} is not allowed"
        if not is_ws and headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return "state-changing requests must be application/json"
        return None

    def _origin_ok(self, origin: str, host: str) -> bool:
        if origin == f"http://{host}":
            return True                          # our own page
        parts = urlsplit(origin)
        return parts.scheme == "chrome-extension" and parts.netloc in self.extension_ids


# ---- media ---------------------------------------------------------------------------------------

class MediaServer:
    """Serves `/media/<root-id>/<path>`: guarded, stateless, converting on first fetch."""

    def __init__(self, library: Library):
        self.library = library
        self._probes: dict[tuple, media.Track] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def track(self, path: Path) -> media.Track:
        st = path.stat()
        key = (str(path), st.st_size, st.st_mtime_ns)
        if len(self._probes) > 4096:             # an always-on process: keep the caches bounded
            self._probes.clear()
            self._locks.clear()
        if key not in self._probes:
            info = await anyio.to_thread.run_sync(media.probe, path)
            self._probes[key] = media.Track(path, info)
        return self._probes[key]

    async def handle(self, request: Request):
        rid = request.path_params["root_id"]
        rel = request.path_params["rel"]
        try:
            path = self.library.resolve_media(rid, rel)
        except KeyError:
            return PlainTextResponse("unknown music folder", status_code=404)
        except PathNotAllowed:
            return PlainTextResponse("forbidden", status_code=403)
        try:
            is_file = path.is_file()
        except OSError:                          # e.g. a name over the filesystem's limit
            is_file = False
        suffixed = False
        if not is_file and rel.lower().endswith(".flac"):
            # `<name>.<ext>.flac` is how a URL names a file that is converted to FLAC (Library.media_path): the file that exists
            # is `<name>.<ext>`. A path that exists as written is always served as itself, so this can't hide a real `.flac`.
            try:
                source = self.library.resolve_media(rid, rel[:-len(".flac")])
                suffixed = source.suffix.lower() in media.AUDIO_EXTENSIONS and source.suffix.lower() != ".flac" and source.is_file()
            except (KeyError, PathNotAllowed, OSError):
                suffixed = False
            if suffixed:
                path, is_file = source, True
        if not is_file:
            return PlainTextResponse("not found", status_code=404)
        try:
            track = await self.track(path)
        except ValueError:                       # not audio ffprobe can read (its message names the path; don't send it)
            return PlainTextResponse("not a supported audio file", status_code=415)
        except FileNotFoundError:                # ffprobe itself is missing, or the file went away
            return PlainTextResponse("audio tools unavailable or file gone", status_code=503)
        except OSError:
            return PlainTextResponse("file unavailable", status_code=503)
        if suffixed and not track.needs_conversion:            # `a.wav.flac` for a WAV that is served as it is: no such file
            return PlainTextResponse("not found", status_code=404)
        try:
            if track.needs_conversion:
                lock = self._locks.setdefault(str(path), asyncio.Lock())
                async with lock:
                    served = await anyio.to_thread.run_sync(media.convert, track)
                return FileResponse(served, media_type="audio/flac")
            return FileResponse(path, media_type=track.mime)
        except (RuntimeError, OSError):          # ffmpeg failed on a file ffprobe accepted
            return PlainTextResponse("could not prepare this file for the speaker", status_code=502)


def _same_uri(a: str, b: str) -> bool:
    from urllib.parse import unquote
    return bool(a) and unquote(a) == unquote(b)


# ---- app -----------------------------------------------------------------------------------------

def create_app(cfg: config_mod.Config | None = None, *, library: Library | None = None,
               hardening: bool = True, allowed_hosts: set[str] | None = None,
               rooms_seen: Callable[[], int] | None = None, monitor: RoomMonitor | None = None,
               player: Player | None = None, port: int | None = None,
               sleep: SleepAssertion | None = None, media_only: bool = False,
               fetcher: remote_mod.Fetcher | None = None, fake_system=None, no_local_clients: bool = False) -> Starlette:
    cfg = cfg or config_mod.get()
    if library is None:
        library = Library.create(cfg.music_folders, (media.CACHE_DIR, config_mod.config_dir() / "playlists"))
    if not isinstance(library, LiveLibrary):
        library = LiveLibrary(library)               # one shared holder: a folder added or removed at run time reaches every part of the service at once
    media_server = MediaServer(library)
    rooms_seen = rooms_seen or (monitor.rooms_seen if monitor else (lambda: 0))

    async def health(request: Request):
        ffmpeg = config_mod.find_binary("ffmpeg", cfg.ffmpeg)
        return JSONResponse({
            "ffmpeg": "ok" if ffmpeg else "missing",
            "rooms_seen": rooms_seen(),
            "host": request.url.hostname,
            "port": (request.scope.get("server") or (None, cfg.port))[1],
            "sleep_assertion": "held" if sleep and sleep.held else "released",
        })

    serve_port = port or cfg.port
    def coordinator_now(uid: str):
        """The room's coordinator; if the cached one has stopped leading its group, re-read the group first."""
        dev = monitor.coordinator_device(uid)
        try:
            leading = dev.is_coordinator
        except Exception:
            leading = True                                       # can't tell; let the request find out
        if not leading:
            monitor.refresh_group_of(uid)
            dev = monitor.coordinator_device(uid)
        return dev

    if player is None and monitor is not None:
        player = Player(library, cfg, coordinator_now,
                        lambda coord: f"http://{local_ip_towards(coord.ip_address)}:{serve_port}")

    async def json_body(request: Request) -> dict:
        # The biggest legitimate body is a page's HTML (5 MB) sent by the extension; nothing needs more than this.
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            raise playback.TooLarge("the request body is too large")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY_BYTES:
                raise playback.TooLarge("the request body is too large")
        try:
            body = json.loads(bytes(raw))
        except ValueError:
            raise PlayError("the request body must be JSON") from None
        if not isinstance(body, dict):
            raise PlayError("the request body must be a JSON object")
        return body

    def problem(e: PlayError) -> JSONResponse:
        return JSONResponse({"error": str(e)}, status_code=e.status)

    async def browse_route(request: Request):
        try:
            offset = int(request.query_params.get("offset", "0"))
            limit = int(request.query_params.get("limit", str(playback.PAGE_SIZE)))
            return JSONResponse(await anyio.to_thread.run_sync(
                playback.browse, library, cfg, request.query_params.get("path", ""), offset, limit))
        except ValueError:
            return JSONResponse({"error": "offset and limit must be whole numbers"}, status_code=400)
        except PlayError as e:
            return problem(e)

    async def play_route(request: Request):
        try:
            body = await json_body(request)
            room, paths = body.get("room"), body.get("paths")
            if not isinstance(room, str) or not isinstance(paths, list) or not paths \
                    or not all(isinstance(x, str) for x in paths):
                raise PlayError("send {\"room\": <room uid>, \"paths\": [<path>, …], \"mode\": \"replace\"|\"append\"}")
            if player is None:
                raise PlayError("no rooms are available")
            shuffle = body.get("shuffle", False)
            if not isinstance(shuffle, bool):
                raise PlayError("shuffle must be true or false")
            result = await anyio.to_thread.run_sync(
                lambda: player.play(room, paths, body.get("mode", "replace"), shuffle))
            if monitor is not None:                  # so the cache (and the sleep assertion) see it now, not at the next discovery
                await anyio.to_thread.run_sync(monitor.refresh_group_of, room)
            return JSONResponse(result)
        except PlayError as e:
            return problem(e)

    async def play_job(request: Request):
        job = player.job(request.path_params["job_id"]) if player else None
        if job is None:
            return JSONResponse({"error": "no such job"}, status_code=404)
        return JSONResponse(job.as_dict())

    TRANSPORT = {"pause": "pause", "resume": "play", "stop": "stop", "next": "next", "prev": "previous"}

    async def transport_route(request: Request):
        """POST /api/rooms/{uid}/transport {"action": "pause"|"resume"|"stop"|"next"|"prev"}: acts on the group's coordinator."""
        try:
            body = await json_body(request)
            action = body.get("action")
            if action not in TRANSPORT:
                raise PlayError(f"action must be one of: {', '.join(TRANSPORT)}")
            if monitor is None:
                raise PlayError("no rooms are available")
            uid = request.path_params["uid"]

            def go():
                try:
                    dev = coordinator_now(uid)
                except KeyError:
                    raise playback.UnknownRoom(f"No such room: {uid}") from None
                try:
                    getattr(dev, TRANSPORT[action])()
                except Exception as e:
                    raise playback.SpeakerError(f"The speaker refused {action}: {e}") from e
                monitor.refresh_group_of(uid)

            await anyio.to_thread.run_sync(go)
            return JSONResponse({"ok": True, "action": action})
        except PlayError as e:
            return problem(e)

    # ---- music folders, from the Music view (FR23) ------------------------------------------------------------------
    def this_mac(request: Request) -> bool:
        """True when the connection comes from this Mac itself: the TCP peer is loopback, or is the very address the connection came in
        on (the Mac talking to itself through its own LAN address). That needs no address lookup and no cache, so nothing another device
        can register in DNS or DHCP makes it count. A peer that isn't an IP address, or is missing, is another device. Judged from the
        connection only, never from a header."""
        def address(pair):
            try:
                ip = ipaddress.ip_address(pair[0]) if pair else None
            except (ValueError, TypeError, IndexError):
                return None
            return ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        if no_local_clients:                                              # `--fake-rooms --no-local-clients`: to see what another device sees
            return False
        peer, local = address(request.scope.get("client")), address(request.scope.get("server"))
        return peer is not None and (peer.is_loopback or (local is not None and peer == local))

    def require_this_mac(request: Request) -> None:
        if not this_mac(request):
            raise playback.NotThisMac("Folders are changed on the Mac itself, not from another device")

    folders_lock = threading.Lock()                                   # one read-modify-write of the config file at a time

    def folder_view() -> list[dict]:
        out = []
        for r in library.music_roots:
            try:
                exists = r.is_dir()
            except OSError:                                               # not allowed to look (a protected place, a parent without search permission)
                exists = False
            out.append({"path": str(r), "name": r.name or str(r), "exists": exists})
        return out

    def absolute_path(raw) -> Path:
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            raise PlayError('send {"path": "<an absolute folder path>"}')
        try:
            raw.encode("utf-8")                                           # a lone surrogate can't be echoed back in an error
        except UnicodeEncodeError:
            raise PlayError("the folder path isn't valid text") from None
        p = Path(raw).expanduser()
        if not p.is_absolute():
            raise PlayError("the folder path must be absolute (or start with ~)")
        try:
            return p.resolve()
        except (OSError, RuntimeError, ValueError) as e:
            raise playback.FolderRefused(f"{raw}: can't be resolved ({e})") from e

    def apply_config_change(**change) -> None:
        """Rewrite music_folders in the config file, then re-read it and hand the library its new roots. Holds `folders_lock`."""
        try:
            config_mod.update_music_folders(cfg.path, **change)
            library.swap_music_roots(config_mod.load(cfg.path).music_folders)
        except config_mod.ConfigError as e:
            raise playback.FolderConflict(str(e)) from e
        except OSError as e:
            raise playback.FolderConflict(f"{cfg.path}: {e.strerror or e}") from e

    def current_roots() -> tuple[Path, ...]:
        try:
            return config_mod.load(cfg.path).music_folders
        except config_mod.ConfigError as e:
            raise playback.FolderConflict(str(e)) from e

    def add_folder(raw) -> list[dict]:
        real = absolute_path(raw)
        try:
            exists, is_dir = real.exists(), real.is_dir()
        except OSError as e:                                              # e.g. a name over the filesystem's limit
            raise playback.FolderRefused(f"{real}: {e.strerror or e}") from e
        if not exists:
            raise playback.FolderRefused(f"{real} doesn't exist")
        if not is_dir:
            raise playback.FolderRefused(f"{real} is not a folder")
        try:
            with os.scandir(real):
                pass
        except OSError as e:
            raise playback.FolderRefused(f"{real} can't be read: {e.strerror or e}") from e
        with folders_lock:
            for r in current_roots():
                if config_mod.same_folder(r, real):
                    raise playback.FolderConflict(f"{real} is already a music folder")
                if config_mod.nested_folders(real, r):
                    raise playback.FolderConflict(f"{real} overlaps the music folder {r}")
            apply_config_change(add=real)
        return folder_view()

    def remove_folder(raw) -> list[dict]:
        target = absolute_path(raw)
        with folders_lock:
            if not any(config_mod.same_folder(r, target) for r in current_roots()):
                raise playback.UnknownFolder(f"{target} is not a music folder")
            apply_config_change(remove=target)
        return folder_view()

    HOME = Path.home().resolve()
    VOLUMES = Path("/Volumes").resolve()
    BROWSE_LIMIT = 500

    def browse_folders(raw) -> dict:
        if not raw:
            return {"path": "", "parent": None, "entries": [{"name": "Home", "path": str(HOME), "readable": True},
                                                            {"name": "Volumes", "path": "/Volumes", "readable": True}]}
        real = absolute_path(raw)
        if not (real.is_relative_to(HOME) or real.is_relative_to(VOLUMES)):
            raise playback.NotThisMac(f"{real} is outside the home folder and /Volumes")
        try:
            is_dir = real.is_dir()
        except OSError as e:
            raise playback.FolderRefused(f"{real}: {e.strerror or e}") from e
        if not is_dir:
            raise playback.FolderRefused(f"{real} is not a folder")
        entries = []
        try:
            with os.scandir(real) as it:
                for e in sorted(it, key=lambda e: e.name.casefold()):
                    if e.name.startswith("."):
                        continue
                    try:
                        if not e.is_dir() or getattr(e.stat(), "st_flags", 0) & stat_mod.UF_HIDDEN:
                            continue
                    except OSError:
                        continue
                    try:
                        with os.scandir(e.path):                     # listed, but marked when the service may not open it (macOS privacy)
                            readable = True
                    except OSError:
                        readable = False
                    entries.append({"name": e.name, "path": os.path.join(str(real), e.name), "readable": readable})
                    if len(entries) >= BROWSE_LIMIT:
                        break
        except OSError as e:
            raise playback.FolderRefused(f"{real} can't be read: {e.strerror or e}") from e
        parent = real.parent if real != HOME and real != VOLUMES and (real.parent.is_relative_to(HOME) or real.parent.is_relative_to(VOLUMES)) else None
        return {"path": str(real), "parent": str(parent) if parent else None, "entries": entries}

    async def folders_route(request: Request):
        # `is_dir()` on each root can hang on a disconnected share: not on the event loop, where it would stall every other request
        return JSONResponse({"folders": await anyio.to_thread.run_sync(folder_view), "editable": this_mac(request), "config_path": str(cfg.path)})

    async def folders_add_route(request: Request):
        try:
            require_this_mac(request)
            body = await json_body(request)
            return JSONResponse({"folders": await anyio.to_thread.run_sync(add_folder, body.get("path")), "editable": True})
        except PlayError as e:
            return problem(e)

    async def folders_remove_route(request: Request):
        try:
            require_this_mac(request)
            body = await json_body(request)
            return JSONResponse({"folders": await anyio.to_thread.run_sync(remove_folder, body.get("path")), "editable": True})
        except PlayError as e:
            return problem(e)

    async def folders_browse_route(request: Request):
        try:
            require_this_mac(request)
            try:
                with anyio.fail_after(BROWSE_TIMEOUT_S):            # a stalled network share must not hold the request
                    return JSONResponse(await anyio.to_thread.run_sync(browse_folders, request.query_params.get("path", ""), abandon_on_cancel=True))
            except TimeoutError:
                return JSONResponse({"error": "That folder took too long to list (a disconnected network share?)"}, status_code=504)
        except PlayError as e:
            return problem(e)

    async def seek_route(request: Request):
        """POST /api/rooms/{uid}/seek {"position": seconds}: move the group's current track to that second (a paused room stays paused). The track's length is
        read from the coordinator now, not from the cache, which can be a poll old (a track change would be checked against the
        previous track). Refused (409, the speaker is not asked) for a track with no length and for a position outside the track."""
        try:
            body = await json_body(request)
            position = body.get("position")
            try:
                valid = not isinstance(position, bool) and isinstance(position, (int, float)) and math.isfinite(position) and position >= 0
            except OverflowError:                                # an integer too large for a float, e.g. 400 digits
                valid = False
            if not valid:
                raise PlayError("position must be a number of seconds, 0 or more")
            if monitor is None:
                raise PlayError("no rooms are available")
            uid = request.path_params["uid"]

            def go():
                try:
                    dev = coordinator_now(uid)
                except KeyError:
                    raise playback.UnknownRoom(f"No such room: {uid}") from None
                info = speaker_call("read the current track", dev.get_current_track_info)
                m = re.fullmatch(r"(\d+):(\d\d):(\d\d)", str(info.get("duration") or ""))
                length = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) if m else 0
                if length <= 0:                                  # "", "0:00:00" (radio, a live stream) or "NOT_IMPLEMENTED"
                    raise playback.NotSeekable("This track has no length (nothing is playing, or it is a radio or live stream), so it can't be seeked")
                whole = math.floor(position)
                if whole >= length:
                    raise playback.NotSeekable(f"That position is past the end of this {length} s track")
                target = f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}"
                speaker_call("seek", lambda: dev.seek(target))
                monitor.refresh_group_of(uid)
                return whole

            return JSONResponse({"position": await anyio.to_thread.run_sync(go)})
        except PlayError as e:
            return problem(e)

    async def volume_route(request: Request):
        """POST /api/rooms/{uid}/volume {"level": 0-100}: the group's volume."""
        try:
            body = await json_body(request)
            level = body.get("level")
            if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 100:
                raise PlayError("level must be a whole number from 0 to 100")
            if monitor is None:
                raise PlayError("no rooms are available")
            uid = request.path_params["uid"]

            def go():
                try:
                    dev = coordinator_now(uid)
                except KeyError:
                    raise playback.UnknownRoom(f"No such room: {uid}") from None
                try:
                    dev.group.volume = level
                    now = dev.group.volume
                except Exception as e:
                    raise playback.SpeakerError(f"The speaker refused the volume change: {e}") from e
                monitor.refresh_group_of(uid)
                return now

            return JSONResponse({"volume": await anyio.to_thread.run_sync(go)})
        except PlayError as e:
            return problem(e)

    balance_locks: dict[str, threading.Lock] = {}
    balance_locks_guard = threading.Lock()

    def balance_lock(uid: str) -> threading.Lock:
        """One balance change at a time per speaker: SoCo writes left and right as two calls, so two overlapping changes could
        interleave and leave a mix of both."""
        with balance_locks_guard:
            return balance_locks.setdefault(uid, threading.Lock())

    def balance_device(uid: str):
        """The room's own speaker (balance belongs to the room, not its group), once the room is known to support it."""
        room = next((r for r in monitor.rooms() if r["uid"] == uid), None) if monitor is not None else None
        if room is None:
            raise playback.UnknownRoom(f"No such room: {uid}")
        if not balance_mod.room_has_balance(room):
            raise playback.NoBalance(f"{room['name']} is a single mono speaker ({room.get('model') or 'unknown model'}), so it has no balance setting")
        return monitor.device(uid)

    async def balance_get_route(request: Request):
        """GET /api/rooms/{uid}/balance: {"balance": -100..100, "left": 0-100, "right": 0-100}."""
        try:
            uid = request.path_params["uid"]

            def go():
                dev = balance_device(uid)
                left, right = speaker_call("read its balance", lambda: dev.balance)
                return {"balance": balance_mod.from_channels(left, right), "left": int(left), "right": int(right)}

            return JSONResponse(await anyio.to_thread.run_sync(go))
        except PlayError as e:
            return problem(e)

    async def balance_set_route(request: Request):
        """POST /api/rooms/{uid}/balance {"balance": -100..100}: set on the room's own speaker; answers the value read back."""
        try:
            body = await json_body(request)
            value = body.get("balance")
            if not isinstance(value, int) or isinstance(value, bool) or not -100 <= value <= 100:
                raise PlayError("balance must be a whole number from -100 (left) to 100 (right)")
            uid = request.path_params["uid"]

            def go():
                dev = balance_device(uid)
                with balance_lock(uid):
                    speaker_call("change its balance", lambda: setattr(dev, "balance", balance_mod.to_channels(value)))
                    left, right = speaker_call("read its balance", lambda: dev.balance)
                return {"balance": balance_mod.from_channels(left, right), "left": int(left), "right": int(right)}

            return JSONResponse(await anyio.to_thread.run_sync(go))
        except PlayError as e:
            return problem(e)

    async def fake_position_route(request: Request):
        """POST /api/fake/rooms/{uid}/position {"position": seconds}: only with `--fake-rooms` (there is no fake system, and so
        no route, in a real service). Puts a fake coordinator's playhead where a browser check needs it, inside the track."""
        try:
            body = await json_body(request)
            position = body.get("position")
            if isinstance(position, bool) or not isinstance(position, (int, float)) or not 0 <= position < 1e7:
                raise PlayError("position must be a number of seconds, 0 or more")
            uid = request.path_params["uid"]
            try:
                dev = fake_system.by_uid(uid).coordinator
            except KeyError:
                raise playback.UnknownRoom(f"No such room: {uid}") from None
            with fake_system.lock:
                fake_system.sync()                                # time already played counts before the playhead is moved
                duration = dev.current_duration()
                dev.position = int(min(position, duration - 1) if duration else position)
            if monitor is not None:
                await anyio.to_thread.run_sync(monitor.refresh_group_of, uid)
            return JSONResponse({"position": dev.position})
        except PlayError as e:
            return problem(e)

    def known_room(uid: str):
        try:
            return monitor.device(uid)
        except KeyError:
            raise playback.UnknownRoom(f"No such room: {uid}") from None

    def speaker_call(what: str, fn):
        try:
            return fn()
        except PlayError:
            raise
        except Exception as e:
            raise playback.SpeakerError(f"The speaker refused to {what}: {e}") from e

    async def rename_route(request: Request):
        """POST /api/rooms/{uid}/rename {"name": "Studio"}."""
        try:
            body = await json_body(request)
            try:
                name = validate_room_name(body.get("name"))
            except ValueError as e:
                raise PlayError(str(e)) from None
            if monitor is None:
                raise PlayError("no rooms are available")
            uid = request.path_params["uid"]

            def go():
                dev = known_room(uid)
                if monitor.name_taken(name, uid):
                    raise playback.NameTaken(f"Another room is already called {name!r}")
                speaker_call("rename the room", lambda: setattr(dev, "player_name", name))
                zgs = getattr(dev, "zone_group_state", None)
                if zgs is not None:
                    zgs.clear_cache()                         # SoCo's setter leaves a stale topology cache behind
                monitor.set_room_name(uid, name)

            await anyio.to_thread.run_sync(go)
            return JSONResponse({"uid": uid, "name": name})
        except PlayError as e:
            return problem(e)

    async def group_route(request: Request):
        """POST /api/rooms/{uid}/group {"into": <uid>}: the room joins the other room's group."""
        try:
            body = await json_body(request)
            into = body.get("into")
            if not isinstance(into, str):
                raise PlayError('send {"into": <room uid>}')
            if monitor is None:
                raise PlayError("no rooms are available")
            uid = request.path_params["uid"]
            if into == uid:
                raise PlayError("a room can't join itself")

            def go():
                dev = known_room(uid)
                target = known_room(into)
                leader = coordinator_now(into)
                try:
                    together = leader.uid == dev.uid or dev.group.coordinator.uid == leader.uid      # asked of the speaker, not the cache
                except Exception:
                    together = False
                if together:
                    return target                              # already together: nothing to tell the speaker
                speaker_call("join the group", lambda: dev.join(leader))
                monitor.refresh_group_of(uid)
                monitor.refresh_group_of(into)
                monitor.request_discovery()
                return target

            await anyio.to_thread.run_sync(go)
            return JSONResponse({"ok": True, "uid": uid, "into": into})
        except PlayError as e:
            return problem(e)

    async def ungroup_route(request: Request):
        """POST /api/rooms/{uid}/ungroup: the room leaves its group and plays on its own."""
        try:
            await json_body(request)
            if monitor is None:
                raise PlayError("no rooms are available")
            uid = request.path_params["uid"]

            def go():
                dev = known_room(uid)
                old = monitor.room(uid).coordinator_uid
                speaker_call("leave the group", dev.unjoin)
                if old == uid:
                    monitor.refresh()                            # the leaver led the group: its members now follow someone new, which only a
                else:                                            # fresh discovery shows (polling the old coordinator's id would read the wrong group)
                    monitor.poll_group(uid)                      # the room now leads a group of its own
                    monitor.poll_group(old)                      # and the group it left lost a member
                monitor.request_discovery()

            await anyio.to_thread.run_sync(go)
            return JSONResponse({"ok": True, "uid": uid})
        except PlayError as e:
            return problem(e)

    async def group_all_route(request: Request):
        """POST /api/rooms/group-all {"into": <uid>}: every visible room joins that room (party mode)."""
        try:
            body = await json_body(request)
            into = body.get("into")
            if not isinstance(into, str):
                raise PlayError('send {"into": <room uid>}')
            if monitor is None:
                raise PlayError("no rooms are available")

            def go():
                known_room(into)
                leader = coordinator_now(into)
                try:
                    speaker_call("group all rooms", leader.partymode)
                finally:
                    monitor.request_discovery()               # even a partly done party needs the cache corrected
                monitor.refresh_group_of(into)

            await anyio.to_thread.run_sync(go)
            return JSONResponse({"ok": True, "into": into})
        except PlayError as e:
            return problem(e)

    def queue_snapshot(uid: str) -> dict:
        """The coordinator's queue, as the UI shows it. Items on another host's service are labelled with that host."""
        try:
            dev = coordinator_now(uid)
        except KeyError:
            raise playback.UnknownRoom(f"No such room: {uid}") from None
        state = monitor.state(uid) or {}
        if state.get("reachable") is False:
            raise playback.SpeakerError("The speaker isn't answering, so its queue can't be read right now")

        def read_all():
            # One Browse returns at most 100 items (SoCo's default), so page until everything (up to the play cap) is read.
            got, start = [], 0
            while len(got) < playback.MAX_TRACKS:
                page = dev.get_queue(start, min(100, playback.MAX_TRACKS - len(got)))
                got.extend(page)
                start += len(page)
                if not len(page) or start >= getattr(page, "total_matches", len(got)):
                    break
            return got

        items = speaker_call("read the queue", read_all)
        hosts = local_addresses()
        out = []
        for i, item in enumerate(items):
            res = (getattr(item, "resources", None) or [None])[0]
            uri = getattr(res, "uri", "") if res is not None else ""
            source = classify_source(uri, hosts, serve_port)
            artist = getattr(item, "creator", "") or ""
            out.append({"index": i, "uri": uri, "title": remote_mod.queue_title(uri, getattr(item, "title", "") or uri, artist),
                        "artist": artist,
                        "host": source[len("other-host:"):] if source.startswith("other-host:") else None})
        track = state.get("track") or {}
        current = track.get("index")
        # Only claim a current item when the room really is playing that queue item (a radio or Spotify source reports track 1 too).
        if not (isinstance(current, int) and 0 <= current < len(out) and _same_uri(out[current]["uri"], track.get("uri", ""))):
            current = None
        for o in out:
            del o["uri"]
        return {"items": out, "current": current}

    async def queue_route(request: Request):
        try:
            if monitor is None:
                raise PlayError("no rooms are available")
            return JSONResponse(await anyio.to_thread.run_sync(queue_snapshot, request.path_params["uid"]))
        except PlayError as e:
            return problem(e)

    async def queue_jump_route(request: Request):
        """POST /api/rooms/{uid}/queue/jump {"index": 2}: play that queue item (0-based)."""
        try:
            body = await json_body(request)
            index = body.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                raise PlayError("index must be a whole number, 0 or more")
            if monitor is None:
                raise PlayError("no rooms are available")
            uid = request.path_params["uid"]

            def go():
                try:
                    dev = coordinator_now(uid)
                except KeyError:
                    raise playback.UnknownRoom(f"No such room: {uid}") from None
                if index >= dev.queue_size:
                    raise playback.NothingPlayable(f"The queue has {dev.queue_size} item(s); there is no item {index}")
                speaker_call("play that item", lambda: dev.play_from_queue(index))
                monitor.refresh_group_of(uid)

            await anyio.to_thread.run_sync(go)
            return JSONResponse({"ok": True, "index": index})
        except PlayError as e:
            return problem(e)

    async def queue_clear_route(request: Request):
        try:
            await json_body(request)
            if monitor is None:
                raise PlayError("no rooms are available")
            uid = request.path_params["uid"]

            def go():
                try:
                    dev = coordinator_now(uid)
                except KeyError:
                    raise playback.UnknownRoom(f"No such room: {uid}") from None
                speaker_call("clear the queue", dev.clear_queue)
                monitor.refresh_group_of(uid)

            await anyio.to_thread.run_sync(go)
            return JSONResponse({"ok": True})
        except PlayError as e:
            return problem(e)

    store = PlaylistStore(cfg.path.parent / "playlists", library)

    def playlist_problem(e: PlaylistError) -> JSONResponse:
        return JSONResponse({"error": str(e)}, status_code=e.status)

    async def playlists_route(request: Request):
        try:
            if request.method == "GET":
                return JSONResponse(await anyio.to_thread.run_sync(store.list))
            body = await json_body(request)
            name = body.get("name")
            if not isinstance(name, str) or not name.strip():
                raise PlayError('send {"name": "<playlist name>"}')
            pid = await anyio.to_thread.run_sync(store.create, name)
            return JSONResponse({"id": pid, "name": pid})
        except PlayError as e:
            return problem(e)
        except PlaylistError as e:
            return playlist_problem(e)

    async def playlist_route(request: Request):
        try:
            return JSONResponse(await anyio.to_thread.run_sync(store.get, request.path_params["pid"]))
        except PlaylistError as e:
            return playlist_problem(e)

    async def playlist_action(request: Request):
        pid, action = request.path_params["pid"], request.path_params["action"]
        try:
            body = await json_body(request)

            def whole_number(key):
                v = body.get(key)
                if not isinstance(v, int) or isinstance(v, bool):
                    raise PlayError(f"{key} must be a whole number")
                return v

            if action == "rename":
                new = body.get("name")
                if not isinstance(new, str) or not new.strip():
                    raise PlayError('send {"name": "<new name>"}')
                return JSONResponse({"id": await anyio.to_thread.run_sync(store.rename, pid, new)})
            if action == "delete":
                await anyio.to_thread.run_sync(store.delete, pid)
                return JSONResponse({"ok": True})
            if action == "add":
                paths = body.get("paths")
                if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
                    raise PlayError('send {"paths": [<file or folder>, …]}')

                def go_add():
                    files, skipped, truncated = playback.expand(library, cfg, paths)
                    odd = [f for f in files if has_line_break(str(f))]
                    for f in odd:
                        skipped["items"].append({"path": str(f), "reason": "its name has a line break, which a playlist can't store"})
                    files = [f for f in files if f not in odd]
                    n = store.add(pid, [(str(f), f.stem) for f in files])
                    return {"added": len(files), "total": n, "skipped": skipped["items"], "truncated": truncated}

                return JSONResponse(await anyio.to_thread.run_sync(go_add))
            if action == "remove":
                await anyio.to_thread.run_sync(store.remove, pid, whole_number("index"))
                return JSONResponse({"ok": True})
            if action == "move":
                await anyio.to_thread.run_sync(store.move, pid, whole_number("from"), whole_number("to"))
                return JSONResponse({"ok": True})
            if action == "play":
                room = body.get("room")
                if not isinstance(room, str):
                    raise PlayError('send {"room": <room uid>}')
                if player is None:
                    raise PlayError("no rooms are available")

                def go_play():
                    playable, unavailable = store.playable(pid)
                    if not playable:
                        raise playback.NothingPlayable("Nothing in this playlist can be played right now"
                                                       + (f" ({len(unavailable)} unavailable)" if unavailable else ""))
                    result = player.play(room, playable, body.get("mode", "replace"))
                    result["unavailable"] = unavailable
                    return result

                result = await anyio.to_thread.run_sync(go_play)
                if monitor is not None:
                    await anyio.to_thread.run_sync(monitor.refresh_group_of, room)
                return JSONResponse(result)
            return JSONResponse({"error": f"unknown action {action!r}"}, status_code=404)
        except PlayError as e:
            return problem(e)
        except PlaylistError as e:
            return playlist_problem(e)

    fetcher = fetcher or remote_mod.Fetcher()
    _secret: list[bytes] = []
    _secret_lock = threading.Lock()

    def secret() -> bytes:
        with _secret_lock:                                       # two first-ever sends must not each make a secret
            if not _secret:
                _secret.append(remote_mod.load_secret(cfg.path.parent / "remote-secret"))
            return _secret[0]

    async def page_tracks(body: dict):
        """(page title, tracks) from either {"url": …} (fetched) or {"html": …, "base_url": …} (sent by the extension)."""
        url, html, base = body.get("url"), body.get("html"), body.get("base_url")
        if isinstance(url, str) and url.strip():
            html, base = await fetcher.get_text(url.strip())     # a RemoteError becomes its own status in the routes
        elif isinstance(html, str) and isinstance(base, str):
            if pages_mod.absolute_url(base, "http://x/") is None:
                raise PlayError("base_url must be an http or https address")
        else:
            raise PlayError('send {"url": "<page address>"} or {"html": "<page html>", "base_url": "<its address>"}')
        try:
            return await anyio.to_thread.run_sync(pages_mod.extract, html, base)      # up to 5 MB of parsing: not on the event loop
        except pages_mod.NoAudio as e:
            raise playback.NothingPlayable(str(e)) from None

    async def send_preview_route(request: Request):
        try:
            title, tracks = await page_tracks(await json_body(request))
            return JSONResponse({"title": title, "tracks": [{"title": t.title, "artist": t.artist, "mime": t.mime, "url": t.url} for t in tracks]})
        except PlayError as e:
            return problem(e)
        except remote_mod.RemoteError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status)

    async def send_route(request: Request):
        try:
            body = await json_body(request)
            room = body.get("room")
            if not isinstance(room, str):
                raise PlayError('send {"room": <room uid>, "url": "<page address>"}')
            if player is None:
                raise PlayError("no rooms are available")
            title, tracks = await page_tracks(body)

            def go():
                base = player.base_url_for(player.coordinator_for(room))
                # The speaker labels a track it hasn't read yet from the last part of its URL, so that name carries the artist too.
                entries = [(f"{base}/remote/{remote_mod.sign(t.url, secret())}/"
                            f"{remote_mod.display_name(f'{t.title} by {t.artist}' if t.artist else t.title, t.mime)}", t.title, t.mime)
                           for t in tracks]
                result = player.play_urls(room, entries, body.get("mode", "replace"))
                if monitor is not None:
                    monitor.refresh_group_of(room)
                return {**result, "title": title, "tracks": [{"title": t.title, "artist": t.artist, "url": t.url} for t in tracks]}

            try:
                return JSONResponse(await anyio.to_thread.run_sync(go))
            except KeyError:
                raise playback.UnknownRoom(f"No such room: {room}") from None
        except PlayError as e:
            return problem(e)
        except remote_mod.RemoteError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status)

    async def remote_route(request: Request):
        """GET/HEAD /remote/<token>[/<name>]: fetch the signed URL for the speaker, passing Range through. The name is only there
        so the speaker, which titles a track from the last part of its URL, shows the page's title; it is never read."""
        try:
            url = remote_mod.verify(request.path_params["token"], secret())
            fwd = {k: request.headers[k] for k in ("range", "if-range") if k in request.headers}
            client, resp, _ = await fetcher.open(request.method, url, fwd)
        except remote_mod.RemoteError as e:
            return PlainTextResponse(str(e), status_code=e.status)
        if resp.status_code not in (200, 206, 416):
            await resp.aclose()
            await client.aclose()
            return PlainTextResponse(f"the origin answered {resp.status_code}", status_code=502)
        if resp.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
            await resp.aclose()                                  # we asked for identity; compressed bytes would reach the speaker as audio
            await client.aclose()
            return PlainTextResponse("the origin sent the file compressed, which a speaker can't play", status_code=502)
        keep = {k: v for k, v in resp.headers.items() if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges", "etag", "last-modified")}
        keep.setdefault("accept-ranges", "bytes")

        async def body():
            try:
                if resp.is_stream_consumed:                      # an already-read response (a test double)
                    yield resp.content
                else:
                    async for chunk in resp.aiter_raw():
                        yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        if request.method == "HEAD":
            await resp.aclose()
            await client.aclose()
            return Response(status_code=resp.status_code, headers=keep)
        return StreamingResponse(body(), status_code=resp.status_code, headers=keep)

    async def list_rooms(request: Request):
        return JSONResponse(monitor.rooms() if monitor else [])

    async def room_state(request: Request):
        state = monitor.state(request.path_params["uid"]) if monitor else None
        if state is None:
            return JSONResponse({"error": "no such room"}, status_code=404)
        # `updated` is when the speaker was read, `now` when this answer was made, both on this Mac's clock: a page can age the
        # reading (the cache may hand out one that is a poll old) without trusting its own clock.
        return JSONResponse({**state, "now": time.time()})

    # ---- live tab streams (FR15, FR16) ------------------------------------------------------------------------------
    def live_coordinator_uid(uid: str) -> str | None:
        room = monitor.room(uid) if monitor is not None else None
        return room.coordinator_uid if room is not None else None

    live_registry = live_mod.LiveRegistry(lambda: config_mod.binary("ffmpeg"), coordinator_of=live_coordinator_uid)

    def live_pin(session) -> bool:
        """Keep the room's group polled for this session. Refused (False) once the session has ended, so a pin can't outlive it."""
        with session.pin_lock:
            if session.ended or session.pinned:
                return False
            monitor.pin(session.room_uid)
            session.pinned = True
            return True

    def live_unpin(session) -> None:
        with session.pin_lock:
            if session.pinned:
                session.pinned = False
                monitor.unpin(session.room_uid)

    async def live_prepare(room: str, session) -> str:
        """The stream's URL for this room. Cheap: the speaker is told about it afterwards (live_play), while audio already flows."""
        if player is None or monitor is None:
            raise live_mod.LiveError("no rooms are available")

        def go() -> str:
            try:
                base = player.stream_base(room)
            except PlayError as e:
                raise live_mod.LiveError(str(e)) from None
            return "x-rincon-mp3radio://" + base.split("://", 1)[1] + f"/live/{session.id}.mp3"

        return await anyio.to_thread.run_sync(go)

    async def live_play(room: str, session, url: str) -> None:
        """Tell the room to play the stream. Can take seconds on a real speaker, which only answers once it has audio to buffer."""
        def go() -> None:
            if not live_pin(session):                            # ended (replaced, or ffmpeg died) before the speaker was told anything
                raise live_mod.LiveError("the stream ended before it started")
            try:
                player.play_stream(room, url)
            except PlayError as e:
                raise live_mod.LiveError(str(e)) from None
            monitor.refresh_group_of(room)
            session.coordinator_uid = live_coordinator_uid(room) or session.coordinator_uid     # the one play_stream really used

        await anyio.to_thread.run_sync(go)
        session.armed = True                                     # from here a change of group or source means the room left the stream

    async def live_after_end(session, reason: str) -> None:
        """A stream the user ended (or that broke) stops the room if it is still playing that stream; one the room left doesn't."""
        if monitor is None:
            return

        def stop_if_ours():
            if reason in ("room-switched", "replaced"):
                return
            monitor.refresh_group_of(session.room_uid)           # read the room now: a cached URL can be a minute old
            try:
                dev = coordinator_now(session.room_uid)
            except KeyError:
                return
            uri = ((monitor.state(session.room_uid) or {}).get("track") or {}).get("uri") or ""
            if f"/live/{session.id}.mp3" in uri:
                dev.stop()
                monitor.refresh_group_of(session.room_uid)

        try:
            await anyio.to_thread.run_sync(stop_if_ours)
        except Exception:
            pass                                                 # the speaker may be gone; the stream is over either way
        finally:
            live_unpin(session)

    live_registry.on_end = live_after_end
    if monitor is not None:
        monitor.subscribe(live_registry.on_state)
        monitor.subscribe_topology(live_registry.on_topology)

    async def live_ingest_route(ws):
        await live_mod.ingest(ws, live_registry, coordinator_uid_of=live_coordinator_uid, prepare=live_prepare, play=live_play)

    @asynccontextmanager
    async def lifespan(app):
        if monitor:
            monitor.start()
        if sleep:
            sleep.start()
        try:
            yield
        finally:
            await live_registry.end_all()
            if player:
                player.close()
            if sleep:
                sleep.stop()
            if monitor:
                monitor.stop()

    media_route = Route("/media/{root_id}/{rel:path}", media_server.handle, methods=["GET", "HEAD"])
    app = Starlette(lifespan=lifespan, routes=[media_route] if media_only else [
        Route("/api/health", health, methods=["GET"]),
        WebSocketRoute("/live/ingest", live_ingest_route),
        Route("/api/browse", browse_route, methods=["GET"]),
        Route("/api/play", play_route, methods=["POST"]),
        Route("/api/play/jobs/{job_id}", play_job, methods=["GET"]),
        Route("/api/rooms", list_rooms, methods=["GET"]),
        Route("/api/send", send_route, methods=["POST"]),
        Route("/api/send/preview", send_preview_route, methods=["POST"]),
        Route("/remote/{token}", remote_route, methods=["GET", "HEAD"]),
        Route("/remote/{token}/{name:path}", remote_route, methods=["GET", "HEAD"]),
        Route("/api/playlists", playlists_route, methods=["GET", "POST"]),
        Route("/api/playlists/{pid}", playlist_route, methods=["GET"]),
        Route("/api/playlists/{pid}/{action}", playlist_action, methods=["POST"]),
        Route("/api/rooms/group-all", group_all_route, methods=["POST"]),
        Route("/api/rooms/{uid}/state", room_state, methods=["GET"]),
        Route("/api/rooms/{uid}/queue", queue_route, methods=["GET"]),
        Route("/api/rooms/{uid}/queue/jump", queue_jump_route, methods=["POST"]),
        Route("/api/rooms/{uid}/queue/clear", queue_clear_route, methods=["POST"]),
        Route("/api/rooms/{uid}/rename", rename_route, methods=["POST"]),
        Route("/api/rooms/{uid}/group", group_route, methods=["POST"]),
        Route("/api/rooms/{uid}/ungroup", ungroup_route, methods=["POST"]),
        Route("/api/rooms/{uid}/transport", transport_route, methods=["POST"]),
        Route("/api/rooms/{uid}/volume", volume_route, methods=["POST"]),
        Route("/api/rooms/{uid}/balance", balance_get_route, methods=["GET"]),
        Route("/api/rooms/{uid}/balance", balance_set_route, methods=["POST"]),
        Route("/api/rooms/{uid}/seek", seek_route, methods=["POST"]),
        Route("/api/folders", folders_route, methods=["GET"]),
        Route("/api/folders/add", folders_add_route, methods=["POST"]),
        Route("/api/folders/remove", folders_remove_route, methods=["POST"]),
        Route("/api/folders/browse", folders_browse_route, methods=["GET"]),
        *([Route("/api/fake/rooms/{uid}/position", fake_position_route, methods=["POST"])] if fake_system is not None else []),
        Route("/media/{root_id}/{rel:path}", media_server.handle, methods=["GET", "HEAD"]),
        Mount("/", app=WebFiles(directory=str(WEB_DIR), html=True), name="web"),      # the UI (last: API routes win)
    ])
    app.state.library = library
    app.state.config = cfg
    if hardening:
        inner = app
        app = Hardening(inner, allowed_hosts if allowed_hosts is not None else default_allowed_hosts(),
                        cfg.extension_ids, refresh_hosts=local_addresses if allowed_hosts is None else None)
        app.state = inner.state
        live_registry.host_ok = lambda header, guard=app: guard._host_allowed(_split_host(header))
    app.live = live_registry                                     # run() gives uvicorn a protocol that serves /live/<id>.mp3 from it
    return app


def run(cfg: config_mod.Config, *, host: str = "0.0.0.0", port: int | None = None, fake_rooms: bool = False,
        no_local_clients: bool = False) -> int:
    import uvicorn

    import soco

    port = port or cfg.port
    fetcher = None
    fake_system = None
    if fake_rooms:
        from .fakes import FakeSystem, fake_fetcher
        fake_system = FakeSystem(realtime=True)               # the playhead follows the clock, so the UI has something to show
        discover = fake_system.discover
        fetcher = fake_fetcher()                              # pages to send: shtl-experiment.com/hello-ep/ (2 tracks) and /about/ (none)
    else:
        def discover():
            return soco.discover(timeout=3, allow_network_scan=True) or set()
    monitor = RoomMonitor(discover, our_hosts=local_addresses, our_port=port)
    sleep = SleepAssertion(monitor, grace_minutes=cfg.pause_grace_minutes)
    app = create_app(cfg, monitor=monitor, port=port, sleep=sleep, fetcher=fetcher, fake_system=fake_system, no_local_clients=no_local_clients)
    # proxy_headers=False: the peer address is the TCP peer, never an X-Forwarded-For header (FR23 trusts it to tell this Mac from another device)
    uvicorn.run(app, host=host, port=port, log_level="info", http=live_mod.protocol_for(app.live), proxy_headers=False)
    return 0
