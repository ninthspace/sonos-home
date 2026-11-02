"""Command-line entry point: `sonos play ~/Dropbox/Music/album` and friends."""

from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .media import Track, clear_cache, find_audio_files, probe
from .player import RoomNotFound, find_room, list_rooms, queue_item
from .server import TrackServer, local_ip_towards

DEFAULT_ROOM = os.environ.get("SONOS_ROOM", "Office")
POLL_SECONDS = 2
STOPPED_GRACE_SECONDS = 10


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except (RoomNotFound, FileNotFoundError, ValueError) as exc:
        print(f"sonos: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sonos", description="Play files from this Mac on Sonos speakers.")
    parser.add_argument("-r", "--room", default=DEFAULT_ROOM, help=f"Sonos room (default: {DEFAULT_ROOM}, or $SONOS_ROOM)")
    parser.add_argument("--ip", default=os.environ.get("SONOS_IP"), help="speaker IP, skips discovery (or $SONOS_IP)")
    sub = parser.add_subparsers(dest="command", required=True)

    play = sub.add_parser("play", help="replace the room's queue with these files or folders and play them")
    play.add_argument("paths", nargs="+", type=Path)
    play.add_argument("-s", "--shuffle", action="store_true")
    play.add_argument("-p", "--port", type=int, default=0, help="port to serve files on (default: any free port)")
    play.add_argument("--idle-minutes", type=float, default=30, help="exit after being paused this long (default: 30)")
    play.add_argument("-v", "--verbose", action="store_true", help="log each request from the speaker")
    play.set_defaults(func=cmd_play)

    for name, func, help_text in [
        ("pause", lambda c: c.pause(), "pause"),
        ("resume", lambda c: c.play(), "resume playback"),
        ("stop", lambda c: c.stop(), "stop"),
        ("next", lambda c: c.next(), "skip to the next track"),
        ("prev", lambda c: c.previous(), "go back a track"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=_simple(func))

    volume = sub.add_parser("volume", help="show or set the room's volume (0-100)")
    volume.add_argument("level", nargs="?", type=int)
    volume.set_defaults(func=cmd_volume)

    sub.add_parser("status", help="show what the room is playing").set_defaults(func=cmd_status)
    sub.add_parser("rooms", help="list Sonos rooms on the network").set_defaults(func=cmd_rooms)
    sub.add_parser("clear-cache", help="delete converted copies of files").set_defaults(func=cmd_clear_cache)
    return parser


def _simple(action):
    def run(args) -> int:
        action(find_room(args.room, args.ip))
        return 0
    return run


def cmd_play(args) -> int:
    files = find_audio_files(args.paths)
    if not files:
        raise ValueError("No audio files found")
    tracks = _probe_all(files)
    if not tracks:
        raise ValueError("None of the files could be read")
    if args.shuffle:
        random.shuffle(tracks)

    coordinator = find_room(args.room, args.ip)
    server = TrackServer(local_ip_towards(coordinator.ip_address), args.port, verbose=args.verbose)
    server.start()
    urls = [server.add(t) for t in tracks]

    converting = sum(t.needs_conversion for t in tracks)
    print(f"{len(tracks)} track(s) for {coordinator.player_name}"
          + (f"; converting {converting} to 16-bit FLAC" if converting else ""))
    if tracks[0].needs_conversion:
        print(f"Converting {tracks[0].path.name}…")
    server.ready(urls[0])

    coordinator.clear_queue()
    coordinator.add_multiple_to_queue([queue_item(t, u, i) for i, (t, u) in enumerate(zip(tracks, urls))])
    coordinator.play_from_queue(0)

    # Playback happens on the speaker, so macOS sees no activity; keep it awake while we serve.
    caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    try:
        _serve_until_done(coordinator, server, len(tracks), args.idle_minutes * 60)
    except KeyboardInterrupt:
        if coordinator.get_current_track_info()["uri"].startswith(server.base_url):
            coordinator.stop()
        print("\nStopped.")
    finally:
        caffeinate.terminate()
        server.stop()
    return 0


def _probe_all(files: list[Path]) -> list[Track]:
    def one(path: Path) -> Track | None:
        try:
            return Track(path, probe(path))
        except ValueError as exc:
            print(f"Skipping: {exc}", file=sys.stderr)
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return [t for t in pool.map(one, files) if t]


def _serve_until_done(coordinator, server: TrackServer, count: int, idle_limit: float) -> None:
    """Keep serving while the room is playing our queue; return when it stops or moves on."""
    last_uri = None
    inactive_since = None
    while True:
        time.sleep(POLL_SECONDS)
        try:
            state = coordinator.get_current_transport_info()["current_transport_state"]
            info = coordinator.get_current_track_info()
        except Exception:  # noqa: BLE001 - transient network hiccups shouldn't end playback
            continue

        ours = info["uri"].startswith(server.base_url)
        if state in ("PLAYING", "TRANSITIONING"):
            if not ours:
                print(f"{coordinator.player_name} is playing something else now; done.")
                return
            inactive_since = None
            if info["uri"] != last_uri:
                last_uri = info["uri"]
                print(f"▶ {info['playlist_position']}/{count}  {info['title']}")
            continue

        inactive_since = inactive_since or time.monotonic()
        limit = idle_limit if state == "PAUSED_PLAYBACK" else STOPPED_GRACE_SECONDS
        if time.monotonic() - inactive_since > limit:
            print("Finished." if state == "STOPPED" else "Paused too long; stopping the file server.")
            return


def cmd_volume(args) -> int:
    coordinator = find_room(args.room, args.ip)
    if args.level is not None:
        coordinator.group.volume = max(0, min(100, args.level))
    print(coordinator.group.volume)
    return 0


def cmd_status(args) -> int:
    coordinator = find_room(args.room, args.ip)
    state = coordinator.get_current_transport_info()["current_transport_state"]
    info = coordinator.get_current_track_info()
    members = ", ".join(sorted(m.ip_address for m in coordinator.group.members))
    print(f"{coordinator.player_name} ({members}): {state}")
    if info.get("title") or info.get("uri"):
        print(f"  {info.get('title') or info.get('uri')}  {info.get('position')}/{info.get('duration')}")
    print(f"  volume {coordinator.group.volume}{' (muted)' if coordinator.group.mute else ''}")
    return 0


def cmd_rooms(args) -> int:
    for device in list_rooms():
        coordinator = device.group.coordinator
        print(f"{device.player_name:<12} {coordinator.ip_address:<15} {device.get_speaker_info()['model_name']}")
    return 0


def cmd_clear_cache(args) -> int:
    print(f"Freed {clear_cache() / 1_000_000:.1f} MB")
    return 0
