# Room management: rename, group, ungroup

**Source spec**: docs/specifications/01-spec-sonos-home-player.md  
**Date**: 2026-11-02  
**Status**: Pending  
**Blocked by**: Epic 01-02-epic-service-core

## Room management API and CLI
**Story**: 1  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR11

**Acceptance Criteria**:

- `POST /api/rooms/{uid}/rename`, `/group {into: uid}`, `/ungroup` and `POST /api/rooms/group-all` call, on the right fake device, `player_name = …`, `join(coordinator)`, `unjoin()` and `partymode()`. `partymode` leaves Office's invisible satellite untouched. `[unit]`
- `sonos rename Office Studio`, `sonos group Kitchen Lounge` (Kitchen joins Lounge), `sonos ungroup Kitchen` and `sonos group --all` resolve names to UIDs and call those endpoints. An unknown name lists the available rooms. `[unit]`
- After renaming Office to Studio in the fake, a play request using Office's UID still queues on the same device. `[unit]`
- Play, transport and volume requests for a room that's a member of another group act on that group's coordinator. `[unit]`

### Endpoints and subcommands
**Task**: 1.1  
**Description**: Routes in `service.py`, subcommands in `cli.py`, name→UID resolution in the client.  
**Status**: Pending

---

## Room management in the UI
**Story**: 2  
**Status**: Pending  
**Blocked by**: Story 1, Epic 01-03-epic-web-ui  
**Satisfies**: FR11

**Acceptance Criteria**:

- In Chrome against fake rooms: renaming Bedroom to Den, grouping Kitchen into Lounge, ungrouping Kitchen and Group all each change the fake system. The room list reflects each change within 3 s. `[browser]`

### Rooms panel
**Task**: 2.1  
**Description**: Rooms panel with rename and group controls.  
**Status**: Pending

---
