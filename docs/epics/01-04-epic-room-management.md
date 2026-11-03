# Room management: rename, group, ungroup

**Source spec**: docs/specifications/01-spec-sonos-home-player.md  
**Date**: 2026-11-02  
**Status**: Complete  
**Blocked by**: Epic 01-02-epic-service-core

## Room management API and CLI
**Story**: 1  
**Status**: Complete  
**Blocked by**: —  
**Satisfies**: FR11  
**Audit**: pass (2026-11-03), 6 findings fixed (the last round's fixes are covered by tests and were not audited a third time)

**Acceptance Criteria**:

- `POST /api/rooms/{uid}/rename`, `/group {into: uid}`, `/ungroup` and `POST /api/rooms/group-all` call, on the right fake device, `player_name = …`, `join(coordinator)`, `unjoin()` and `partymode()`. `partymode` leaves Office's invisible satellite untouched. `[unit]`
  - Evidence: tests/test_rooms_api.py::test_rename_group_ungroup_and_group_all_call_the_right_devices (player_name set on Office only, never on its satellite; join on the room's own device with the target's coordinator; unjoin; partymode on the chosen room, and the satellite goes with its partner without ever being sent a join), ::test_bad_room_management_requests, ::test_unknown_rooms_and_group_all_validation, ::test_a_rename_to_another_rooms_name_is_refused_in_any_case_and_a_name_is_trimmed, ::test_names_with_invisible_characters_or_too_long_are_refused, ::test_a_rename_the_speakers_topology_has_not_caught_up_with_is_not_undone_by_a_discovery, ::test_joining_a_group_you_are_already_in_does_not_bother_the_speaker, ::test_a_partly_done_party_still_asks_for_a_rediscovery, ::test_already_together_is_decided_by_the_speaker_not_by_the_cache, ::test_names_may_use_zero_width_joiners_but_not_other_invisible_characters, ::test_a_speaker_that_refuses_is_a_json_502 (full suite 309 passed) — red: "assert (('join', 'RINCON_000E…') in [])" with the already-together check reading the cache (service.py `together = …`); "assert (200 == 409)" with the duplicate-name check at service.py `if monitor.name_taken(name, uid):` replaced by `if False:`; "assert 'Kitchen' == 'Den'" with rooms.py's pending-rename branch (`else: name = pending[0]`) disabled; "assert ([('join', 'RINCON_000E…')] == []" with the already-together check at service.py `if leader.uid == dev.uid or …` disabled; "assert False is True" with the `finally: monitor.request_discovery()` in the group-all route removed; "assert (0 == 1)" with rooms.py `name = name.strip()` removed (the direct CLI accepts '  '); and the name byte limit removed; "assert ('player_name', 'Studio') in []" with the rename not sent; "assert {'Bedroom',…,'Office'} == {…'Studio'}" with the rename not shown at once; "assert (200 == 200 and ('join', 'RINCON_000E…') in [])" with join sent to the wrong device; "assert 'RINCON_000E…' == 'RINCON_347E…'" with the cache not following an ungroup; "assert (200 == 200 and ('partymode',) in [])" with party mode done by joining each room; 'AssertionError: {"ok":true,…"into":…}' with a room allowed to join itself; 'AssertionError: {"uid":…,"name":""}' with an empty name accepted (service.py rename/group/ungroup/group-all routes)
- `sonos rename Office Studio`, `sonos group Kitchen Lounge` (Kitchen joins Lounge), `sonos ungroup Kitchen` and `sonos group --all` resolve names to UIDs and call those endpoints. An unknown name lists the available rooms. `[unit]`
  - Evidence: tests/test_rooms_api.py::test_cli_rename_group_ungroup_and_group_all_resolve_names_and_call_the_endpoints (the recorder shows POST rename {name}, group {into: <uid>}, ungroup, group-all {into: <uid>} with names resolved case-insensitively), ::test_an_unknown_room_lists_the_available_rooms (rename, group both positions, ungroup: exit 1, the unknown name and Kitchen/Lounge/Office listed, nothing posted), ::test_group_needs_two_rooms_or_all, ::test_the_cli_commands_work_without_the_service_too, ::test_the_direct_cli_applies_the_same_checks (direct SoCo when the connection is refused, with the API's name and same-group checks) — red: 'assert (1 == 0)' with rename resolving the new name instead of the old; "assert (0 == 0 and <FakeSoCo Kitchen> is <FakeSoCo Lounge>)" with group's two rooms swapped; "assert ('Garage' in \"sonos: No Sonos room called 'Garage' (rooms: none found)\\n\" and 'Kitchen' in …)" with the room list dropped; "assert (0 == 1)"-style failures (`run_cli(*argv) == 1`) with the CLI two-room check in cmd_group removed (cli.py `elif not (args.member and args.into):`)
- After renaming Office to Studio in the fake, a play request using Office's UID still queues on the same device. `[unit]`
  - Evidence: tests/test_rooms_api.py::test_after_a_rename_the_same_uid_still_plays_on_the_same_device (the fake's player_name is 'Studio', a monitor refresh shows the new name under the same UID, and /api/play with the old UID queues on the same device) — red: "assert ('player_name', 'Studio') in []" and "assert {'Bedroom',…} == {…'Studio'}" with the rename not applied or not shown (service.py rename route)
- Play, transport and volume requests for a room that's a member of another group act on that group's coordinator. `[unit]`
  - Evidence: tests/test_rooms_api.py::test_transport_queue_and_volume_for_a_member_act_on_the_coordinator (Kitchen joined to Lounge through the API: play queues on Lounge, pause pauses Lounge, volume sets the group; nothing is sent to Kitchen itself), and tests/test_play_api.py::test_transport_actions_act_on_the_groups_coordinator, ::test_play_via_a_group_member_queues_on_the_coordinator — red: 'assert 502 == 200' (a SoCoSlaveException from pausing the member) with coordinator_now returning the room's own device instead of its group's coordinator (service.py coordinator_now)

### Endpoints and subcommands
**Task**: 1.1  
**Description**: Routes in `service.py`, subcommands in `cli.py`, name→UID resolution in the client.  
**Status**: Complete

---

## Room management in the UI
**Story**: 2  
**Status**: Complete  
**Blocked by**: Story 1, Epic 01-03-epic-web-ui  
**Satisfies**: FR11  
**Audit**: pass after fixes (2026-11-03), 2 rounds: round 1 (2 Warnings: Leave group on a coordinator left stale rooms, the empty-state note never cleared) and round 2 (2 Warnings in the fixes: a focused button froze panel refreshes, the queue-title fallback named audio that wasn't from the queue) fixed and re-checked in Chrome; no Critical, so the second audit was the last.  
**Signal**: fix-introduced-regression — project — the round 1 focus fix (restore focus to a button inside the panel) made the panel's "don't refresh while focused" guard permanent, which round 2 found (Story 2; scope a re-render guard to the elements that lose typed input, not to anything focused)  

**Acceptance Criteria**:

- In Chrome against fake rooms: renaming Bedroom to Den, grouping Kitchen into Lounge, ungrouping Kitchen and Group all each change the fake system. The room list reflects each change within 3 s. `[browser]`
  - Evidence: browser-observed at http://localhost:8761/ in Chrome (`sonos service run --fake-rooms --port 8761`), Rooms view: Rename on Bedroom opened a focused name field, `Den` + Enter showed "Renamed Bedroom to Den." and the panel listed Den after 0.1 s, `/api/rooms` agreeing; choosing Lounge in Kitchen's "Group with…" and pressing Group showed "Kitchen joined Lounge.", the panel listed one group "Lounge + Kitchen" ("2 rooms grouped") after 0.1 s and `/api/rooms` reported `Lounge+Kitchen`; Leave group on Kitchen showed "Kitchen left the group." and four separate rooms after 0.1 s; "Group all rooms with Kitchen" showed "Every room is now grouped with Kitchen." and one group "Office + Lounge + Kitchen + Den" after 0.16 s with four member rows, `/api/rooms` agreeing; a blank new name gave "Give the room a name first." and Escape closed the field; no console errors. — red: n/a — browser-observed After audit round 1 (re-run in Chrome): after "Group all rooms", Leave group on the group's coordinator (Office) showed "Office left the group." and the panel at once as "Lounge + Kitchen + Den" and "Office", with no waiting for the next poll; a half-typed name (`Studio B`) survived a re-render caused by clicking a room in the sidebar; renaming Den to `Kitchen` showed "Another room is already called 'Kitchen'" and kept the typed text; Escape returned focus to that room's Rename button; no console errors. The backend part of the fix has a test: tests/test_rooms_api.py::test_when_a_groups_coordinator_leaves_the_rooms_it_led_are_reported_with_their_new_group_at_once (full suite 516 passed) — red: "assert 'RINCON_000E58AABB0101400' != 'RINCON_000E58AABB0101400'" with the ungroup route polling only the leaver's and the old coordinator's group (service.py). After audit round 2 (Chrome): with focus left on a room's Rename button, a group made through the API showed in the panel on the next `pollRooms` ("Lounge + Kitchen"), so a focused button no longer freezes the panel (only a focused text field or select does); the player card showed "Undertow" for a queue item the service marks current and "Playing" for a line-in source whose queue has `current: null`; no console errors.

### Rooms panel
**Task**: 2.1  
**Description**: Rooms panel with rename and group controls.  
**Status**: Complete

---
