"""An in-memory Sonos system for tests and `sonos service run --fake-rooms`: five rooms, groups,
queues and transport state.

`FakeSoCo` implements the parts of `soco.SoCo` that the service and CLI use. It follows SoCo where
the code under test can go wrong: transport calls on a group member raise `SoCoSlaveException`,
every device has its own queue, `partymode` is a `join` of every other visible room, and a
stereo-pair satellite moves with its partner. Every state-changing call (and every volume or mute
change) is appended to `.calls` as `(name, *args)`, so tests can assert which device received what
and in what order.

`FakeSystem.install(monkeypatch)` makes `soco.discover` and `soco.SoCo(ip)` return the fakes.
Real SoCo has no `uri` attribute; `set_source()` and `source_uri` are test-only knobs for "the room
was switched to another source".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from soco.exceptions import SoCoSlaveException, SoCoUPnPException

PLAYING, PAUSED, STOPPED, TRANSITIONING = (
    "PLAYING", "PAUSED_PLAYBACK", "STOPPED", "TRANSITIONING")


class FakeGroup:
    def __init__(self, system: "FakeSystem", coordinator: "FakeSoCo"):
        self._system = system
        self.coordinator = coordinator

    @property
    def uid(self) -> str:
        return f"{self.coordinator.uid}:1"

    @property
    def members(self) -> set["FakeSoCo"]:
        """Every member, including an invisible stereo-pair satellite (as real SoCo does)."""
        return {d for d in self._system.devices if d.coordinator is self.coordinator}

    @property
    def volume(self) -> int:
        vols = [m.volume for m in self.members]
        return round(sum(vols) / len(vols))

    @volume.setter
    def volume(self, value: int) -> None:
        for m in self.members:
            m.volume = value
        self.coordinator.calls.append(("group_volume", value))

    @property
    def mute(self) -> bool:
        return all(m.mute for m in self.members)

    @mute.setter
    def mute(self, value: bool) -> None:
        for m in self.members:
            m.mute = value


class FakeSoCo:
    def __init__(self, system: "FakeSystem", uid: str, name: str, ip: str, model: str,
                 visible: bool = True):
        self._system = system
        self.uid = uid
        self._name = name
        self.ip_address = ip
        self.model_name = model
        self.is_visible = visible
        self.coordinator: FakeSoCo = self
        self.pair_of: FakeSoCo | None = None      # set on an invisible stereo-pair satellite
        self.calls: list[tuple] = []
        self.reads: dict[str, int] = {}              # how often a state read hit this device
        self.volume = 30
        self.mute = False
        self.queue: list = []
        self.queue_index: int | None = None     # zero-based index into the queue
        self.transport_state = STOPPED
        self.position = 0                         # seconds into the current track
        self._source: str | None = None           # a non-queue source (radio, Spotify…), see set_source()
        self.calls.clear()

    def __setattr__(self, name, value):
        if name == "volume":
            value = max(0, min(100, value))             # SoCo clamps to 0-100
        if name in ("volume", "mute") and "calls" in self.__dict__:
            self.calls.append((f"set_{name}", value))
        super().__setattr__(name, value)

    def _master(self) -> "FakeSoCo":
        """Transport calls are only valid on the coordinator, as in SoCo's @only_on_master."""
        if not self.is_coordinator:
            raise SoCoSlaveException(f"The method or property can only be called/used on the coordinator "
                                     f"in a group ({self.player_name} follows {self.coordinator.player_name})")
        return self

    # ---- identity -----------------------------------------------------------------------------
    @property
    def player_name(self) -> str:
        return self._name

    @player_name.setter
    def player_name(self, value: str) -> None:
        self._name = value
        self.calls.append(("player_name", value))

    def __repr__(self) -> str:
        return f"<FakeSoCo {self._name} {self.ip_address}>"

    def get_speaker_info(self, refresh: bool = False) -> dict:
        return {"zone_name": self._name, "model_name": self.model_name, "uid": self.uid,
                "software_version": "11.16.1"}

    # ---- groups -------------------------------------------------------------------------------
    @property
    def is_coordinator(self) -> bool:
        return self.coordinator is self

    @property
    def group(self) -> FakeGroup:
        return FakeGroup(self._system, self.coordinator)

    @property
    def all_zones(self) -> set["FakeSoCo"]:
        return set(self._system.devices)

    @property
    def visible_zones(self) -> set["FakeSoCo"]:
        return {d for d in self._system.devices if d.is_visible}

    def _followers(self, visible: bool | None = None) -> list["FakeSoCo"]:
        return [d for d in self._system.devices
                if d.coordinator is self and d is not self and (visible is None or d.is_visible == visible)]

    def _satellites(self) -> list["FakeSoCo"]:
        """Invisible stereo-pair partners: they always follow whatever group this room is in."""
        return [d for d in self._system.devices if d.pair_of is self]

    def _follow(self, coordinator: "FakeSoCo") -> None:
        self.coordinator = coordinator
        for sat in self._satellites():
            sat.coordinator = coordinator

    def _leave_group(self) -> None:
        """Leave the current group. A coordinator's visible followers regroup under one of them."""
        if self.is_coordinator:
            followers = [f for f in self._followers(visible=True)]
            if followers:
                new = followers[0]
                for f in followers:
                    f._follow(new)
        self._follow(self)

    def join(self, master: "FakeSoCo") -> None:
        self.calls.append(("join", master.uid))
        master = master.coordinator
        if master is self:
            return
        self._leave_group()
        self._follow(master)
        self.transport_state = STOPPED
        self._source = None                              # we now play whatever the group plays
        self.queue_index = None

    def unjoin(self) -> None:
        self.calls.append(("unjoin",))
        self._leave_group()

    def partymode(self) -> None:
        """As real SoCo: tell every other *visible* room to join this one. Satellites get no call of their own."""
        self.calls.append(("partymode",))
        for zone in sorted(self.visible_zones, key=lambda d: d.ip_address):
            if zone is not self:
                zone.join(self)

    # ---- queue (each device has its own queue, as on a real system) ---------------------------------
    @property
    def queue_size(self) -> int:
        return len(self.queue)

    def get_queue(self, start: int = 0, max_items: int = 100, full_album_art_uri: bool = False) -> "FakeQueue":
        """As SoCo: one Browse of at most `max_items` (default 100) from `start`; `total_matches` says how many exist."""
        q = FakeQueue(self.queue[start:start + max_items])
        q.total_matches = len(self.queue)
        q.update_id = len(self.calls)
        return q

    def clear_queue(self) -> None:
        self._master()
        self.calls.append(("clear_queue",))
        self.queue.clear()
        self.queue_index = None

    def add_to_queue(self, item, position: int = 0, as_next: bool = False) -> int:
        self._master()
        self.calls.append(("add_to_queue", item))
        self.queue.append(item)
        return len(self.queue)

    def add_multiple_to_queue(self, items, container=None) -> None:
        items = list(items)
        self.calls.append(("add_multiple_to_queue", len(items)))
        self.queue.extend(items)

    def play_from_queue(self, index: int, start: bool = True) -> None:
        self._master()
        self.calls.append(("play_from_queue", index))
        if not 0 <= index < len(self.queue):
            raise SoCoUPnPException("fake: bad index", "718", "")
        self.queue_index = index
        self._source = None
        self.position = 0
        if start:
            self.transport_state = PLAYING

    # ---- transport (coordinator only) ---------------------------------------------------------------
    def play(self) -> None:
        self._master().calls.append(("play",))
        self.transport_state = PLAYING

    def pause(self) -> None:
        self._master().calls.append(("pause",))
        self.transport_state = PAUSED

    def stop(self) -> None:
        self._master().calls.append(("stop",))
        self.transport_state = STOPPED

    def next(self) -> None:
        self._master().calls.append(("next",))
        if self.queue_index is None or self.queue_index + 1 >= len(self.queue):
            raise SoCoUPnPException("fake: no next track", "711", "")
        self.queue_index += 1
        self.position = 0

    def previous(self) -> None:
        self._master().calls.append(("previous",))
        if self.queue_index is None or self.queue_index == 0:
            raise SoCoUPnPException("fake: no previous track", "711", "")
        self.queue_index -= 1
        self.position = 0

    def seek(self, position: str | None = None, track: int | None = None) -> None:
        self._master().calls.append(("seek", position, track))
        if position is None and track is None:
            raise ValueError("No position or track information given")
        if track is not None:
            if not 0 <= track < len(self.queue):
                raise SoCoUPnPException("fake: bad track", "718", "")
            self.queue_index = track
            self.position = 0
        if position is not None:
            if not re.fullmatch(r"[0-9][0-9]?:[0-9][0-9]:[0-9][0-9]", position):
                raise ValueError("Seek position must be in the format HH:MM:SS")
            h, m, s = (int(x) for x in position.split(":"))
            self.position = h * 3600 + m * 60 + s

    def play_uri(self, uri: str, meta: str = "", title: str = "", start: bool = True,
                 force_radio: bool = False) -> None:
        self._master().calls.append(("play_uri", uri))
        self._source = uri
        self.queue_index = None
        self.position = 0
        if start:
            self.transport_state = PLAYING

    # ---- what the speaker reports -------------------------------------------------------------
    @property
    def source_uri(self) -> str | None:
        """Test-only: what this room is playing, ignoring group membership (queue item or a set_source URI)."""
        if self._source is not None:
            return self._source
        if self.queue_index is not None and self.queue_index < len(self.queue):
            return _item_uri(self.queue[self.queue_index])
        return None

    def set_source(self, uri: str | None) -> None:
        """Test-only: switch the room to another source (Spotify, radio, another host's URL…)."""
        self._source = uri
        if uri is not None:
            self.queue_index = None

    def _reported_uri(self) -> str:
        """A group member reports `x-rincon:<coordinator uid>`; a coordinator its own source.
        (Which call reports it on a real member is unverified; the fake does it for both.)"""
        if not self.is_coordinator:
            return f"x-rincon:{self.coordinator.uid}"
        return self.source_uri or ""

    def get_current_transport_info(self) -> dict:
        self.reads["transport"] = self.reads.get("transport", 0) + 1
        return {"current_transport_state": self.coordinator.transport_state,
                "current_transport_status": "OK", "current_speed": "1"}

    def get_current_track_info(self) -> dict:
        self.reads["track"] = self.reads.get("track", 0) + 1
        src = self if self.is_coordinator else self.coordinator
        item = src.queue[src.queue_index] if src.queue_index is not None and src.queue_index < len(src.queue) else None
        return {
            "playlist_position": str(src.queue_index + 1) if src.queue_index is not None else "0",
            "title": getattr(item, "title", "") if item is not None else "",
            "artist": getattr(item, "creator", "") if item is not None else "",
            "album": getattr(item, "album", "") if item is not None else "",
            "uri": self._reported_uri(),
            "position": _hms(src.position),
            "duration": _item_duration(item),
        }

    def get_current_media_info(self) -> dict:
        return {"uri": self._reported_uri(), "channel": ""}

    def advance(self, seconds: float) -> None:
        """Move the playhead on (tests drive time themselves)."""
        c = self.coordinator
        if c.transport_state == PLAYING:
            c.position += int(seconds)


class FakeQueue(list):
    total_matches = 0
    update_id = 0


def _item_uri(item) -> str:
    resources = getattr(item, "resources", None) or []
    return resources[0].uri if resources else ""


def _item_duration(item) -> str:
    resources = getattr(item, "resources", None) or []
    return (resources[0].duration if resources and resources[0].duration else "0:00:00")


def _hms(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


@dataclass
class FakeSnapshot:
    """Stands in for soco.snapshot.Snapshot. Set `restore_error` to make restore() raise like a Spotify source does."""
    device: FakeSoCo
    snapshot_queue: bool = False
    restore_error: str | None = None      # a UPnP error code, e.g. "402"
    taken: bool = False
    restored: bool = False

    def snapshot(self) -> bool:
        self.taken = True
        return True

    def restore(self, fade: bool = False) -> None:
        if self.restore_error:
            raise SoCoUPnPException(f"UPnP Error {self.restore_error} received", self.restore_error, "")
        self.restored = True


class FakeSystem:
    """Office (Play:1 pair: coordinator + invisible satellite), Bedroom, Kitchen and Lounge."""

    def __init__(self) -> None:
        self.devices: list[FakeSoCo] = []
        self.office = self._add("RINCON_B8E937B8082A01400", "Office", "192.168.0.21", "Sonos PLAY:1")
        self.office_satellite = self._add("RINCON_B8E937B80E7401400", "Office", "192.168.0.27",
                                          "Sonos PLAY:1", visible=False)
        self.office_satellite.pair_of = self.office
        self.office_satellite.coordinator = self.office
        self.bedroom = self._add("RINCON_B8E937A005B801400", "Bedroom", "192.168.0.31", "Sonos PLAY:1")
        self.kitchen = self._add("RINCON_347E5C4251C401400", "Kitchen", "192.168.0.29", "Sonos PLAY:3")
        self.lounge = self._add("RINCON_000E58AABB0101400", "Lounge", "192.168.0.26", "Sonos CONNECT:AMP")

    def _add(self, uid, name, ip, model, visible=True) -> FakeSoCo:
        d = FakeSoCo(self, uid, name, ip, model, visible)
        self.devices.append(d)
        return d

    # ---- lookups ------------------------------------------------------------------------------
    def by_ip(self, ip: str) -> FakeSoCo:
        for d in self.devices:
            if d.ip_address == ip:
                return d
        raise OSError(f"fake: no Sonos at {ip}")

    def by_uid(self, uid: str) -> FakeSoCo:
        for d in self.devices:
            if d.uid == uid:
                return d
        raise KeyError(uid)

    def discover(self, timeout: int = 5, **kwargs) -> set[FakeSoCo]:
        """What `soco.discover` returns: the visible players."""
        return {d for d in self.devices if d.is_visible}

    @property
    def visible(self) -> list[FakeSoCo]:
        return [d for d in self.devices if d.is_visible]

    # ---- mutations a test can make to the world ------------------------------------------------
    def change_ip(self, device: FakeSoCo, new_ip: str) -> None:
        device.ip_address = new_ip

    def install(self, monkeypatch) -> "FakeSystem":
        """Make soco.discover() and soco.SoCo(ip) return these fakes."""
        import soco
        monkeypatch.setattr(soco, "discover", self.discover)
        monkeypatch.setattr(soco, "SoCo", self.by_ip)
        return self


# ---- a pretend internet for `sonos service run --fake-rooms` and tests -----------------------------------------------------

FAKE_PUBLIC_IP = "93.184.216.34"
FAKE_RELEASE_URL = "https://shtl-experiment.com/hello-ep/"
FAKE_EMPTY_URL = "https://shtl-experiment.com/about/"

_RELEASE_PAGE = """<!doctype html><html><head><title>Hello EP</title></head><body>
<div class="track"><a class="title" href="1/">Halyard</a><audio><source type="audio/mpeg" src="1/mp3-v5/aaaa/01 Halyard.mp3"></audio></div>
<div class="track"><a class="title" href="2/">Undertow</a><audio><source type="audio/mpeg" src="2/mp3-v5/bbbb/02 Undertow.mp3"></audio></div>
</body></html>"""
_EMPTY_PAGE = "<!doctype html><html><head><title>About</title></head><body><p>No audio here, just words.</p></body></html>"


def fake_fetcher():
    """A remote.Fetcher whose \"internet\" is two pages on shtl-experiment.com: a release with two MP3 tracks and a page with none.

    Only `shtl-experiment.com` is given a pretend (public) address; every other name or address is resolved for real and checked for
    real, so loopback and LAN addresses are refused as they would be in the real service. Any host that does resolve to a public
    address is served the pages below, since nothing leaves this machine."""
    import httpx

    from . import remote

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/hello-ep/":
            return httpx.Response(200, text=_RELEASE_PAGE, headers={"content-type": "text/html; charset=utf-8"})
        if path == "/about/":
            return httpx.Response(200, text=_EMPTY_PAGE, headers={"content-type": "text/html; charset=utf-8"})
        if path.endswith(".mp3"):
            return httpx.Response(200, content=b"ID3" + bytes(1000), headers={"content-type": "audio/mpeg", "accept-ranges": "bytes"})
        return httpx.Response(404, text="not found")

    def resolve(host: str, port: int) -> list[str]:
        if host == "shtl-experiment.com":
            return [FAKE_PUBLIC_IP]
        return remote.default_resolve(host, port)             # everything else is resolved for real, so private addresses are refused for real

    return remote.Fetcher(resolve=resolve, transport=httpx.MockTransport(handler))
