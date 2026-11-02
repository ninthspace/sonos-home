"""Finding a Sonos room and putting tracks in its queue."""

from __future__ import annotations

import soco
from soco.data_structures import DidlMusicTrack, DidlResource

from .media import Track


class RoomNotFound(Exception):
    def __init__(self, name: str, available: list[str]):
        rooms = ", ".join(available) or "none found"
        super().__init__(f"No Sonos room called {name!r} (rooms: {rooms})")


def find_room(name: str, ip: str | None = None) -> soco.SoCo:
    """Return the coordinator of the group the named room belongs to."""
    if ip:
        return soco.SoCo(ip).group.coordinator
    devices = soco.discover(timeout=3, allow_network_scan=True) or set()
    for device in devices:
        if device.player_name.casefold() == name.casefold():
            return device.group.coordinator
    raise RoomNotFound(name, sorted({d.player_name for d in devices}))


def list_rooms() -> list[soco.SoCo]:
    devices = soco.discover(timeout=3, allow_network_scan=True) or set()
    return sorted(devices, key=lambda d: d.player_name.casefold())


def queue_item(track: Track, url: str, index: int) -> DidlMusicTrack:
    resource = DidlResource(
        uri=url,
        protocol_info=f"http-get:*:{track.mime}:*",
        duration=format_duration(track.info.duration),
    )
    extra = {}
    if track.info.artist:
        extra["creator"] = track.info.artist
    if track.info.album:
        extra["album"] = track.info.album
    return DidlMusicTrack(
        title=track.title,
        parent_id="",
        item_id=f"sonos-cli:{index}",
        resources=[resource],
        **extra,
    )


def format_duration(seconds: float | None) -> str | None:
    if not seconds:
        return None
    total = int(round(seconds))
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
