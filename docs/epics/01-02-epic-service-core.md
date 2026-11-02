# Service core: config, stable media URLs, rooms, play API, CLI, launchd

**Source spec**: docs/specifications/01-spec-sonos-home-player.md  
**Date**: 2026-11-02  
**Status**: Pending  
**Blocked by**: —

## Configuration
**Story**: 1  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR3

**Acceptance Criteria**:

- With no config file, a partial file and an invalid TOML file, loading gives the defaults (no music folders; port 8737; room `Office`), the merged values, and an error naming the file and line, respectively. With no config file, the music-folder list is empty even when `~/Dropbox` and `~/Music` exist. `[unit]`
- A music folder given as a symlink is stored as its `realpath` target. `[unit]`
- ffmpeg/ffprobe are resolved from the config path, then `PATH`, then `/opt/homebrew/bin`, then `/usr/local/bin`. With `PATH` emptied and a fake binary placed in a stand-in for `/opt/homebrew/bin`, that binary is found. `[unit]`

### Config module
**Task**: 1.1  
**Description**: `src/sonos_cli/config.py` with `tomllib`, dataclass defaults, binary resolution; `media.py` uses the resolved paths.  
**Status**: Pending

---

## Fake Sonos system and live-test helper
**Story**: 2  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR18

**Acceptance Criteria**:

- `tests/fakes.py` provides a fake system with the five rooms. Office is a coordinator plus an invisible satellite. Each fake room has a queue, transport state, position, volume, mute, group membership and a settable `uri`, and is usable anywhere a `soco.SoCo` is. `[unit]`
- `tests/live.py`'s helper restores every member's volume and mute when the fake's restore raises `SoCoUPnPException` with error 402, and reports "source not restored". With the separate `finally` for volume and mute removed, the same test fails (control). `[unit]`

### Fakes and helper
**Task**: 2.1  
**Description**: Fake classes mirroring the SoCo attributes used in `player.py`/`cli.py`; context-manager live helper wrapping `soco.snapshot.Snapshot`.  
**Status**: Pending

---

## Service skeleton with stable media URLs, path guard and origin hardening
**Story**: 3  
**Status**: Pending  
**Blocked by**: Story 1  
**Satisfies**: FR1, FR6, FR7, NFR6, AD1, AD7  
**Effort**: high

**Acceptance Criteria**:

- `sonos service run` (and `--fake-rooms`) starts a Starlette app on the configured port, on all interfaces. `/api/health` returns 200 from `127.0.0.1` and from the LAN address, with `ffmpeg`, `rooms_seen`, host and port. `[integration]`
- `/api/health` reports `ffmpeg: missing` when the configured ffmpeg path doesn't exist. `[integration]`
- `/media/<root-id>/<path>` serves files with correct MIME, `Accept-Ranges` and 206 responses. A file needing conversion is converted on first fetch and served as 16-bit FLAC. A URL built before an app restart returns 200 with the same bytes after it. `[integration]`
- These are refused with 403: `/etc/hosts`, `<music>/../secret.txt`, `..%2f` and `%252e%252e%252f` traversal, a symlink inside the root pointing outside it, and the sibling-prefix path `<music>2/x.wav`. With the guard removed, the same requests succeed (control). `[integration]`
- Through a music root configured as a symlink to the real folder, a file inside is served with 200 and the right bytes. So is `Café – Ünïcode 日本.wav`, requested in both NFC and NFD form. `[integration]`
- These are refused: a request with a forged `Host`, a POST with a foreign `Origin`, a WebSocket upgrade with a foreign `Origin`, and a `text/plain` POST. A media GET with no `Origin` is accepted. With the checks removed, the refused requests are accepted (control). `[integration]`

### Library module
**Task**: 3.1  
**Description**: `src/sonos_cli/library.py`: resolved roots, `root-id`, guard (`realpath` + `is_relative_to` + NFC), stable URL build/parse.  
**Status**: Pending

### App and middleware
**Task**: 3.2  
**Description**: `src/sonos_cli/service.py`: Starlette app, health and media routes, Host/Origin/Content-Type middleware, `service run` subcommand.  
**Status**: Pending

---

## RoomMonitor and rooms API
**Story**: 4  
**Status**: Pending  
**Blocked by**: Story 2  
**Satisfies**: FR4, NFR5, AD8

**Acceptance Criteria**:

- `/api/rooms` matches a stubbed discovery of the fake system: UIDs, names, coordinator, model, members, state, volume, mute. It is served from cache, and the test fails if SoCo is called during the request. `[unit]`
- After the fake changes a room's IP, and separately renames it, a refresh shows the new IP and name under the same UID within one discovery cycle (60 s, driven by a fake clock). `[unit]`
- `GET /api/rooms/{uid}/state` returns cached transport state, track, position and a `source` of `ours`, `other-host:<host>` or `other` (AD7). Polling runs only for rooms that a client is watching or that are playing ours. `[unit]`
- On the real network, with the service running from the terminal, Office is unplugged and replugged. `/api/rooms` shows it again within 60 s with no restart. `[manual]`

### Monitor
**Task**: 4.1  
**Description**: `src/sonos_cli/rooms.py`: discovery thread, per-room poll loop, cache, change subscribers.  
**Status**: Pending

---

## Play and browse API
**Story**: 5  
**Status**: Pending  
**Blocked by**: Story 3, Story 4  
**Satisfies**: FR5, FR10, NFR1

**Acceptance Criteria**:

- `POST /api/play` with a folder holding a 16-bit WAV, a 24-bit/96 k WAV, an MP3 and a fake dataless placeholder queues the three real tracks in name order on the fake coordinator, with stable URLs and the right MIME types. It skips the placeholder with a reason in the response. `[integration]`
- The first track is queued and `play_from_queue(0)` called before the rest are probed: with ffprobe slowed by 1 s per file on a 10-file folder, play starts within 2 s. A folder over 500 tracks queues 500 and says so. `replace` clears the queue first and `append` does not. `[integration]`
- `GET /api/browse?path=` lists folders then audio files by name, 200 per page, without opening any audio file. A test fails if `probe` or `open` is called on an audio file. On a generated 2,000-file folder, the first page returns in under 1 s, and play reaches `play_from_queue` within 3 s. `[integration]`
- With no music folders configured, `GET /api/browse` and `POST /api/play` return 409 with a message naming `config.toml`, and nothing is queued on the fake room. With one folder configured, the same requests succeed (control). `[integration]`
- On muted Office through `tests/live.py`, the median of 5 runs from an API play call on a native-format file to PLAYING is 3 s or less. `[manual]`

### Endpoints
**Task**: 5.1  
**Description**: Play and browse routes on the shared core; background probe/queue worker; dataless detection via `st_flags & SF_DATALESS`.  
**Status**: Pending

---

## Sleep prevention while our audio plays
**Story**: 6  
**Status**: Pending  
**Blocked by**: Story 4  
**Satisfies**: FR17

**Acceptance Criteria**:

- The service starts `caffeinate -i -w <pid>` while a room is PLAYING our URL, or PAUSED on it within the 30-minute grace period. It doesn't for another host's URL, a remote HTTPS URL, or a grouped member reporting `x-rincon:`. It stops caffeinate within 30 s of nothing of ours playing. With the release step removed, the assertion stays held (control). `[unit]`
- On muted Office, `pmset -g assertions` lists the service's caffeinate assertion while playing, and no longer lists it 30 s after stop. `[manual]`

### Assertion manager
**Task**: 6.1  
**Description**: Subscriber on RoomMonitor changes; owns one caffeinate subprocess.  
**Status**: Pending

---

## CLI as a thin client on the shared core
**Story**: 7  
**Status**: Pending  
**Blocked by**: Story 5  
**Satisfies**: FR8, AD3

**Acceptance Criteria**:

- With a stub service listening, `play`, `pause`, `resume`, `stop`, `next`, `prev`, `volume`, `status` and `rooms` each call the matching API endpoint, and `play` sends absolute paths. `[unit]`
- With the connection refused, `play` runs in-process on the shared core with stable URLs, and `pause` calls SoCo directly. With the stub returning 500, the error is shown and neither fallback runs. `[unit]`
- With the service running, `play` on a path outside the music folders exits non-zero with a message naming `config.toml`. `[unit]`
- The existing `tests/test_media.py` tests still pass unchanged. `[unit]`

### Client and fallback
**Task**: 7.1  
**Description**: `src/sonos_cli/client.py`; rebuild `cmd_play`'s in-process path on `library.py`, removing `TrackServer`'s own Range and token code.  
**Status**: Pending

---

## launchd install, uninstall and status
**Story**: 8  
**Status**: Pending  
**Blocked by**: Story 3, Story 5, Epic 01-01-epic-spikes  
**Satisfies**: FR2, NFR3, AD6  
**Effort**: high

**Acceptance Criteria**:

- `sonos service install` writes `~/Library/LaunchAgents/local.sonos-cli.plist`, running the executable chosen in AD6 with `KeepAlive`, `RunAtLoad` and the log path, and loads it. Once it's started by launchd, `/api/health` shows `ffmpeg: ok`, and `/api/rooms` lists all 5 rooms. `[manual]`
- After `kill -9` of the service process, `/api/health` is 200 again within 10 s. `[manual]`
- `sonos service status` reports loaded, healthy and the log path. After `sonos service uninstall`, the plist is gone and nothing listens on the port. `[manual]`
- Idle for 5 minutes with no UI open, the launchd-run service stays under 150 MB RSS and averages under 1% CPU. The same holds with one UI tab polling, once the UI exists, re-measured in Epic 01-03. `[manual]`
- The plist content is generated by a function covered by a test asserting the executable, arguments, `KeepAlive` and log paths. `[unit]`

### Service subcommands
**Task**: 8.1  
**Description**: `service install|uninstall|status` using `launchctl bootstrap/bootout gui/$UID`.  
**Status**: Pending

---
