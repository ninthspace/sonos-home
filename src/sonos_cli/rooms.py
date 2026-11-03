"""RoomMonitor: finds rooms, keeps their state in a cache, and publishes changes (FR4, AD8, NFR5).

All SoCo traffic happens here, on the monitor's own thread (or a worker thread that calls `refresh_group_of`).
Requests only read the cache, and the cache lock is never held across a SoCo call.

- Discovery runs at start-up and every `discovery_interval` seconds (and on demand), and polls each group once.
- Between discoveries a room is polled every `poll_interval` seconds only while a client is watching it
  (it asked for the room's state within `watch_seconds`) or while the room is playing something of ours.
- Rooms are identified by UID; a changed IP or name is picked up under the same UID.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlsplit

OURS_PATHS = ("/media/", "/live/", "/remote/")
ACTIVE_STATES = {"PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING"}


def validate_room_name(name) -> str:
    """A room name as Sonos will take it: trimmed, 1-64 characters, at most 128 UTF-8 bytes, nothing invisible."""
    import unicodedata
    if not isinstance(name, str):
        raise ValueError("name must be text")
    name = name.strip()
    if not 1 <= len(name) <= 64 or len(name.encode("utf-8")) > 128 or any((unicodedata.category(c).startswith("C") and c not in "\u200c\u200d") or unicodedata.category(c) in ("Zl", "Zp") for c in name):
        raise ValueError("name must be 1 to 64 characters (128 bytes), with no control or invisible characters")
    return name


def classify_source(uri: str | None, our_hosts: Iterable[str], our_port: int) -> str:
    """`ours`, `other-host:<host>` (this service's URL shapes on another host) or `other` (AD7)."""
    if not uri:
        return "other"
    if uri.startswith("x-rincon-mp3radio://"):
        rest = uri[len("x-rincon-mp3radio://"):]
        # A real S1 speaker reports the track URI of a radio stream as `x-rincon-mp3radio://http://host:port/…` (the scheme twice),
        # while its media URI has it once: both mean the same stream.
        uri = rest if rest.startswith(("http://", "https://")) else "http://" + rest
    try:
        parts = urlsplit(uri)
        host, port = parts.hostname, parts.port
    except ValueError:                                   # a bad port or bracket in someone else's URL
        return "other"
    if parts.scheme not in ("http", "https") or not host:
        return "other"
    if not parts.path.startswith(OURS_PATHS):
        return "other"
    if host.lower() in {h.lower() for h in our_hosts} and (port or 80) == our_port:
        return "ours"
    return f"other-host:{host}"


@dataclass
class Room:
    uid: str
    name: str
    ip: str
    model: str
    coordinator_uid: str
    members: list[dict]
    online: bool = True
    last_seen: float = 0.0

    def as_dict(self) -> dict:
        return {"uid": self.uid, "name": self.name, "ip": self.ip, "model": self.model,
                "coordinator_uid": self.coordinator_uid, "members": self.members, "online": self.online}


class RoomMonitor:
    def __init__(self, discover: Callable[[], Iterable], *, our_hosts: Callable[[], Iterable[str]] | None = None,
                 our_port: int = 8737, clock: Callable[[], float] = time.monotonic, poll_interval: float = 2.0,
                 discovery_interval: float = 60.0, watch_seconds: float = 10.0):
        self.discover = discover
        self.our_hosts = our_hosts or (lambda: ())
        self.our_port = our_port
        self.clock = clock
        self.poll_interval = poll_interval
        self.discovery_interval = discovery_interval
        self.watch_seconds = watch_seconds
        self._rooms: dict[str, Room] = {}
        self._devices: dict[str, object] = {}
        self._states: dict[str, dict] = {}
        self._watched: dict[str, float] = {}
        self._last_discovery: float | None = None
        self._last_poll: float | None = None
        self._rediscover = False
        self._pending_names: dict[str, tuple[str, float]] = {}
        self._poll_locks: dict[str, threading.Lock] = {}
        self._hosts_cache: set[str] | None = None
        self._hosts_at = 0.0
        self._subscribers: list[Callable[[str, dict | None, dict], None]] = []
        self._topology_subscribers: list[Callable[[], None]] = []
        self._pinned: dict[str, int] = {}           # room uid -> how many things (live streams) need its group polled whatever it is doing
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- reads (cache only; these never touch SoCo and never wait for it) -----------------------------
    def rooms(self) -> list[dict]:
        with self._lock:
            out = []
            for room in sorted(self._rooms.values(), key=lambda r: r.name.casefold()):
                d = room.as_dict()
                st = self._states.get(room.coordinator_uid) or {}
                d.update(state=st.get("state"), volume=st.get("volume"), mute=st.get("mute"))
                d["online"] = room.online and st.get("reachable", True) is not False
                out.append(d)
            return out

    def rooms_seen(self) -> int:
        return sum(1 for r in self.rooms() if r["online"])

    def room(self, uid: str) -> Room | None:
        with self._lock:
            return self._rooms.get(uid)

    def state(self, uid: str) -> dict | None:
        """The cached state of the room's group (its coordinator's), and note that a client is watching."""
        with self._lock:
            room = self._rooms.get(uid)
            if room is None:
                return None
            self._watched[room.coordinator_uid] = self.clock()
            st = self._states.get(room.coordinator_uid) or _empty_state("not polled yet")
            return {**st, "uid": uid, "coordinator_uid": room.coordinator_uid}

    def subscribe(self, callback: Callable[[str, dict | None, dict], None]) -> None:
        """callback(coordinator_uid, old_state, new_state) after a state changes, on whichever thread polled."""
        self._subscribers.append(callback)

    def subscribe_topology(self, callback: Callable[[], None]) -> None:
        """callback() after a room has moved to another group (or a discovery found one did), on whichever thread noticed."""
        self._topology_subscribers.append(callback)

    def pin(self, uid: str) -> None:
        """Keep polling this room's group, even when it is stopped or playing something that isn't ours."""
        with self._lock:
            self._pinned[uid] = self._pinned.get(uid, 0) + 1

    def unpin(self, uid: str) -> None:
        with self._lock:
            n = self._pinned.get(uid, 0) - 1
            if n > 0:
                self._pinned[uid] = n
            else:
                self._pinned.pop(uid, None)

    def _topology_changed(self) -> None:
        for cb in list(self._topology_subscribers):
            try:
                cb()
            except Exception:
                pass

    def group_states(self) -> dict[str, dict]:
        """A copy of every group's cached state, keyed by coordinator UID, for rooms that are online."""
        with self._lock:
            live = {r.coordinator_uid for r in self._rooms.values() if r.online}
            return {uid: dict(st) for uid, st in self._states.items() if uid in live}

    def device(self, uid: str):
        """The SoCo object for the room itself (not its group's coordinator). KeyError if unknown."""
        with self._lock:
            self._rooms[uid]
            return self._devices[uid]

    def set_room_name(self, uid: str, name: str) -> None:
        """Show a rename at once; the next discovery confirms it."""
        with self._lock:
            room = self._rooms.get(uid)
            self._pending_names[uid] = (name, self.clock())
            if room is not None:
                room.name = name
                for m in room.members:
                    if m["uid"] == uid:
                        m["name"] = name
            self._rediscover = True

    def name_taken(self, name: str, except_uid: str) -> bool:
        with self._lock:
            return any(r.uid != except_uid and r.name.casefold() == name.casefold() for r in self._rooms.values())

    def request_discovery(self) -> None:
        """Ask for a discovery on the next tick (after grouping changes that polling can't fully see)."""
        self._rediscover = True

    def coordinator_device(self, uid: str):
        """The SoCo object for the coordinator of the room's group, from the last discovery or poll. KeyError if unknown."""
        with self._lock:
            room = self._rooms[uid]
            return self._devices[room.coordinator_uid]

    # ---- the work (blocking SoCo calls; call these from the monitor thread or a worker thread) ---------
    def refresh(self) -> None:
        """Discover rooms and poll each group once. A discovery that fails is retried in 10 s; polling carries on."""
        now = self.clock()
        try:
            found = list(self.discover() or [])
        except Exception:
            self._last_discovery = now - self.discovery_interval + min(10.0, self.discovery_interval)
            self._rediscover = False                 # the retry is governed by time now, not by the flag
            return
        self._last_discovery = now
        self._rediscover = False
        built: list[tuple[Room, object, object]] = []
        for dev in found:                          # all SoCo calls happen here, outside the lock
            try:
                coord = dev.group.coordinator
                members = [{"uid": m.uid, "name": m.player_name, "ip": m.ip_address, "visible": m.is_visible}
                           for m in sorted(dev.group.members, key=lambda m: m.ip_address)]
                model = dev.get_speaker_info().get("model_name", "")
                name = dev.player_name
                pending = self._pending_names.get(dev.uid)
                if pending:
                    if name == pending[0] or now - pending[1] > 30:
                        self._pending_names.pop(dev.uid, None)      # the speaker agrees now (or we gave up waiting)
                    else:
                        name = pending[0]                            # a rename the speaker's topology hasn't caught up with
                built.append((Room(dev.uid, name, dev.ip_address, model, coord.uid, members,
                                   online=True, last_seen=now), dev, coord))
            except Exception:
                continue                          # unreachable right now; keep what we knew
        moved = False
        with self._lock:
            seen = set()
            for room, dev, coord in built:
                seen.add(room.uid)
                before = self._rooms.get(room.uid)
                if before is not None and before.coordinator_uid != room.coordinator_uid:
                    moved = True
                self._rooms[room.uid] = room
                self._devices[room.uid] = dev
                self._devices[coord.uid] = coord
            for uid, room in self._rooms.items():
                if uid not in seen:
                    room.online = False
            coordinators = {self._rooms[u].coordinator_uid for u in seen}
        if moved:
            self._topology_changed()
        for coord_uid in coordinators:
            self.poll_group(coord_uid)

    def poll_group(self, coordinator_uid: str) -> dict | None:
        """Read one group's state now. Polls of the same group are serialised, so an older read can't overwrite a newer.
        If the room turns out to follow another coordinator, that group is read next, after this group's lock is released."""
        uid, visited = coordinator_uid, set()
        while uid not in visited and len(visited) < 4:
            visited.add(uid)
            with self._lock:
                lock = self._poll_locks.setdefault(uid, threading.Lock())
            with lock:
                result = self._poll_group(uid)
            if not isinstance(result, _Moved):
                return result
            uid = result.coordinator_uid
        return None

    def _poll_group(self, coordinator_uid: str) -> "dict | _Moved | None":
        dev = self._devices.get(coordinator_uid)
        if dev is None:
            return None
        try:
            transport = dev.get_current_transport_info()
            track = dev.get_current_track_info()
            group = dev.group
            coord = group.coordinator
            members = [{"uid": m.uid, "name": m.player_name, "ip": m.ip_address, "visible": m.is_visible}
                       for m in sorted(group.members, key=lambda m: m.ip_address)]
            state = {
                "state": transport.get("current_transport_state"),
                "track": {"title": track.get("title", ""), "artist": track.get("artist", ""),
                          "album": track.get("album", ""), "uri": track.get("uri", ""),
                          "index": _index(track.get("playlist_position"))},
                "position": track.get("position"), "duration": track.get("duration"),
                "volume": group.volume, "mute": group.mute,
                "source": classify_source(track.get("uri"), self._hosts(), self.our_port),
                "reachable": True,
            }
        except Exception as e:
            with self._lock:
                old = self._states.get(coordinator_uid)
                state = {**(old or _empty_state()), "reachable": False, "error": str(e)}
                self._states[coordinator_uid] = state
            return state
        state["updated"] = time.time()
        if coord.uid != coordinator_uid:                         # the room we polled has joined another group
            self._apply_group(coord, members)
            return _Moved(coord.uid)
        self._apply_group(coord, members)
        with self._lock:
            old = self._states.get(coordinator_uid)
            self._states[coordinator_uid] = state
        if _without_timestamp(old) != _without_timestamp(state):
            for cb in list(self._subscribers):
                try:
                    cb(coordinator_uid, old, state)
                except Exception:
                    pass
        return state

    def _apply_group(self, coord, members: list[dict]) -> None:
        """Make the cached rooms agree with a group we have just read; ask for a discovery if some room fell out of it."""
        uids = {m["uid"] for m in members}
        moved = False
        with self._lock:
            self._devices[coord.uid] = coord
            for uid in uids:
                room = self._rooms.get(uid)
                if room is not None:
                    if room.coordinator_uid != coord.uid:
                        moved = True
                    room.coordinator_uid = coord.uid
                    room.members = members
            if any(r.coordinator_uid == coord.uid and r.uid not in uids and r.online for r in self._rooms.values()):
                self._rediscover = True              # someone left this group; find out where they went
        if moved:
            self._topology_changed()

    def _hosts(self) -> set[str]:
        now = self.clock()
        if self._hosts_cache is None or now - self._hosts_at > 30:
            self._hosts_cache, self._hosts_at = set(self.our_hosts()), now
        return self._hosts_cache

    def refresh_group_of(self, uid: str) -> dict | None:
        """Poll a room's group now (after an API call changed something). Blocking: call it from a worker thread."""
        room = self.room(uid)
        if room is None:
            return None
        with self._lock:
            self._watched[room.coordinator_uid] = self.clock()
        return self.poll_group(room.coordinator_uid)

    def _should_poll(self, coordinator_uid: str, now: float) -> bool:
        if now - self._watched.get(coordinator_uid, -1e9) <= self.watch_seconds:
            return True
        st = self._states.get(coordinator_uid) or {}
        return st.get("source") == "ours" and st.get("state") in ACTIVE_STATES

    def tick(self) -> None:
        """One scheduler step: discovery if it is due (or a group change asked for one), else poll what needs it."""
        now = self.clock()
        if self._last_discovery is None or self._rediscover or now - self._last_discovery >= self.discovery_interval:
            self.refresh()
            self._last_poll = now
            return
        if self._last_poll is not None and now - self._last_poll < self.poll_interval:
            return
        self._last_poll = now
        with self._lock:
            pinned = {self._rooms[u].coordinator_uid for u in self._pinned if u in self._rooms}
            targets = [u for u in {r.coordinator_uid for r in self._rooms.values() if r.online}
                       if u in pinned or self._should_poll(u, now)]
        for uid in targets:
            self.poll_group(uid)

    # ---- the thread -----------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="room-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass                              # a bad network moment must not kill the monitor
            self._stop.wait(min(self.poll_interval, 1.0))


class _Moved:
    """A poll found that the room follows another coordinator; poll that one instead."""

    def __init__(self, coordinator_uid: str):
        self.coordinator_uid = coordinator_uid


def _empty_state(error: str | None = None) -> dict:
    """A state with every key the API promises, for a group we have no reading of yet."""
    return {"state": None, "track": {"title": "", "artist": "", "album": "", "uri": "", "index": None},
            "position": None, "duration": None, "volume": None, "mute": None, "source": "other",
            "reachable": None if error == "not polled yet" else False, "updated": None,
            **({"error": error} if error else {})}


def _without_timestamp(state: dict | None) -> dict | None:
    return None if state is None else {k: v for k, v in state.items() if k != "updated"}


def _index(position) -> int | None:
    try:
        n = int(position)
    except (TypeError, ValueError):
        return None
    return n - 1 if n > 0 else None
