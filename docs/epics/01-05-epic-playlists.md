# Playlists

**Source spec**: docs/specifications/01-spec-sonos-home-player.md  
**Date**: 2026-11-02  
**Status**: Pending  
**Blocked by**: Epic 01-02-epic-service-core

## Playlist storage and API
**Story**: 1  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR12

**Acceptance Criteria**:

- Creating, renaming, deleting, adding, removing and reordering tracks via `/api/playlists` writes UTF-8 `.m3u8` files with `#EXTM3U` and absolute paths. A playlist with a track named `Café – Ünïcode 日本.wav` reads back identically after the app restarts. `[unit]`
- A playlist name such as `../../x` or `a/b` is stored under a safe filename inside the playlists directory. No file is created outside it, which the test checks by listing the parent directory. `[unit]`
- Playing a playlist containing a deleted file and an entry `/etc/hosts` queues only the remaining tracks, and reports both as unavailable. With the guard removed, `/etc/hosts` is queued (control). `[unit]`

### Store and routes
**Task**: 1.1  
**Description**: `src/sonos_cli/playlists.py` and routes; reading tolerates plain `.m3u` written by other apps.  
**Status**: Pending

---

## Playlists in the UI
**Story**: 2  
**Status**: Pending  
**Blocked by**: Story 1, Epic 01-03-epic-web-ui  
**Satisfies**: FR12

**Acceptance Criteria**:

- In Chrome against fake rooms: create a playlist, add two files from the browser, reorder them, play it on Kitchen (the fake queue matches the order), rename it, then delete it. Each step shows in the playlist panel. `[browser]`

### Playlist panel
**Task**: 2.1  
**Description**: Playlist list and editor in the UI; "Add to playlist" on browse rows.  
**Status**: Pending

---
