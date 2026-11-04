"""Command-line entry point: `sonos play ~/Dropbox/Music/album` and friends."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config
from .client import ServiceClient, ServiceUnavailable
from .media import clear_cache
from .player import RoomNotFound, find_room, list_rooms
from .rooms import validate_room_name
from .server import EmbeddedServer, local_ip_towards

POLL_SECONDS = 2
STOPPED_GRACE_SECONDS = 10


def _resolve_room_and_ip(args) -> None:
    """Flags beat the environment: an explicit -r ignores $SONOS_IP, and $SONOS_ROOM only fills in a missing -r."""
    args.room_explicit = args.room is not None
    env_ip = os.environ.get("SONOS_IP")
    if args.ip is None and env_ip:
        args.ip = None if args.room_explicit else env_ip
    if args.room is None:
        args.room = os.environ.get("SONOS_ROOM") or None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _resolve_room_and_ip(args)
    try:
        config.get()                  # an invalid config.toml is reported once, up front, naming the file
        return args.func(args)
    except (RoomNotFound, FileNotFoundError, ValueError) as exc:
        print(f"sonos: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sonos", description="Play files from this Mac on Sonos speakers.")
    parser.add_argument("-r", "--room", default=None,
                        help="Sonos room (default: $SONOS_ROOM, else `room` in config.toml, else Office)")
    parser.add_argument("--ip", default=None,
                        help="a speaker's IP: picks the room it belongs to, and skips discovery without the service "
                             "(or $SONOS_IP, which an explicit -r overrides). With -r, the IP must belong to that room")
    sub = parser.add_subparsers(dest="command", required=True)

    service = sub.add_parser("service", help="run or manage the background service")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    run = service_sub.add_parser("run", help="run the service in the foreground")
    run.add_argument("--fake-rooms", action="store_true", help="use an in-memory Sonos system (development and tests)")
    run.add_argument("--no-local-clients", action="store_true",
                     help="with --fake-rooms: treat every client, even localhost, as another device (to see the read-only folder view)")
    run.add_argument("--host", default="0.0.0.0")
    run.add_argument("-p", "--port", type=int, default=None, help="port (default: config.toml, else 8737)")
    run.set_defaults(func=cmd_service_run)
    install = service_sub.add_parser("install", help="run the service at login with launchd, and restart it if it stops")
    install.add_argument("--allow-editable", action="store_true", help="install even from an editable source checkout")
    install.set_defaults(func=cmd_service_install)
    service_sub.add_parser("uninstall", help="stop the service and remove its launchd agent").set_defaults(func=cmd_service_uninstall)
    service_sub.add_parser("status", help="is the launchd agent loaded and the service answering?").set_defaults(func=cmd_service_status)

    play = sub.add_parser("play", help="replace the room's queue with these files or folders and play them")
    play.add_argument("paths", nargs="+", type=Path)
    play.add_argument("-s", "--shuffle", action="store_true")
    play.add_argument("-a", "--append", action="store_true", help="add to the queue instead of replacing it")
    play.add_argument("-p", "--port", type=int, default=0,
                      help="without the service: port to serve files on (default: any free port)")
    play.add_argument("--idle-minutes", type=float, default=30,
                      help="without the service: exit after being paused this long (default: 30)")
    play.set_defaults(func=cmd_play)

    for name, action, help_text in [
        ("pause", "pause", "pause"),
        ("resume", "resume", "resume playback"),
        ("stop", "stop", "stop"),
        ("next", "next", "skip to the next track"),
        ("prev", "prev", "go back a track"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=_transport(action))

    volume = sub.add_parser("volume", help="show or set the room's volume (0-100)")
    volume.add_argument("level", nargs="?", type=int)
    volume.set_defaults(func=cmd_volume)

    send = sub.add_parser("send", help="queue the audio on a web page: sonos send https://example.com/release/")
    send.add_argument("url")
    send.add_argument("-a", "--append", action="store_true", help="add to the queue instead of replacing it")
    send.set_defaults(func=cmd_send)
    rename = sub.add_parser("rename", help="rename a room: sonos rename Office Studio")
    rename.add_argument("old")
    rename.add_argument("new")
    rename.set_defaults(func=cmd_rename)
    group = sub.add_parser("group", help="put a room in another room's group: sonos group Kitchen Lounge; or --all")
    group.add_argument("member", nargs="?", help="the room that joins")
    group.add_argument("into", nargs="?", help="the room whose group it joins")
    group.add_argument("--all", action="store_true", help="group every room with -r/--room (default room), a.k.a. party mode")
    group.set_defaults(func=cmd_group)
    ungroup = sub.add_parser("ungroup", help="take a room out of its group: sonos ungroup Kitchen")
    ungroup.add_argument("room")
    ungroup.set_defaults(func=cmd_ungroup)

    sub.add_parser("status", help="show what the room is playing").set_defaults(func=cmd_status)
    sub.add_parser("rooms", help="list Sonos rooms on the network").set_defaults(func=cmd_rooms)
    sub.add_parser("clear-cache", help="delete converted copies of files").set_defaults(func=cmd_clear_cache)
    return parser


DIRECT_ACTIONS = {
    "pause": lambda c: c.pause(), "resume": lambda c: c.play(), "stop": lambda c: c.stop(),
    "next": lambda c: c.next(), "prev": lambda c: c.previous(),
}


def _client() -> ServiceClient:
    return ServiceClient(config.get().port)


def _room_name(args) -> str:
    return args.room or config.get().room


def _uid_by_name(rooms: list[dict], name: str) -> str:
    for room in rooms:
        if room["uid"] == name or room["name"].casefold() == name.casefold():
            return room["uid"]
    raise RoomNotFound(name, sorted({r["name"] for r in rooms}))


def _room(client: ServiceClient, args) -> dict:
    """The room the user means: the one the --ip speaker belongs to if given, else the named room."""
    rooms = client.get("/api/rooms")
    if args.ip:
        for room in rooms:
            if args.ip == room["ip"] or any(m["ip"] == args.ip for m in room["members"]):
                if args.room_explicit and args.room.casefold() not in (room["name"].casefold(), room["uid"].casefold()):
                    raise ValueError(f"{args.ip} belongs to {room['name']}, not {args.room}")
                return room
        raise ValueError(f"No Sonos room has a speaker at {args.ip} (rooms: {', '.join(sorted(r['name'] for r in rooms))})")
    uid = _uid_by_name(rooms, _room_name(args))
    return next(r for r in rooms if r["uid"] == uid)


def _room_uid(client: ServiceClient, args) -> str:
    return _room(client, args)["uid"]


def _direct_room(args):
    """No service: the coordinator of the room the user means, found directly."""
    coordinator = find_room(_room_name(args), args.ip)
    if args.ip and args.room_explicit and coordinator.player_name.casefold() != args.room.casefold():
        raise ValueError(f"{args.ip} belongs to {coordinator.player_name}, not {args.room}")
    return coordinator


def _transport(action: str):
    def run(args) -> int:
        try:
            client = _client()
            client.post(f"/api/rooms/{_room_uid(client, args)}/transport", {"action": action})
            return 0
        except ServiceUnavailable:
            DIRECT_ACTIONS[action](_direct_room(args))
            return 0
    return run


def cmd_play(args) -> int:
    paths = [Path(os.path.abspath(p.expanduser())) for p in args.paths]      # the service wants absolute paths
    mode = "append" if args.append else "replace"
    try:
        client = _client()
        room = _room(client, args)
        uid = room["uid"]
        result = client.post("/api/play", {"room": uid, "paths": [str(p) for p in paths], "mode": mode,
                                           "shuffle": args.shuffle})
    except ServiceUnavailable:
        return _play_in_process(args, paths, mode)
    _print_play_result(result, room["name"], "the service keeps serving them")
    return 0


def _print_play_result(result: dict, room: str, note: str) -> None:
    verb = "Playing" if result["started"] else "Added"
    print(f"{verb} {result['total']} track(s) on {room}; {note}.")
    if result.get("truncated"):
        t = result["truncated"]
        print(f"Only the first {t['limit']} of {t['found']} tracks were queued.", file=sys.stderr)
    for item in result.get("skipped", []):
        print(f"Skipping {item['path']}: {item['reason']}", file=sys.stderr)
    if result.get("skipped_total", 0) > len(result.get("skipped", [])):
        print(f"… and {result['skipped_total'] - len(result['skipped'])} more skipped.", file=sys.stderr)


def _play_in_process(args, paths: list[Path], mode: str) -> int:
    """No service is running: do the same thing with the shared core, serving the files from this process."""
    from .library import Library
    from .playback import Player, PlayError
    if mode == "append":
        raise ValueError("--append needs the service running, because the files must keep being served after this "
                         "command exits. Start it with `sonos service run` (or `sonos service install`).")
    for p in paths:
        if not p.exists():
            raise FileNotFoundError(f"No such file or folder: {p}")
    resolved = [p.resolve() for p in paths]                     # a link's target decides which folder is served
    library = Library.create({p if p.is_dir() else p.parent for p in resolved})
    cfg = config.get()
    coordinator = _direct_room(args)
    server = EmbeddedServer(library, cfg, local_ip_towards(coordinator.ip_address), args.port)
    server.start()
    player = Player(library, cfg, lambda uid: coordinator, lambda c: server.base_url)
    caffeinate = None
    try:
        try:
            result = player.play(coordinator.uid, [str(p) for p in resolved], mode, args.shuffle)
        except PlayError as e:
            raise ValueError(str(e)) from None
        _print_play_result(result, coordinator.player_name, "serving them from this command until they finish (Ctrl-C stops)")
        # Playback happens on the speaker, so macOS sees no activity; keep it awake while we serve.
        caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        try:
            _serve_until_done(coordinator, server.base_url + "/media/", result["total"], args.idle_minutes * 60)
        except KeyboardInterrupt:
            if coordinator.get_current_track_info()["uri"].startswith(server.base_url):
                coordinator.stop()
            print("\nStopped.")
    finally:
        player.close()
        if caffeinate:
            caffeinate.terminate()
        server.stop()
    return 0


def cmd_service_install(args) -> int:
    from . import launchd
    return launchd.install(allow_editable=args.allow_editable)


def cmd_service_uninstall(args) -> int:
    from . import launchd
    return launchd.uninstall()


def cmd_service_status(args) -> int:
    from . import launchd
    return launchd.status()


def cmd_service_run(args) -> int:
    from . import service
    if args.no_local_clients and not args.fake_rooms:
        print("sonos: --no-local-clients only works with --fake-rooms", file=sys.stderr)
        return 2
    return service.run(config.get(), host=args.host, port=args.port, fake_rooms=args.fake_rooms, no_local_clients=args.no_local_clients)


def _serve_until_done(coordinator, our_prefix: str, count: int, idle_limit: float) -> None:
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

        ours = info["uri"].startswith(our_prefix)
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


def _direct_device(name: str):
    """No service: the named room's own speaker, found by discovery."""
    devices = list_rooms()
    for d in devices:
        if d.player_name.casefold() == name.casefold():
            return d
    raise RoomNotFound(name, sorted({d.player_name for d in devices}))


def cmd_send(args) -> int:
    try:
        client = _client()
        room = _room(client, args)
        result = client.post("/api/send", {"room": room["uid"], "url": args.url, "mode": "append" if args.append else "replace"})
    except ServiceUnavailable:
        raise ValueError("`sonos send` needs the service running, because it fetches the tracks for the speaker. "
                         "Start it with `sonos service run` (or `sonos service install`).") from None
    verb = "Playing" if result["started"] else "Added"
    print(f"{verb} {result['total']} track(s) from {result.get('title') or args.url} on {room['name']}.")
    return 0


def cmd_rename(args) -> int:
    try:
        client = _client()
        uid = _uid_by_name(client.get("/api/rooms"), args.old)
        client.post(f"/api/rooms/{uid}/rename", {"name": args.new})
    except ServiceUnavailable:
        new = validate_room_name(args.new)
        device = _direct_device(args.old)
        if any(d.player_name.casefold() == new.casefold() and d.uid != device.uid for d in list_rooms()):
            raise ValueError(f"Another room is already called {new!r}")
        device.player_name = new
    print(f"Renamed {args.old} to {args.new.strip()}.")
    return 0


def cmd_group(args) -> int:
    if args.all:
        if args.member or args.into:
            raise ValueError("`sonos group --all` takes no room names; pick the room the others join with -r")
    elif not (args.member and args.into):
        raise ValueError("Say which room joins which: `sonos group Kitchen Lounge` (or `sonos group --all`)")
    try:
        client = _client()
        rooms = client.get("/api/rooms")
        if args.all:
            into = _room_uid(client, args)
            client.post("/api/rooms/group-all", {"into": into})
            print("Grouped all rooms.")
        else:
            client.post(f"/api/rooms/{_uid_by_name(rooms, args.member)}/group", {"into": _uid_by_name(rooms, args.into)})
            print(f"{args.member} now plays with {args.into}.")
    except ServiceUnavailable:
        if args.all:
            _direct_room(args).partymode()
            print("Grouped all rooms.")
        else:
            member, target = _direct_device(args.member), _direct_device(args.into)
            leader = target.group.coordinator
            if leader.uid != member.uid and member.group.coordinator.uid != leader.uid:
                member.join(leader)
            print(f"{args.member} now plays with {args.into}.")
    return 0


def cmd_ungroup(args) -> int:
    try:
        client = _client()
        client.post(f"/api/rooms/{_uid_by_name(client.get('/api/rooms'), args.room)}/ungroup", {})
    except ServiceUnavailable:
        _direct_device(args.room).unjoin()
    print(f"{args.room} now plays on its own.")
    return 0


def cmd_volume(args) -> int:
    try:
        client = _client()
        uid = _room_uid(client, args)
        if args.level is not None:
            print(client.post(f"/api/rooms/{uid}/volume", {"level": max(0, min(100, args.level))})["volume"])
        else:
            print(client.get(f"/api/rooms/{uid}/state")["volume"])
        return 0
    except ServiceUnavailable:
        coordinator = _direct_room(args)
        if args.level is not None:
            coordinator.group.volume = max(0, min(100, args.level))
        print(coordinator.group.volume)
        return 0


def cmd_status(args) -> int:
    try:
        client = _client()
        uid = _room_uid(client, args)
        st = client.get(f"/api/rooms/{uid}/state")
        room = _room(client, args)
        members = ", ".join(sorted(m["ip"] for m in room["members"]))
        print(f"{room['name']} ({members}): {st['state']}")
        track = st["track"]
        if track.get("title") or track.get("uri"):
            print(f"  {track.get('title') or track.get('uri')}  {st.get('position')}/{st.get('duration')}")
        print(f"  volume {st['volume']}{' (muted)' if st['mute'] else ''}")
        return 0
    except ServiceUnavailable:
        coordinator = _direct_room(args)
        state = coordinator.get_current_transport_info()["current_transport_state"]
        info = coordinator.get_current_track_info()
        members = ", ".join(sorted(m.ip_address for m in coordinator.group.members))
        print(f"{coordinator.player_name} ({members}): {state}")
        if info.get("title") or info.get("uri"):
            print(f"  {info.get('title') or info.get('uri')}  {info.get('position')}/{info.get('duration')}")
        print(f"  volume {coordinator.group.volume}{' (muted)' if coordinator.group.mute else ''}")
        return 0


def cmd_rooms(args) -> int:
    try:
        for room in _client().get("/api/rooms"):
            print(f"{room['name']:<12} {room['ip']:<15} {room['model']}")
        return 0
    except ServiceUnavailable:
        for device in list_rooms():
            coordinator = device.group.coordinator
            print(f"{device.player_name:<12} {coordinator.ip_address:<15} {device.get_speaker_info()['model_name']}")
        return 0


def cmd_clear_cache(args) -> int:
    print(f"Freed {clear_cache() / 1_000_000:.1f} MB")
    return 0
