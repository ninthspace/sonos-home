# Spec: Sonos Home Player

**Date**: 2026-11-02  
**Source**: docs/briefs/01-brief-sonos-home-player.md

## Problem Summary

`sonos play` (src/sonos_cli/cli.py `cmd_play`) plays local files on a Sonos room, but only while a foreground command keeps its temporary `TrackServer` (src/sonos_cli/server.py) alive. Its track URLs are random in-memory tokens, so they die with the process even though the speaker keeps the queue. There's no UI, no playlists, no room management and no way to play browser audio. This spec turns the tool into an always-on background service with a LAN web UI, playlists, room management, page sending (direct audio links, first of all the user's Faircamp site) and live Chrome tab streaming. It targets five Sonos S1 players: Office, a Play:1 stereo pair with 192.168.0.21 as coordinator and .27; Bedroom .31; Kitchen .29; Lounge .26.

## Functional Requirements

### Must Have

- **FR1**: `sonos service run` starts one long-running process. On a fixed port (default 8737, configurable), on all interfaces, it serves the web UI, a JSON API and the audio rooms fetch. `GET /api/health` reports `ffmpeg: ok|missing`, `rooms_seen: N` and the service's host and port. — AC: with the service running, `/api/health` answers 200 from `127.0.0.1` and from the Mac's LAN address, and reports `ffmpeg: missing` when ffmpeg can't be found. `[integration]`
- **FR2**: `sonos service install` writes and loads a launchd LaunchAgent (`~/Library/LaunchAgents/local.sonos-cli.plist`) that runs the service at login and restarts it if it exits. The agent runs the stable executable chosen in spike 01-01 Story 1, never an editable checkout. `sonos service uninstall` unloads and removes it. `sonos service status` reports loaded/healthy and the log path. — AC: after `install`, with the service started by launchd (not a terminal), `/api/health` shows ffmpeg ok and `/api/rooms` lists all 5 rooms. After `kill -9` of the service, it's healthy again within 10 s. After `uninstall`, no plist and no listener on the port. `[manual]`
- **FR3**: Configuration lives in `~/Library/Application Support/sonos/config.toml`: music folders (no default: the list is empty until the user adds a folder), port, default room, and the ffmpeg/ffprobe paths. Each music folder is resolved with `realpath` when the config loads. A missing file means defaults. ffmpeg/ffprobe are looked up in this order: the config path, then `PATH`, then `/opt/homebrew/bin`, then `/usr/local/bin`. — AC: no file gives no music folders, port 8737 and room `Office`. A partial file gives merged values, and an invalid file a clear error. A symlinked music folder is stored as its resolved target. ffmpeg is found in `/opt/homebrew/bin` when it isn't on `PATH`. `[unit]`
- **FR4**: A RoomMonitor (AD8) discovers rooms at start-up and every 60 s off the request path, and on demand. It identifies rooms by UID (`RINCON_…`). `GET /api/rooms` returns each room's UID, name, coordinator UID and IP, model, group members, transport state, volume and mute, from the monitor's cache. — AC: the room list matches a stubbed discovery result, a refresh picks up a changed IP and a renamed room under the same UID, and `/api/rooms` answers without calling SoCo. `[unit]`
- **FR5**: `POST /api/play` takes a room UID, a list of local paths (files or folders) and a mode (`replace` or `append`), then queues and plays them on the room's group coordinator. Each track's URL is stable (AD7). Folders expand in name order, capped at 500 tracks with a note in the response. The first track is probed and starts playing before the rest are probed and queued in the background. Dataless (online-only) Dropbox placeholders are skipped with a reason and never read. Conversion follows the existing rules (`media.needs_conversion`, `media.convert`) and happens lazily on the first fetch. — AC: a mixed folder (16-bit WAV, 24-bit/96 k WAV, MP3, one fake dataless file) is queued in name order, has the right MIME types, serves the hi-res file as 16-bit FLAC and skips the placeholder with a reason. `[integration]`
- **FR6**: Every file path the service accepts or serves is resolved with `realpath` and must be `is_relative_to` a resolved allowed root: music folders, the cache directory or the playlists directory. This applies to stable media URLs, `/api/play` paths, the browse path, playlist names and M3U entries. Filenames are compared after Unicode NFC normalisation. A path outside the allowed roots is refused with 403. — AC (refused): `/etc/hosts`, `<music>/../secret.txt`, `..%2f` and double-encoded `%252e%252e%252f` traversal, `<music>/link-to-home/x.wav` (a symlink pointing outside), the sibling-prefix path `<music>2/x.wav`, the playlist name `../../x`, and an M3U entry `/etc/hosts`. The same inputs succeed with the guard removed (control). `[integration]`
- **FR7**: A file inside the allowed roots is served when requested through a music root that is itself a symlink (as `~/Dropbox` is). The same applies to `Café – Ünïcode 日本.wav` requested in NFC and in NFD. — AC: these floor cases return 200 with the right bytes. `[integration]`
- **FR8**: The existing CLI commands (`play`, `pause`, `resume`, `stop`, `next`, `prev`, `volume`, `status`, `rooms`) go through the service API whenever `127.0.0.1:<port>` accepts a connection. Any API error is reported, not hidden by a fallback. Only when the connection is refused do non-play commands use SoCo directly and `play` use the in-process path. `play` and the service share one core: path expansion, probe, convert, queue-item building and stable URLs (AD3). `play` turns paths into absolute ones. With the service running, a path outside the music folders is an error that names the config file. — AC: each command calls the API when a stub service is up. `play` uses the in-process path, and `pause` direct SoCo, when the connection is refused. A 500 from the service is reported and does not fall back. An outside-roots path gives the config hint. `[unit]`
- **FR9**: The web UI, at `http://<mac>:8737/`, lets the user browse the music folders (folders first, then audio files, by name; listing never probes or opens audio files; paginated at 200 entries) and filter the current folder by name. It can play or append a file or a folder on the selected room, and shows the room's current track, transport state, position and source ("this Mac", "from <other host>" or "other source"). It has play/pause, next, previous and volume (group volume) controls, and shows a clear "service unreachable" state when polling fails. With no music folders configured, the browse view shows the config file's path and an example `music_folders` entry instead of a listing. — AC: in a browser against the service with fake rooms, browsing into a folder, playing a file and changing the volume each change the fake room, and the page shows the new state within 3 s. Stopping the service shows the unreachable state. With no music folders configured, the browse view shows the config path and example. `[browser]`
- **FR10**: Folder listing stays fast on huge folders. — AC: a folder of 2,000 generated tiny audio files returns its first page in under 1 s, and playing it reaches `play_from_queue` on the fake room within 3 s. `[integration]`
- **FR11**: The user can rename a room, add a room to another's group, remove a room from its group, and group all rooms, from both the UI and the CLI (`sonos rename`, `sonos group`, `sonos ungroup`). Rooms are addressed by UID in the API; the CLI and config accept names and resolve them to UIDs. Stereo pairs aren't created or split. Transport, queue and volume act on the group coordinator. — AC: against a fake system including an invisible stereo-pair satellite, the API calls `player_name` set / `join` / `unjoin` / `partymode` on the right device, and `partymode` leaves the satellite alone. After a rename, the same UID still plays. `[unit]`
- **FR12**: Playlists can be created, renamed, deleted, edited (add, remove, reorder tracks) and played on a room. Each is stored as a UTF-8 extended M3U (`.m3u8`, `#EXTM3U`, absolute paths) in `~/Library/Application Support/sonos/playlists/`, under a safe filename derived from its name. Tracks whose files are missing, or outside the allowed roots, are shown as unavailable and skipped on play. — AC: a playlist with a non-ASCII track survives a service restart and round-trips through M3U, and playing it skips a deleted file and an outside-roots entry. `[unit]`
- **FR13**: `POST /api/send` takes a room UID and either a page URL, which the service fetches, or page HTML plus its base URL, which the extension sends. It extracts audio the room can play natively and queues it on the room straight from the remote URLs. Faircamp release pages yield one entry per track: the `audio/mpeg` source with the track title, in order. Other pages yield `<audio>`/`<source>` URLs and links to `.mp3`/`.m4a`/`.aac`/`.flac`/`.ogg`. Opus and other formats S1 can't play are skipped. URLs are made absolute and percent-encoded. Each queue item's `protocolInfo` comes from its MIME type. A page with nothing playable gives a clear error. `sonos send <url>` and a UI URL field use the same endpoint. — AC: saved copies of a shtl-experiment.com release page and of a generic page (with relative, space-containing and non-ASCII URLs) give the expected ordered list of absolute, percent-encoded tracks, with no Opus URLs. `[unit]`
- **FR14**: A Chrome extension (Manifest V3, loaded unpacked from `extension/`) has a popup that lists rooms from the service and remembers the last room by UID. When the popup opens, it sends the current page's rendered HTML to `POST /api/send/preview`. It shows **Send page** if the preview finds tracks within 2 s, and **Checking…** then **Stream tab** otherwise. It offers **Stream tab** / **Stop** on every page. — AC: on a local test page with direct MP3 links the popup offers Send page, and on a page without them only Stream tab. The chosen room is remembered across popup opens and across a rename of that room. `[browser]`
- **FR15**: Tab streaming. The extension's offscreen document captures the tab (`tabCapture` stream ID → `getUserMedia`), runs an `AudioContext({sampleRate: 48000})` with an AudioWorklet producing 16-bit stereo PCM, and sends it over a WebSocket to `/live/ingest?room=<uid>`. The service encodes it once with ffmpeg (MP3 320 kbps CBR) into a live stream the room plays as `x-rincon-mp3radio://<mac-ip>:<port>/live/<id>.mp3` (AD4). Streaming state lives in the offscreen document, so the popup can close. Only one stream runs at a time. The stream ends when the user clicks Stop, when the tab closes, or when the room switches to another source; the extension is told which. — AC: on a test page playing a 440 Hz tone, Stream tab produces `/live/<id>.mp3` that decodes to 440 Hz ± 5 Hz, and closing the tab ends the stream within 5 s. `[browser]`
- **FR16**: The live stream serves listeners from a shared buffer. — AC (all with fake rooms): a listener joining mid-stream gets decodable MP3 starting on a frame boundary. A listener that stops reading doesn't stall a second listener. Responses carry no chunked encoding and no `icy-metaint`. A listener that reconnects within 10 s gets audio again. Ingest running 60 s with no listener doesn't grow memory beyond the buffer. When ingest closes, `/live/<id>.mp3` ends rather than hanging. When the fake room's URI changes to another source, ffmpeg exits within 5 s, leaving no orphan ffmpeg process. `[integration]`
- **FR17**: While any room's coordinator is playing or paused on a URL this service serves (AD7's definition of "ours"), the service holds a sleep-prevention assertion (`caffeinate -i -w <service pid>`). This includes a pause of up to 30 minutes (configurable). Remote HTTPS tracks from FR13 and another Mac's URLs don't count. The assertion is released within 30 s of nothing of ours playing. — AC: the assertion is held for a PLAYING room on our URL and for a room PAUSED on our URL within the grace period. It isn't held for another host's URL, a remote HTTPS URL, or a grouped member reporting `x-rincon:`. It's released within 30 s of STOPPED. With the release step removed, it stays held (control). `[unit]`
- **FR18**: A live-test helper (`tests/live.py`) saves Office's state, mutes every member, and afterwards restores volume and mute in their own `finally` step before attempting the queue and source. It reports a source that can't be restored instead of failing silently. — AC: with a fake whose restore raises `SoCoUPnPException` 402, every member's volume and mute are still restored and the helper reports "source not restored". With the separate `finally` removed, volume and mute aren't restored (control). `[unit]`

### Should Have

- **FR19**: The UI shows the selected room's queue, highlights the current track, labels items served by another host, and lets the user jump to a track or clear the queue. — AC: jumping to track 3 in a stubbed queue calls `play_from_queue(2)`, and an item with another host's URL is labelled with that host. `[unit]`
- **FR20**: When streaming, the extension can also play the tab locally (off by default). "On" routes the captured stream to the AudioContext's destination; "off" needs no code, since capture already mutes the tab. — AC: with the option on, the test page's tone is audible locally while streaming, and with it off it isn't. `[browser]`

### Could Have

- **FR21**: Cover art: the UI and Sonos show embedded or folder artwork for local files. — AC: a file with an embedded cover gets an `albumArtURI` the service serves. `[unit]`
- **FR22**: A Homebrew tap with a `service do` block.

### Won't Have (this iteration)

- Login on the web UI (the user's decision). Access from outside the home network.
- Safari or Firefox extensions.
- Creating or splitting stereo pairs.
- Playlists synced between Macs (the user chose local storage).
- AirPlay emulation, and video/audio sync for browser audio.
- Counting Sonos plays in shtl-experiment.com's `plays.js`.
- More than one live stream at a time.

## Non-Functional Requirements

- **NFR1**: From an API play call on a natively playable local file to Office reporting PLAYING, the median of 5 muted runs is 3 s or less. Measured with the live-test helper. `[manual]`
- **NFR2**: Live tab streaming delay of 6 s or less, tab to speaker, measured by the user. They stream a click track to Office and record the tab and the speaker together on their phone. Spike 01-01 Story 2 gives a first figure by timestamping ingest against the first speaker GET. `[user]`
- **NFR3**: Idle service under 150 MB RSS and averaging under 1% CPU over 5 minutes. Measured with `ps` with no UI open, and again with one UI tab polling. `[manual]`
- **NFR4**: Installation needs only Python 3.12+, uv and Homebrew ffmpeg. The UI and extension have no build step.
- **NFR5**: After a speaker changes IP, the service finds it again within 60 s without a restart (unit test with fake discovery), and after a speaker is unplugged and replugged. `[unit]` + `[manual]`
- **NFR6**: Browser-origin hardening (no login, but no drive-by control):
  - A request whose `Host` isn't the Mac's IP, its hostname, `localhost` or `127.0.0.1` is refused (stops DNS rebinding).
  - Every state-changing request, and every WebSocket upgrade, must carry an `Origin` that is the service's own or `chrome-extension://<configured id>`.
  - State-changing requests must be `Content-Type: application/json`.
  - Speakers fetching media (no `Origin` header) are unaffected.
  - AC: a forged Host, a forged Origin on POST and on the WebSocket, and a `text/plain` POST are each refused. The same requests are accepted with the checks removed (control). `[integration]`

## Architecture Decisions

### AD1: One Python service built on Starlette + uvicorn
The service is one process hosting the API, UI, media serving and the live WebSocket. It has `FileResponse` with Range support and raw ASGI responses where streaming needs control over headers (AD4). Blocking SoCo calls run in a thread pool, and only the RoomMonitor makes them for reads. Alternatives: the stdlib `ThreadingHTTPServer`, which has no WebSockets, or aiohttp, which would work equally well.

### AD2: UI is static HTML + vanilla JS, no build step
Shipped in `src/sonos_cli/web/`. It polls `GET /api/rooms/{uid}/state`, served from the RoomMonitor's cache, every 2 s.

### AD3: Shared playback core; the CLI is a thin client with one narrow fallback
`media.py`, a new `library.py` (path expansion, guard, stable URLs) and `player.py` form the core both paths use. The in-process `TrackServer` is reimplemented on the core and kept only for when the service isn't listening (FR8), so fixes aren't made twice.

### AD4: Live streams: PCM in, MP3 out through a ring buffer

- **One stream per room.** A live-session registry keyed by room UID; starting a new stream for a room replaces the old one.
- **Ingest.** PCM (s16le, 48 kHz, stereo) over the WebSocket is piped to ffmpeg, which encodes MP3 320 CBR. A reader always drains ffmpeg's output into a bounded ring buffer of MP3 frames, dropping the oldest, so ffmpeg never blocks.
- **Listeners.** Each starts at a frame boundary a short burst behind the live edge, with its own bounded queue, and a slow listener drops frames rather than stalling others. Responses are HTTP/1.0-style: `Content-Type: audio/mpeg`, no Content-Length, no chunking, `Connection: close`, and no ICY metadata.
- **Control.** Messages on the ingest socket tell the extension `ended: stop|tab-closed|room-switched|service-error`.
- **Spike first.** Spike 01-01 Story 2 proves Office plays `x-rincon-mp3radio` from this design, fed from a WAV, before any extension work.

### AD5: Remote audio plays from the source
FR13 queues remote HTTPS URLs directly, so the Mac isn't needed during playback. FR13 waits on spike 01-01 Story 2. If S1 can't play the site's HTTPS URLs (an old TLS root store, for example), the service proxies them under `/remote/…` instead, and this AD is amended.

### AD6: launchd agent with a stable executable identity
The plist runs a fixed executable path, with `KeepAlive` and `RunAtLoad`. Logs go to `~/Library/Logs/sonos/service.log`. Spike 01-01 Story 1 decides the executable, e.g. a small ad-hoc-signed app-bundle wrapper with `NSLocalNetworkUsageDescription` and `NSBonjourServices`, or the uv tool's Python with a documented re-grant step. The goal is that Local Network permission and access to `~/Library/CloudStorage` survive `uv tool upgrade`.

### AD7: Stable, stateless media URLs

- **Form.** Local tracks are `/media/<root-id>/<percent-encoded path relative to the root>`, where `root-id` is a short hash of the resolved root. Every request re-resolves the path, guards it (FR6), and converts it if needed (cached by the existing `cached_conversion_path` key). A service restart therefore never breaks a queue.
- **Host.** URLs carry the Mac's LAN IP, because S1 can't resolve `.local`. An IP change still breaks queued URLs, so the README recommends a DHCP reservation for the Mac.
- **What counts as ours.** A URL is "ours" when its port is this service's port, its host is one of this Mac's addresses, and its path starts with `/media/` or `/live/`.

### AD8: RoomMonitor owns room state; Sonos owns queues and groups
One RoomMonitor per service does discovery and polls each room that a UI is watching, or that is playing something of ours, every 2 s. It caches the state and publishes changes to the UI API, the live sessions (FR15's room-switched) and the sleep assertion (FR17). The service keeps no copy of queues or groups. The only state it owns is config, playlists, the conversion cache and live sessions.

## Multiple Macs

Each Mac may run its own service. Each serves, and keeps awake for, only its own URLs (AD7). The UI labels sources served by another host. A "replace" from one Mac replaces whatever the room was playing, including another Mac's queue; that's the expected Sonos behaviour. A laptop with its lid closed sleeps whatever assertion is held, and its queues then stop. Use the Mac that stays awake as the host for long sessions.

## Scope

### In Scope
Background service, LaunchAgent install, config, RoomMonitor, local file playback through the service with stable URLs, path guard and origin hardening, CLI on the API, web UI, room management, playlists, page sending, Chrome extension, live tab streaming, sleep prevention, the live-test helper.

### Out of Scope
Everything under Won't Have. Changes to the user's Faircamp site.

## Testing Strategy

- Test command: `uv run pytest`.
- `[unit]`: pytest with SoCo replaced by an in-memory fake system (`tests/fakes.py`: rooms, groups, an invisible satellite, queues, transport state). No network.
- `[integration]`: pytest that starts the Starlette app (in-process with `httpx`/`TestClient`, or uvicorn on an ephemeral port for raw streaming and WebSocket tests) against fake rooms, with real ffmpeg/ffprobe and generated test tones.
- `[browser]`: `do` drives the web UI with claude-in-chrome against `sonos service run --fake-rooms`. It drives the extension through the automation harness chosen in spike 01-01 Story 3 (e.g. Chrome launched with `--load-extension` and a tab-capture allowlist flag), since loading an unpacked extension and granting tab capture can't be done through claude-in-chrome. Local test pages are served by the test harness (a page with MP3 links, a page playing a 440 Hz tone). Live speakers are never needed for `[browser]`.
- `[manual]`: `do` runs the step in the session, e.g. launchd install or a live check on Office through `tests/live.py` (FR18).
- `[user]`: only the user can prove these, because they involve hearing the speakers, their phone, a real shtl-experiment.com page or YouTube in their own Chrome. They're all in the final user-checks epic.
- **Live-test rules**: anything that touches a real room uses Office through `tests/live.py`. It mutes every member first and restores volume and mute in a separate `finally` before the queue or source. (In the 2026-11-02 session, restoring a Spotify Connect source raised UPnP 402 and skipped the rest of the restore.)

## Assumptions

- No login on the UI (the user's choice). FR6 and NFR6 are the protection: the path guard limits what can be read, and the Host/Origin checks stop websites in the user's own browser from driving the service. This does not reopen the login decision.
- No default music folder (the user's choice). The service browses and plays nothing until a folder is added to `config.toml`; every place that would list or play files says so and names the file.
- Default port 8737, fixed so phone bookmarks keep working.
- Playlists are stored separately on each Mac (the user's choice). They hold absolute paths, so a playlist copied to another Mac works only where the paths match.
- Tab capture mutes the tab locally by default, which matches the user's goal of sound only from the speakers.
- MP3 320 CBR for live streams, because `x-rincon-mp3radio` is the most widely supported live format on S1.
- The service polls rather than using UPnP event subscriptions. One monitor bounds the load on the speakers.
- With the service running, `sonos play` on a file outside the music folders is an error with a config hint, rather than starting a second server. One server per Mac keeps "ours" unambiguous (from the architect's review). The user can add folders to the config.
- The extension sends rendered page HTML for detection rather than parsing it in JavaScript. One Python parser covers both paths and sees pages built by JavaScript.
- Faircamp's markup (`<audio><source type="audio/mpeg">`) is as seen on 2026-11-02. A saved fixture guards the parser.
- **Review findings not acted on**:
  - IP changes still break queued URLs (only a DHCP reservation for the Mac avoids it). Re-queuing automatically on an IP change is deferred, since it's rare on a home network.
  - Lid-closed sleep can't be prevented by an assertion; this is documented, not engineered around.
