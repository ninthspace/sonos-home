# Web UI: browse, play, now playing, queue

**Source spec**: docs/specifications/01-spec-sonos-home-player.md  
**Date**: 2026-11-02  
**Status**: Complete  
**Blocked by**: Epic 01-02-epic-service-core

## Browse, play and control a room
**Story**: 1  
**Status**: Complete  
**Blocked by**: —  
**Satisfies**: FR9, NFR3  
**Audit**: pass (2026-11-03), 8 findings fixed (the last round's fix was checked in the browser and not audited a third time)

**Acceptance Criteria**:

- Against `sonos service run --fake-rooms`, in Chrome: choosing Kitchen, browsing into a folder, clicking Play on a file and setting volume to 25 each change the fake Kitchen, and the page shows the new track, state and volume within 3 s. `[browser]`
  - Evidence: browser-observed at http://127.0.0.1:8761/ (`sonos service run --fake-rooms -p 8761` with a one-folder config; first Chrome, before it disconnected): clicking Kitchen, then music → GLDR, then Play on `01 Halyard.wav` made the page show "01 Halyard" with "this Mac, playing" and the note "Playing 1 track on Kitchen."; moving the volume to 25 sent POST …/volume {level: 25}, and 2.8 s later the page showed 25, with the service's state for Kitchen `PLAYING`, volume 25, title "01 Halyard", source `ours`. — red: n/a — browser-observed After the audit fixes the flow was re-run on Slate against a fresh service: Kitchen → music → GLDR → Masters (230 files: 200 shown, Show more offered); typing `f229` found `f229.wav` on the second page; Play on `01 Halyard.wav` clicked twice showed "01 Halyard", "this Mac, playing", the playing row highlighted and a screen-reader line "Now playing 01 Halyard"; volume 25 reached the service (Kitchen `PLAYING`, volume 25). A last run after the final fix: typing `hal` straight after clicking Up left the page in GLDR showing `01 Halyard.wav`, not back in Masters. No script errors. — red: n/a — browser-observed
- The filter box narrows the current folder's listing to names containing the typed text, case-insensitively, and folders are listed before files. `[browser]`
  - Evidence: browser-observed, same session: GLDR listed `Masters` (a folder) before `01 Halyard.wav`, `02 Undertow.wav`, `03 Slack Water.wav`; typing `UNDER` left only `02 Undertow.wav`. — red: n/a — browser-observed
- Play folder and Append folder on a folder send `mode=replace` and `mode=append` respectively, as seen in the fake queue. `[browser]`
  - Evidence: browser-observed on the machine's own Chrome ("Slate") at http://127.0.0.1:8761/: Lounge selected, inside Hello EP, Play folder showed "Playing 2 tracks on Lounge." and Add folder to queue showed "Added 2 tracks on Lounge."; the page's POSTs to /api/play carried `mode` `replace` then `append`. — red: n/a — browser-observed
- The now-playing area shows "this Mac" for our URL, "from <host>" for another host's URL and "other source" otherwise, for the matching fake states. `[browser]`
  - Evidence: browser-observed, same page with the room-state response stubbed in the page for the two cases the stock fake can't produce: source `ours` → "this Mac, playing, 0:05 / 3:20"; `other-host:10.0.0.9` → "from 10.0.0.9, playing, 0:05 / 3:20"; `other` → "other source, playing, 0:05 / 3:20". (The real fake room had already shown "this Mac" after a real play.) — red: n/a — browser-observed
- After the service is stopped, the page shows "service unreachable" within 5 s instead of stale state. `[browser]`
  - Evidence: browser-observed: with the page open the service was stopped; the banner "The service is unreachable. Check that sonos service run is …" was visible within about 0.6 s and the player read "Service unreachable" / "Waiting for the service to answer." — red: n/a — browser-observed
- With no music folders configured, the browse view shows the config file's path and an example `music_folders` entry, and no Play buttons. `[browser]`
  - Evidence: browser-observed at http://127.0.0.1:8762/ (a service whose config has no `music_folders`): heading "No music folders yet", the config path `…/cfg-empty/config.toml`, the example `music_folders = ["~/Dropbox/Music"]`, no Play or Add button visible and the folder actions hidden. — red: n/a — browser-observed
- The page is usable at 375 px wide with no horizontal scrolling. `[browser]`
  - Evidence: browser-observed: the browser window couldn't be resized, so the page was loaded in a 375-px-wide iframe (its own viewport, so the media queries apply): document scroll width 371 = viewport width, no element past the right edge, the rooms became a scrolling chip row, file sizes hidden, the player bar (transport and volume) visible at the bottom, and a screenshot looked right. — red: n/a — browser-observed
- Idle with one UI tab polling for 5 minutes, the service stays under 150 MB RSS and averages under 1% CPU. `[manual]`
  - Evidence: measured on the python process of `sonos service run --fake-rooms -p 8761` with one tab open at http://127.0.0.1:8761/ (polling every 2 s and 5 s): RSS 54,000 kB at the start and 54,048 kB after 5 minutes (limit 153,600 kB); CPU time 0.27 s → 1.23 s, so 0.96 s over 300 s = 0.32% (limit 1%). — red: n/a — manual

### Static UI
**Task**: 1.1  
**Description**: `src/sonos_cli/web/` (index.html, app.js, style.css) served by the service; JSON calls with `Content-Type: application/json`. Design it with the `frontend-design:frontend-design` skill (the user's instruction, 2026-11-02): load the skill before writing any markup or CSS, and apply the same design to the rooms (01-04) and playlists (01-05) panels.  
**Status**: Complete

---

## Queue view
**Story**: 2  
**Status**: Complete  
**Blocked by**: Story 1  
**Satisfies**: FR19  
**Audit**: pass (2026-11-03), 6 findings fixed (the last round's fixes are covered by tests and were not audited a third time)

**Acceptance Criteria**:

- `GET /api/rooms/{uid}/queue` returns the fake queue with the current index. Each item served by another host carries that host as a label. `POST …/queue/jump {index: 2}` calls `play_from_queue(2)`, and `POST …/queue/clear` empties it. `[unit]`
  - Evidence: tests/test_queue_api.py::test_the_queue_comes_back_with_the_current_index, ::test_items_served_by_another_host_carry_that_host_as_a_label (mine → none, http://10.0.0.9… → "10.0.0.9", a Spotify item → none), ::test_jump_plays_that_item_and_clear_empties_the_queue (jump {index: 2} calls play_from_queue(2); clear empties it), ::test_a_member_acts_on_the_coordinators_queue, ::test_bad_jumps, ::test_unknown_room_and_speaker_errors, ::test_a_queue_longer_than_one_browse_page_is_read_in_full_and_the_current_item_is_found (a fake that pages like SoCo: 100 per Browse), ::test_only_the_play_cap_is_read_from_an_enormous_queue, ::test_no_item_is_current_when_the_room_plays_something_that_is_not_the_queue_item, ::test_an_unreachable_speaker_gets_a_quick_json_502_not_a_blocked_read, ::test_the_playing_uri_matches_the_queue_item_whatever_the_percent_encoding, ::test_a_queue_read_never_asks_for_more_than_the_play_cap_even_from_a_short_paging_speaker (full suite 374 passed) — red: 'assert _same_uri("…01%20Intro.wav", "…01 Intro.wav")' failing with the comparison reduced to plain equality; 'assert … <= 500' failing with the page size not limited to the cap; 'assert (100 == 230)' with the read stopping after one Browse page; 'assert 650 == 500' with the play cap removed; 'assert 0 is None' with the current item not checked against the playing URI; 'assert (200 == 502)' with the unreachable-speaker check removed; "assert (['t0','t1','t2'] == … current None" with the current index dropped; "assert [('mine', None), ('theirs', None), …] == […('theirs', '10.0.0.9')…]" with the host label removed; "assert (200 == 200 and ('play_from_queue', 2) in [('play_from_queue', 0)])" with jump always playing item 0; "assert ({'items': […], 'current': 2} == {'items': [], 'current': None}" with clear doing nothing; 'assert 0 == 3' with the queue read from the room's own device instead of the coordinator's; 'assert (502 == 422)' with the range check removed and 'assert (502 == 400)' with the index type check loosened (service.py queue routes)
- In Chrome against fake rooms, the queue panel highlights the current track. Clicking the third item makes it current, and Clear empties the panel. `[browser]`
  - Evidence: browser-observed on Slate at http://127.0.0.1:8761/ (fake rooms, GLDR folder): with Lounge selected the panel said "The queue is empty. Play something to fill it."; after Play folder it listed 4 items (01 Halyard, 02 Undertow, 03 Slack Water, 01 Halyard (master)) with the first marked current; clicking the third made it current (the player read "03 Slack Water"); Clear emptied the panel, hid the Clear button and showed the empty message. No script errors. After the audit fixes: Play folder on a 132-file folder showed all 132 items; clicking item 121 made it current (player "t119", item marked `aria-current` on its button), and screenshots were sent to the user. — red: n/a — browser-observed

### Queue endpoints and panel
**Task**: 2.1  
**Description**: Queue routes reading from the coordinator via the monitor's thread pool; panel in the UI.  
**Status**: Complete

---
