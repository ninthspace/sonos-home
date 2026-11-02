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

- After `sonos service install`, restart the Mac and log in. Without opening a terminal, open `http://localhost:8737/`: all five rooms are listed, and playing a file on Office is audible. `[user]`

---
