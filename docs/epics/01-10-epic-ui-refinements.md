# UI refinements: faders, seek, folders, queue feedback

**Source spec**: docs/specifications/01-spec-sonos-home-player.md  
**Date**: 2026-11-03  
**Status**: Pending  
**Blocked by**: Epic 01-03-epic-web-ui, Epic 01-04-epic-room-management

Six observations from the user's first use of the installed service (2026-11-03). Browser checks run against `sonos service run --fake-rooms --port 8761` in the user's Chrome through claude-in-chrome; tests run with `.venv/bin/python -m pytest` (not `uv run`, which re-locks). Every change keeps the Console look (docs/quick/01-quick-console-look.md): one set of tokens, visible focus, 375 px without horizontal scroll.

## A progress bar that moves smoothly
**Story**: 1  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR9

**Acceptance Criteria**:

- While the selected fake room is PLAYING, the footer progress bar's width grows between state polls: sampled every 250 ms for 4 s in the page, the width increases at least 12 times and never decreases except when a poll reports an earlier position. With the extrapolation removed (control), the same sampling shows the width changing at most twice. `[browser]`
- When the room is paused or stopped, the bar stops: sampled every 250 ms for 3 s after a pause, the width doesn't change; it never passes 100 % (a fake track 2 s from its end, sampled for 5 s, stays at or below 100 %); and on each poll it snaps to the reported position (after the fake room's position is moved back by 30 s, the bar shows the new position within one poll). `[browser]`
- The bar keeps `role="progressbar"` semantics until Story 2 makes it a slider, its `aria-valuenow` follows the shown position, and under `prefers-reduced-motion` it still updates (without a CSS transition). `[browser]`

### Extrapolate the position between polls
**Task**: 1.1  
**Description**: Keep the last reported position, duration, state and the time it arrived in `app.js`; on animation frames while PLAYING, draw `min(duration, position + elapsed)`; snap on each poll; stop when not playing; drop the `.9s` width transition that fights the frames.  
**Status**: Pending

---

## Seek by clicking the progress bar
**Story**: 2  
**Status**: Pending  
**Blocked by**: Story 1  
**Satisfies**: FR24

**Acceptance Criteria**:

- `POST /api/rooms/{uid}/seek {"position": <seconds>}` on a fake room playing a file with a duration calls the coordinator's `seek("H:MM:SS")` with that position (whole seconds, `0:01:05` for 65), for a member room too (it acts on the coordinator), and the state then reports that position. `[integration]`
- A seek is refused with a 4xx error naming the reason, and the fake room's `seek` is never called, for: a non-numeric, negative, NaN or missing `position`; a position past the duration; a room whose current track has no duration (a radio stream or this service's `/live/` stream); an unknown room (404). Control: a valid seek in the same fixture is accepted and calls `seek`. `[integration]`
- In the browser against fake rooms, clicking at about 50 % of the bar seeks the playing room to about half its duration (the fake room's position, read from `/api/rooms/{uid}/state`, is within 2 s of half), dragging along the bar seeks once on release, and with the bar focused the Right and Left arrow keys seek 10 s forward and back. The bar is `role="slider"` with `aria-valuemin`, `aria-valuemax` (seconds) and `aria-valuetext` like `1:05 of 4:00`. `[browser]`
- When the current track can't be seeked (no duration), the bar is not interactive (`aria-disabled="true"`, no seek request on click). `[browser]`

### Seek endpoint
**Task**: 2.1  
**Description**: `seek_route` in `service.py` with input checks, `speaker_call` on the coordinator, `monitor.refresh_group_of` after; the fake's `seek` (`src/sonos_cli/fakes.py`) moves the fake position; tests in a new `tests/test_seek_api.py`.  
**Status**: Pending

### Seek from the bar
**Task**: 2.2  
**Description**: Pointer and keyboard handling on `#progress` in `app.js` and its markup in `index.html`.  
**Status**: Pending

---

## Drag each room's volume on its strip
**Story**: 3  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR9, FR11

**Acceptance Criteria**:

- Every room strip has a volume fader that can be dragged with the pointer: dragging Bedroom's fader from its level to about 30 sets Bedroom's group volume to within 3 of 30 (read from `/api/rooms`), without changing the selected room. Floor and control: Lounge, Kitchen and Office keep their volumes, and with two rooms selected in turn, each strip's fader changes only its own room. `[browser]`
- The fader is reachable by keyboard (Tab to it; Up/Down change the volume by 1, Page Up/Down by 10), has an accessible name `<room> volume` and its value, and shows a visible focus outline. Clicking the rest of the strip still selects the room. `[browser]`
- While a fader is being dragged, state polls don't move it back: during a 3 s drag sampled every 250 ms, the fader's value only follows the pointer. Requests during a drag are sent at most every 250 ms and the final value is sent on release (counted in the service log or with a request counter in the page). `[browser]`
- At 375 px wide the faders are usable (each at least 44 px along its travel) and the strip row still scrolls sideways without horizontal page scroll. `[browser]`

### Strip fader
**Task**: 3.1  
**Description**: Replace the decorative `chan` (meter, slot, knob) in `renderRooms` with an `input type="range"` (vertical, Console-styled) outside the strip's select button, posting to the existing `/api/rooms/{uid}/volume`; keep the meter as the live level.  
**Status**: Pending

---

## Style the "Group with…" select like the keys beside it
**Story**: 4  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR11  
**Effort**: low

**Acceptance Criteria**:

- In the Rooms view, each room's "Group with…" select has the same computed height, border radius, background, border and font as the key buttons beside it in that row (compared from `getComputedStyle` in the page), and a chevron drawn in the Console's tokens instead of the native one (`appearance: none`). `[browser]`
- It keeps a visible focus outline (computed `outline` like the keys'), its text and chevron meet 4.5:1 against its background, and at 375 px the row has no horizontal page scroll. `[browser]`

---

## Show that a queue jump was taken
**Story**: 5  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR19

**Acceptance Criteria**:

- Double-clicking a queue row (or Enter on its focused button) marks that row pending within 100 ms (a visible pending style and `aria-busy="true"`, before the request returns), and when the jump succeeds the row is marked current and the Now playing line shows its title without waiting for the next 2 s poll (the page refreshes state and queue as soon as the jump returns). `[browser]`
- A jump that fails (the queue was cleared in another tab first, so the index no longer exists) clears the pending mark, leaves the previous current row highlighted, and shows the service's error in the queue note. Control: the same double-click on an existing row succeeds and shows no error. `[browser]`
- A second double-click while a jump is pending doesn't send a second request. `[browser]`

---

## Add and remove music folders from the Music view
**Story**: 6  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR23, FR6, NFR6  
**Effort**: high

**Acceptance Criteria**:

- `GET /api/folders` lists the configured music folders (real paths) to any allowed client, with `"editable": true` only for a client on this Mac (a loopback address or one of the Mac's own interface addresses, judged from the TCP peer address, never a header). `[integration]`
- From a client on this Mac, `POST /api/folders/add {"path": …}` with an existing, readable directory adds its real path to `music_folders` in `config.toml`; `/api/browse` lists it at once, and its files play, without a service restart. A `~` path, a symlink to a folder (stored as its target), and a path with spaces and non-ASCII characters are each accepted. `[integration]`
- The config file is changed only in its `music_folders` value: a file with comments, `port`, `room`, `extension_ids` and keys in a custom order keeps every other line byte for byte; a file with no `music_folders` gets the key added; the write is atomic (a temp file renamed into place). A config file that doesn't parse is not overwritten: the request fails naming the file and line, and the file is unchanged. `[integration]`
- An add is refused with a 4xx error naming the reason, and the config file is unchanged, for: a path that doesn't exist, a file, an unreadable directory, a relative path, an empty or non-string path, and a folder already configured (same real path). Control: an existing readable directory in the same fixture is accepted. `[integration]`
- `POST /api/folders/remove {"path": …}` from this Mac removes that folder from `music_folders` (other keys untouched), `/api/browse` stops listing it and its media URLs return 404 at once; removing a folder that isn't configured is a 404; no file or folder on disk is changed (a snapshot of the folder's tree before and after is equal). `[integration]`
- `GET /api/folders/browse?path=…` from this Mac lists only the names of subfolders (no files, no hidden entries) of a folder under the home folder or `/Volumes`; a path outside both, a `..` path, a file and a missing path are refused. `[integration]`
From a client that is not this Mac (the test client's address set to a LAN address that isn't one of the Mac's, with the local-address lookup injected), `add`, `remove` and `browse` are each refused with 403 and the config file is unchanged; the same three requests succeed in the same fixture from 127.0.0.1, from ::1 and from an address the injected lookup lists as the Mac's own (control and floor). An `X-Forwarded-For` or `Host` header naming 127.0.0.1 doesn't make a foreign client count as this Mac. The NFR6 checks still apply to the new routes: a forged `Origin` or a `text/plain` POST from this Mac is refused. `[integration]`
- In the browser on `http://localhost:8761/`, the Music view lists the folders with Add folder and Remove; Add folder opens a picker that browses folder names from the home folder and `/Volumes` and accepts a pasted path; adding a folder shows it in the listing straight away, and removing asks for confirmation in the page (not a browser `confirm()`), then drops it. With no folders configured, the empty view offers Add folder instead of only the config example. `[browser]`
- In the browser on the Mac's LAN address (`http://<LAN IP>:8761/`), Add folder and Remove are offered as on `localhost`. When `/api/folders` reports `"editable": false` (checked by pointing the page at a stub that says so, or with the editable flag forced off in the page), the list shows with no Add or Remove and a note that folders are changed on the Mac itself. `[browser]`

### Config writer and live library
**Task**: 6.1  
**Description**: In `config.py`, a writer that replaces or appends only the `music_folders` value (re-read and validated with `tomllib` before the temp file is renamed); a way for `service.py` to swap the `Library` the routes use, under a lock, so browse, play and media serving see the new roots at once.  
**Status**: Pending

### Folder routes
**Task**: 6.2  
**Description**: `/api/folders`, `/api/folders/add`, `/api/folders/remove`, `/api/folders/browse` with the this-Mac rule (loopback or `local_addresses()`, injectable for tests), in `service.py`; tests in a new `tests/test_folders_api.py`.  
**Status**: Pending

### Music view
**Task**: 6.3  
**Description**: Folder list, picker dialog and remove confirmation in `app.js`/`index.html`/`style.css`, in the Console look.  
**Status**: Pending

---

## Check the refinements on the real speakers
**Story**: 7  
**Status**: Pending  
**Blocked by**: Story 2, Story 3, Story 6  
**Satisfies**: FR9, FR23, FR24

**Acceptance Criteria**:

- With the service reinstalled from this repo (`uv tool install . --reinstall`, `sonos service install`), play a local file on Office from the UI on the Mac, click about halfway along the progress bar, and listen: playback jumps to about the middle of the track, and the bar and time show the new position within 2 s. Then send `https://shtl-experiment.com/foreshore/` to Office and do the same on its first track. Report both. `[user]`
- Drag Bedroom's fader on its strip from the Mac and then from the phone (`http://<Mac IP>:8737/`): the speaker's volume follows and the Sonos app shows the same level. Report the levels. `[user]`
- On the Mac, add a real folder from the Music view (one not yet configured), play a file from it, then remove it; open the Music view on the Mac at its LAN address and confirm Add folder is offered there too; on the phone, confirm the folder list shows without Add or Remove. Report what each showed, and that `~/Library/Application Support/sonos/config.toml` kept your other settings. `[user]`
