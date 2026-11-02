# Send a page's audio to a room

**Source spec**: docs/specifications/01-spec-sonos-home-player.md  
**Date**: 2026-11-02  
**Status**: Pending  
**Blocked by**: Epic 01-02-epic-service-core, Epic 01-01-epic-spikes

## Extract and queue page audio
**Story**: 1  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR13, AD5

**Acceptance Criteria**:

- A saved shtl-experiment.com release page (fixture in `tests/fixtures/`) yields one entry per track, in order, each with its title and an absolute `…/mp3-v5/…` URL, and no `opus` URL. `[unit]`
- A generic fixture page with `<audio>`, `<source>` and `<a href>` links, including relative, space-containing and non-ASCII URLs, yields absolute, percent-encoded URLs for `.mp3`/`.m4a`/`.aac`/`.flac`/`.ogg`, in document order, and skips `.opus`. `[unit]`
- `POST /api/send` with a page URL (fetched with a stubbed HTTP client) or with `html` + `base_url` queues the tracks on the fake coordinator. Each DIDL item's `protocolInfo` matches its MIME type, and titles containing `&`, `<` and non-ASCII characters are escaped correctly in the DIDL. A page with nothing playable returns 422 with a message. `[unit]`
- `POST /api/send/preview` returns the track list without queuing anything. `[unit]`
- `sonos send <url>` calls the endpoint for the default room or `-r`. `[unit]`
- If spike 01-01 Story 2 amended AD5 to proxy, remote tracks are queued under `/remote/…` and served through the service. Otherwise they're queued as the original HTTPS URLs. A test asserts the variant AD5 names. `[unit]`

### Parser and endpoint
**Task**: 1.1  
**Description**: `src/sonos_cli/pages.py` with `html.parser`; Faircamp-specific rule first, generic rule second.  
**Status**: Pending

---

## Send a URL from the UI
**Story**: 2  
**Status**: Pending  
**Blocked by**: Story 1, Epic 01-03-epic-web-ui  
**Satisfies**: FR13

**Acceptance Criteria**:

- In Chrome against fake rooms, pasting a test page URL, served by the test harness, into the UI's Send field queues its tracks on the selected fake room. A page with no audio shows the 422 message. `[browser]`

### Send field
**Task**: 2.1  
**Description**: URL field and result message in the UI.  
**Status**: Pending

---

## A Faircamp track plays on Office
**Story**: 3  
**Status**: Pending  
**Blocked by**: Story 1  
**Satisfies**: FR13

**Acceptance Criteria**:

- Through `tests/live.py` on muted Office, `POST /api/send` with a real shtl-experiment.com release URL reaches PLAYING on track 1, with its position advancing over 10 s and the title matching the page. `[manual]`

---
