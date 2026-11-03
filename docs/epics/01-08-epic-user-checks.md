# User checks: hearing it work in the house

**Source spec**: docs/specifications/01-spec-sonos-home-player.md  
**Date**: 2026-11-02  
**Status**: Pending  
**Blocked by**: Epic 01-02-epic-service-core, Epic 01-03-epic-web-ui, Epic 01-04-epic-room-management, Epic 01-05-epic-playlists, Epic 01-06-epic-send-page, Epic 01-07-epic-live-streaming

## Local masters at the desk
**Story**: 1  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR5, FR9

**Acceptance Criteria**:

- Add the folder holding your masters to `music_folders` in `~/Library/Application Support/sonos/config.toml` and restart the service. Then in the UI on the Mac, browse to a folder of your 24-bit masters (e.g. the GLDR folder) and click Play folder on Office. Track 1 plays audibly within a few seconds at the right pitch and speed, with no clicks, and Next moves to track 2. `[user]`

---

## Control from the phone
**Story**: 2  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR9, FR11

**Acceptance Criteria**:

- On your phone on home Wi-Fi, open `http://<mac-ip>:8737/`, choose Lounge, set volume to 25 and group Kitchen into Lounge. The Sonos app shows Lounge + Kitchen grouped at the same volume, and the page has no sideways scrolling. `[user]`

---

## Your Faircamp release on a speaker
**Story**: 3  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR13, FR14

**Acceptance Criteria**:

- In your own Chrome with the extension loaded, open a release on shtl-experiment.com and click Send page with Office selected. Track 1 plays audibly in the Office, and the Sonos app shows the right title. `[user]`

---

## YouTube to the Office, and its delay
**Story**: 4  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR15, NFR2

**Acceptance Criteria**:

- On a YouTube music video, click Stream tab with Office selected. It plays audibly in the Office. It stops within 5 s of clicking Stop, and within 5 s of closing the tab on a second run. `[user]`
- Stream a click-track video to Office and record the Mac and the speaker together on your phone. The gap between the clicks is 6 s or less, and you report the figure. `[user]`

---

## Survives a restart
**Story**: 5  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR2

**Acceptance Criteria**:

- After `sonos service install`, restart the Mac and log in. Without opening a terminal, open `http://localhost:8737/`: every room that is online is listed (the Office pair counts once), and playing a file on Office is audible. `[user]`

---

## A room that is unplugged comes back
**Story**: 6  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR4, NFR5

**Acceptance Criteria**:

- With `uv run sonos service run` going in a terminal and `curl -s http://127.0.0.1:8737/api/rooms` showing Office, unplug Office (the Play:1 pair). Within 60 s `/api/rooms` shows Office with `"online": false`. Plug it back in. Within 60 s of it joining the network, `/api/rooms` shows Office with `"online": true` again, with no restart. `[user]`

---

## The service installs, restarts and uninstalls
**Story**: 7  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR2, NFR3, AD6

**Acceptance Criteria**:

- From the installed tool (`uv tool install` of this repo, so it isn't an editable checkout), with the Mac unlocked, run `sonos service install`. The first run may show a macOS Local Network prompt for SonosService: allow it, and note which prompts appeared and for which app. Then `curl -s http://127.0.0.1:8737/api/health` shows `"ffmpeg":"ok"`, and `curl -s http://127.0.0.1:8737/api/rooms` lists every room that is online (the Office pair counts once). Run `sonos service install` a second time with it loaded: it should replace the agent and the same two `curl`s should still work. `[user]`
- Find the launcher with `pgrep -f MacOS/SonosService`, then the service Python it started with `pgrep -P <launcher pid>`. `kill -9` the Python one. The launcher exits and launchd starts a fresh one: `pgrep -f MacOS/SonosService` now prints a different pid, `pgrep -P <that new pid>` shows the new Python, and `curl -s http://127.0.0.1:8737/api/health` answers 200 again within 10 s. `[user]`
- `sonos service status` prints "loaded", "healthy" and the log path. After `sonos service uninstall`, `~/Library/LaunchAgents/local.sonos-cli.plist` is gone and `curl -s http://127.0.0.1:8737/api/health` is refused. `[user]`
- With the service installed and no UI open, `ps -o rss=,pcpu= -p PID` (PID is the Python process from `pgrep -P`, not the launcher's) sampled once a minute for 5 minutes stays under 150 MB (153600 kB) and averages under 1 % CPU. With one UI tab polling (once the UI exists, epic 01-03), the same holds. `[user]`

