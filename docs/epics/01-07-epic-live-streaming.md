# Chrome extension and live tab streaming

**Source spec**: docs/specifications/01-spec-sonos-home-player.md  
**Date**: 2026-11-02  
**Status**: Pending  
**Blocked by**: Epic 01-02-epic-service-core, Epic 01-01-epic-spikes

## Live stream sessions in the service
**Story**: 1  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR16, AD4  
**Effort**: high

**Acceptance Criteria**:

- PCM fed to `/live/ingest?room=<uid>` comes out of `/live/<id>.mp3` as decodable MP3 at 440 Hz ± 5 Hz for a 440 Hz input, and the fake room is told to play `x-rincon-mp3radio://<mac-ip>:<port>/live/<id>.mp3`. `[integration]`
- A listener joining 10 s into the stream receives bytes starting on an MP3 frame sync that decode without error. `[integration]`
- With one listener that never reads, a second listener keeps receiving audio at the live rate. Removing the per-listener drop makes the test fail (control). `[integration]`
- Response headers have `Content-Type: audio/mpeg` and no `Transfer-Encoding: chunked`, `Content-Length` or `icy-metaint`. `[integration]`
- A listener that disconnects and reconnects within 10 s receives decodable audio again. `[integration]`
- Ingest running 60 s with no listener keeps the buffer at its bound. Process memory grows by less than 10 MB over the run. `[integration]`
- When ingest closes, an open `/live/<id>.mp3` response ends within 5 s, and a new request returns 404. `[integration]`
- When the fake room's URI changes to another source, the ingest socket receives `ended: room-switched` and ffmpeg exits within 5 s. No ffmpeg child of the service remains. A second stream started for the same room replaces the first. `[integration]`

### Live session registry
**Task**: 1.1  
**Description**: `src/sonos_cli/live.py`: registry keyed by room UID, ffmpeg process, draining reader, frame-aligned ring buffer, raw ASGI listener response, RoomMonitor subscription.  
**Status**: Pending

---

## Extension popup and Send page
**Story**: 2  
**Status**: Pending  
**Blocked by**: Epic 01-06-epic-send-page  
**Satisfies**: FR14

**Acceptance Criteria**:

- With the extension loaded via the spike 01-01 Story 3 harness against fake rooms, the popup on a local test page with MP3 links shows Send page. Clicking it queues the page's tracks on the chosen fake room. `[browser]`
- On a local page without direct audio, the popup shows Checking… then only Stream tab. When the preview takes over 2 s (stubbed slow), Stream tab is shown without waiting. `[browser]`
- The chosen room is remembered across popup opens, and still selected after that room is renamed in the fake system. `[browser]`
- The manifest declares exactly `tabCapture`, `offscreen`, `storage` and host permission `http://localhost:8737/*`. The service's NFR6 Origin check accepts the extension's configured ID and refuses another. `[unit]`

### Popup
**Task**: 2.1  
**Description**: `extension/manifest.json`, popup, page-HTML capture via `chrome.scripting`, calls to `/api/send/preview` and `/api/send`.  
**Status**: Pending

---

## Stream a tab
**Story**: 3  
**Status**: Pending  
**Blocked by**: Story 1, Story 2  
**Satisfies**: FR15, FR20  
**Effort**: high

**Acceptance Criteria**:

- Through the harness against fake rooms, Stream tab on a local page playing a 440 Hz tone makes `/live/<id>.mp3` decode to 440 Hz ± 5 Hz. The stream keeps going while the popup is closed and the tab is in the background. `[browser]`
- Clicking Stop, and separately closing the tab, each end the stream within 5 s. The popup then shows the reason (`stop` / `tab-closed`). `[browser]`
- With "Also play here" on, the test page's tone is audible locally while streaming (the page's AudioContext destination receives signal). With it off, it isn't. `[browser]`
- Starting Stream tab on a second tab while one is streaming stops the first, so only one stream runs. `[browser]`

### Offscreen capture
**Task**: 3.1  
**Description**: Offscreen document with `AudioContext({sampleRate: 48000})`, AudioWorklet float→s16le, WebSocket with control messages; state in `chrome.storage.session`.  
**Status**: Pending

---

## A streamed tab plays on Office
**Story**: 4  
**Status**: Pending  
**Blocked by**: Story 3  
**Satisfies**: FR15

**Acceptance Criteria**:

- Through `tests/live.py` on muted Office, streaming the 440 Hz test page through the harness makes Office report PLAYING on the `x-rincon-mp3radio` URI for 60 s. Stop returns Office to STOPPED within 10 s. `[manual]`

---
