"""Helper for tests that touch a real room. Always use Office, and always through this.

    with muted_room(coordinator) as members:
        ...play something...

It saves the room's state, mutes every member (stereo-pair satellites too) and, on the way out, restores
volume and mute in their own step *before* it tries the queue and source. Restoring a Spotify Connect
source raised UPnP 402 in the 2026-11-02 session and skipped everything after it, so the source restore
can fail without taking volume and mute with it. A source that can't be restored is reported, not hidden.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator


def restore_volume_and_mute(members, saved: dict, report: Callable[[str], None]) -> None:
    for m in members:
        volume, mute = saved[m.uid]
        try:
            m.volume = volume
        except Exception as e:   # one stubborn member mustn't stop the others
            report(f"volume not restored on {m.player_name} ({m.ip_address}): {e}")
        try:
            m.mute = mute
        except Exception as e:
            report(f"mute not restored on {m.player_name} ({m.ip_address}): {e}")


def restore_source(coordinator, snapshot, report: Callable[[str], None]) -> None:
    try:
        snapshot.restore(fade=False)
    except Exception as e:
        report(f"source not restored: {e}")


def stop_playback(coordinator, report: Callable[[str], None] = print) -> None:
    """Stop whatever the test left playing *before* anything is unmuted; say so if that isn't possible."""
    try:
        coordinator.stop()
    except Exception as e:
        report(f"playback not stopped (it may still be playing when unmuted): {e}")


def restore_all(coordinator, members, saved, snapshot, report) -> None:
    """Stop, then volume and mute in their own try/finally step, then the queue and source."""
    stop_playback(coordinator, report)
    try:
        restore_volume_and_mute(members, saved, report)
    finally:
        restore_source(coordinator, snapshot, report)


@contextmanager
def muted_room(coordinator, snapshot_cls=None, report: Callable[[str], None] = print) -> Iterator[list]:
    if snapshot_cls is None:
        from soco.snapshot import Snapshot as snapshot_cls
    members = list(coordinator.group.members)
    saved = {m.uid: (m.volume, m.mute) for m in members}
    snapshot = snapshot_cls(coordinator, snapshot_queue=True)
    try:
        snapshot.snapshot()
    except Exception as e:
        report(f"snapshot failed ({e}); the queue and source won't be restored")
    try:
        for m in members:
            m.mute = True
        yield members
    finally:
        restore_all(coordinator, members, saved, snapshot, report)
