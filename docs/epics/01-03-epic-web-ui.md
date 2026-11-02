# Web UI: browse, play, now playing, queue

**Source spec**: docs/specifications/01-spec-sonos-home-player.md  
**Date**: 2026-11-02  
**Status**: Pending  
**Blocked by**: Epic 01-02-epic-service-core

## Browse, play and control a room
**Story**: 1  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR9, NFR3

**Acceptance Criteria**:

- Against `sonos service run --fake-rooms`, in Chrome: choosing Kitchen, browsing into a folder, clicking Play on a file and setting volume to 25 each change the fake Kitchen, and the page shows the new track, state and volume within 3 s. `[browser]`
- The filter box narrows the current folder's listing to names containing the typed text, case-insensitively, and folders are listed before files. `[browser]`
- Play folder and Append folder on a folder send `mode=replace` and `mode=append` respectively, as seen in the fake queue. `[browser]`
- The now-playing area shows "this Mac" for our URL, "from <host>" for another host's URL and "other source" otherwise, for the matching fake states. `[browser]`
- After the service is stopped, the page shows "service unreachable" within 5 s instead of stale state. `[browser]`
- With no music folders configured, the browse view shows the config file's path and an example `music_folders` entry, and no Play buttons. `[browser]`
- The page is usable at 375 px wide with no horizontal scrolling. `[browser]`
- Idle with one UI tab polling for 5 minutes, the service stays under 150 MB RSS and averages under 1% CPU. `[manual]`

### Static UI
**Task**: 1.1  
**Description**: `src/sonos_cli/web/` (index.html, app.js, style.css) served by the service; JSON calls with `Content-Type: application/json`.  
**Status**: Pending

---

## Queue view
**Story**: 2  
**Status**: Pending  
**Blocked by**: Story 1  
**Satisfies**: FR19

**Acceptance Criteria**:

- `GET /api/rooms/{uid}/queue` returns the fake queue with the current index. Each item served by another host carries that host as a label. `POST …/queue/jump {index: 2}` calls `play_from_queue(2)`, and `POST …/queue/clear` empties it. `[unit]`
- In Chrome against fake rooms, the queue panel highlights the current track. Clicking the third item makes it current, and Clear empties the panel. `[browser]`

### Queue endpoints and panel
**Task**: 2.1  
**Description**: Queue routes reading from the coordinator via the monitor's thread pool; panel in the UI.  
**Status**: Pending

---
