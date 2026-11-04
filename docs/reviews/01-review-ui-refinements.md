# Review: UI refinements: faders, seek, folders, queue feedback

**Date**: 2026-11-03  
**Source**: docs/epics/01-10-epic-ui-refinements.md  
**Reviewers**: Jordan (Product Manager), Bella (Senior Developer), Casey (Test Engineer)  
**Findings**: 30 (6 critical, 20 warnings, 4 suggestions)

## Summary

The three reviewers worked independently and agreed on the main problems. The browser checks couldn't be run as written: fake rooms never move their playhead outside tests, the checks would have edited the user's real config, and several timings had no named way to be measured. The Story 6 security criterion had lost its list bullet, so `do` would have skipped it. Swapping the music library at runtime, and editing only one TOML value, were much harder than the tasks said. The fixes rewrote the epic (all stories Pending) into ten stories: a real-time fake clock first, Story 6 split into an API story and a UI story, and a real-speaker check for every observation. The findings below were merged across reviewers and checked against the code (`fakes.py:313`, `app.js:74,206,255,278`, `service.py:577,964`, `config.py:27`, `tests/test_send.py:180`). Two findings wait on the user.

## Findings

- **[Critical]** 🧪 **Casey**, 💻 **Bella**, 📋 **Jordan**: fake rooms never advance their position in `sonos service run --fake-rooms` (`FakeSoCo.advance` is test-driven only, `fakes.py:313`), so the smooth-bar checks would sample a sawtooth and their control would pass with 0 changes; nothing could put a fake track near its end or move it back.  
  → Story 1 (old), criteria 1–2: the checks prove nothing as written.
- **[Critical]** 🧪 **Casey**, 💻 **Bella**: "width increases at least 12 times in 4 s" can't pass on a 4-minute track while the width is rounded to whole percent (`app.js:206`), and the sampling method wasn't named.  
  → Story 1 (old), criterion 1.
- **[Critical]** 🧪 **Casey**, 💻 **Bella**, 📋 **Jordan**: the "not this Mac" criterion had no leading `- `, so under the contract's reading rule it wasn't a criterion at all; it held the 403 floor, header spoofing and the NFR6 regression together.  
  → Story 6 (old), the line after criterion 6.
- **[Critical]** 💻 **Bella**: `renderRooms` rebuilds every strip with `replaceChildren()` on every poll (`app.js:74`), so a fader would lose the pointer mid-drag and lose keyboard focus every 5 s; the task didn't say so.  
  → Story 3 (old), criteria 2–3, task 3.1.
- **[Critical]** 💻 **Bella**: the library is held by five objects (the browse closure, `MediaServer`, `Player`, `PlaylistStore`, `app.state`), all frozen dataclasses, so "swap the Library under a lock" isn't one swap; removing a folder mid-queue would make `_queue_rest` raise.  
  → Story 6 (old), task 6.1, criterion 2.
- **[Critical]** 💻 **Bella**, 🧪 **Casey**: "change only the `music_folders` value byte for byte" without a TOML writer means a hand-written parser, and no floor of TOML forms was named (multi-line arrays, comments, `]` and `#` in strings, a key in a sub-table, no trailing newline, insertion before a table header).  
  → Story 6 (old), criterion 3.
- **[Warning]** 🧪 **Casey**, 💻 **Bella**, 📋 **Jordan**: the browser runs would read and write the user's real `config.toml` (the installed service's file), since nothing set `SONOS_CLI_CONFIG_DIR`.  
  → Epic preamble, Story 6 (old) criteria 8–9.
- **[Warning]** 💻 **Bella**, 📋 **Jordan**: extrapolating from the response's arrival time makes the bar jump back at each poll, because the state comes from a monitor cache up to 2 s old; the state's `updated` time should be used and the phone's clock not trusted.  
  → Story 1 (old), task 1.1.
- **[Warning]** 💻 **Bella**: the time text would still step every 2 s while the bar moved smoothly.  
  → Story 1 (old).
- **[Warning]** 🧪 **Casey**, 📋 **Jordan**, 💻 **Bella**: no criteria for a track with no duration, TRANSITIONING, offline or unreachable, or the race between Pause and the next poll; `prefers-reduced-motion` can't be emulated through claude-in-chrome.  
  → Story 1 (old), criteria 2–3.
- **[Warning]** 🧪 **Casey**, 💻 **Bella**: the seek API's bad-input floor missed what `json.loads` accepts (`true`, `"65"`, `NaN`, `Infinity`), rounding, `position == duration`, `NOT_IMPLEMENTED` durations, a non-object body, and the speaker refusing; the duration was read from a possibly stale cache.  
  → Story 2 (old), criterion 2.
- **[Warning]** 🧪 **Casey**: the recurring signal speaker-metadata-differs-from-what-we-send bears on seek refusal (it rests on the duration the speaker reports), and no criterion read a real speaker's durations for each source.  
  → Story 2 (old); Lessons.
- **[Warning]** 🧪 **Casey**, 💻 **Bella**, 📋 **Jordan**: the seek UI criterion held four proofs; drag needed its own state and a request count; held arrow keys weren't coalesced or clamped; the disabled-bar must-not had no control; the bar is about 10 px tall with no touch criterion; no criterion covered a refused seek in the page.  
  → Story 2 (old), criteria 3–4.
- **[Warning]** 🧪 **Casey**: the fader check couldn't fail (every fake room starts at volume 30, `fakes.py:75`, and the target was "about 30"), and its "control" wasn't one.  
  → Story 3 (old), criterion 1.
- **[Warning]** 🧪 **Casey**, 💻 **Bella**: the drag-survives-polls criterion couldn't be measured as tagged (claude-in-chrome's drag is one action; the access log has no timestamps; a 3 s drag may hold no 5 s poll), and offered two methods.  
  → Story 3 (old), criterion 3.
- **[Warning]** 💻 **Bella**, 📋 **Jordan**: no criteria for disabled faders (offline, no volume, unreachable), out-of-order volume requests, snap-back after release, the footer fader staying in step, or vertical range inputs and touch on the phone.  
  → Story 3 (old).
- **[Warning]** 📋 **Jordan**, 💻 **Bella**: `/api/rooms` gives each member its group's volume and the volume route sets group volume, so "each strip's fader changes only its own room" contradicts grouped rooms; the user may expect per-speaker volume.  
  → Story 3 (old), criterion 1.
- **[Warning]** 💻 **Bella**, 📋 **Jordan**: matching the keys' "border" means `border: 0` while the key look is a `box-shadow`; a data-URI SVG chevron can't use the tokens' `var()`; a font under 16 px makes iOS zoom.  
  → Story 4 (old), criterion 1.
- **[Warning]** 🧪 **Casey**, 💻 **Bella**, 📋 **Jordan**: queue rows jump on a single click today (`app.js:278`) but the criteria said double-click; switching to `dblclick` would break taps on the phone, and keeping click without a guard sends two jumps per double-click.  
  → Story 5 (old), criteria 1 and 3.
- **[Warning]** 🧪 **Casey**, 💻 **Bella**, 📋 **Jordan**: the pending state and "within 100 ms" can't be seen against fakes that answer in milliseconds; the pending mark would be wiped by `renderQueue`'s rebuild; the failure set-up (queue cleared in another tab) contradicted "leaves the previous current row highlighted"; errors go to the Music view's note.  
  → Story 5 (old), criteria 1–2.
- **[Warning]** 💻 **Bella**: uvicorn's default `proxy_headers=True` rewrites the client address from `X-Forwarded-For` on loopback, so "never a header" wasn't true in production; `local_addresses()` is IPv4 only and may block on mDNS; a non-IP TestClient peer must count as another device.  
  → Story 6 (old), criterion 7, task 6.2.
- **[Warning]** 💻 **Bella**, 🧪 **Casey**: duplicates compared by real path miss case and NFC/NFD variants on APFS, and nested folders; remove didn't cover `~/…` or relative entries, or the last folder.  
  → Story 6 (old), criteria 4–5.
- **[Warning]** 💻 **Bella**, 🧪 **Casey**, 📋 **Jordan**: the browse check didn't say real paths (a symlink under home, `/Volumes/Macintosh HD` → `/`), status codes weren't named, "readable" should be tested by listing (macOS privacy refusals), and a stalled network share could block.  
  → Story 6 (old), criterion 6.
- **[Warning]** 💻 **Bella**, 🧪 **Casey**: atomic write had no named proof; a symlinked config, file mode, a missing config file, and concurrent adds weren't covered.  
  → Story 6 (old), criterion 3.
- **[Warning]** 🧪 **Casey**, 📋 **Jordan**: the read-only view was to be checked by "a stub or a flag forced off in the page", undefined and mixed with a positive check; no criteria for a refused add in the page, a remove warning about queued tracks and playlists, `window.confirm` (no control), or a double-click on Add or Remove.  
  → Story 6 (old), criteria 8–9.
- **[Warning]** 📋 **Jordan**, 🧪 **Casey**: Story 7 checked only three of the six observations on real speakers (not smooth progress, jump feedback, or the select on the phone), and its config and phone checks relied on the user's judgement instead of a `diff` and a log line.  
  → Story 7 (old).
- **[Warning]** 🧪 **Casey**, 💻 **Bella**: a this-Mac user could add `/`, the home folder or `~/Library`, which would list folder names across the disk to every LAN device through `/api/browse`.  
  → Story 6 (old); spec FR23 Assumptions.
- **[Suggestion]** 📋 **Jordan**: the epic said "six observations" without listing them.  
  → Epic intro.
- **[Suggestion]** 🧪 **Casey**, 📋 **Jordan**: the spec's Testing Strategy still says `uv run pytest` and that every `[user]` criterion is in the final user-checks epic; neither exception is recorded there.  
  → Spec Testing Strategy.
- **[Suggestion]** 🧪 **Casey**, 💻 **Bella**: task 2.1 said the fake's `seek` must move its position, which it already does (`fakes.py:244`).  
  → Story 2 (old), task 2.1.

## Remediation

- Fixed: fake rooms don't advance → new Story 1 (a real-time clock in `--fake-rooms` only, tests keep manual `advance()`, a fake-only position route that is 404 otherwise).
- Fixed: 12 changes in 4 s → Story 2 uses the 240 s fixture tone, fractional width and a named `setInterval` sampler, with a control that cancels the frame loop.
- Fixed: Story 6 security line not a criterion → Story 8 criteria 2–5 as list items: the peer rule, the 403 floor and control, header spoofing, NFR6 on the new routes.
- Fixed: `renderRooms` rebuilds → Story 5 task names updating strips in place, plus criteria for focus across a poll and a drag across a forced poll with a control.
- Fixed: library swap → Story 8 task 8.1 names one library holder for all five users and keeps `extra_roots`; a criterion for a queue job when its folder is removed.
- Fixed: TOML writer → Story 8 names `tomlkit` (added as a dependency; NFR4 only asks for uv) and a floor of file forms, existing entries kept verbatim, the key inserted before the first table.
- Fixed: real config used → the preamble requires `SONOS_CLI_CONFIG_DIR` set to a scratch dir with fixture tones.
- Fixed: arrival-time extrapolation → Story 2 adds `updated`/`now` to the state response and snaps only when `updated` changes.
- Fixed: time text → Story 2 criterion 3.
- Fixed: missing bar states and reduced motion → Story 2 criteria 5–8 (stop on Pause success, near the end, no-duration/TRANSITIONING/offline/unreachable, `aria-valuenow` throttled, no width transition).
- Fixed: seek bad inputs → Story 3 criteria 2–4 name each input and status, rounding down, the duration read from the coordinator, and the speaker refusing (502).
- Fixed: recurring signal → Story 3 criterion 1, a `[manual]` read of Office's raw durations for four sources; the **Lessons** line names it.
- Fixed: seek UI → Story 4: separate click, drag (one request, control), keys (clamp, coalesce), ARIA, disabled with control, refused seek in the page, a 44 px hit area and `touch-action: none` at 375 px.
- Fixed: fader check can't fail → Story 5 criterion 1 sets distinct levels first, targets 70, and names a real control.
- Fixed: drag measurement → Story 5 criteria 4–5 name synthetic pointer events, a forced poll and a `fetch` wrapper, with a control.
- Fixed: fader edge cases → Story 5 criteria 6–8 and the task's vertical-range technique.
- Fixed: group or per-speaker volume on the strip faders → the user chose group volume, as FR9 says (2026-11-03); Story 5 keeps it, with a grouped-room criterion.
- Fixed: select styling → Story 6 compares against the Rename key including `box-shadow` and letter spacing, a 16 px floor, a chevron on a wrapper element, a real Tab press.
- Fixed: click versus double-click → Story 7 keeps single click and tap (no regression on the phone) and guards the double-click, with a control.
- Fixed: jump feedback measurement → Story 7 names a `fetch` wrapper holding the jump 1 s, `timeStamp` against a MutationObserver, a forced re-render, an injected 502, a 10 s timeout and the queue note; the jump route's existing refresh (`service.py:577`) gets an `[integration]` criterion.
- Fixed: proxy headers and peer rule → Story 8 criterion 2 and task 8.2 (`proxy_headers=False`, non-IP peers are other devices, a cached lookup).
- Fixed: duplicates, nested and remove → Story 8 criteria 7 and 10 (device and inode, nested refused, every resolving entry removed, last folder).
- Fixed: browse → Story 8 criterion 13 (real paths, hidden flag, `readable: false`, 5 s limit, status codes).
- Fixed: atomic write and concurrency → Story 8 criteria 9 and 12.
- Fixed: read-only view and UI gaps → Story 9 (a fake-rooms-only `--no-local-clients` option with a control, refused add, remove warning, no `window.confirm` with a recorder, one request per click, the no-folders view).
- Fixed: Story 7 coverage → Story 10 checks all six observations, blocked by Stories 2, 4, 5, 6, 7 and 9, with a `diff`, the phone's IP and its log line.
- Not acted on: broad folders (`/`, the home folder, `~/Library`) → the user chose to allow any readable folder from this Mac, as FR23 says (2026-11-03); Story 8 only refuses folders inside or containing a configured one.
- Fixed: observations not listed → the epic intro lists the six with their stories.
- Not acted on: spec Testing Strategy wording → a spec amendment; recommended for the next `/cpm-next:plan` pass on spec 01.
- Fixed: task 2.1 wording → Story 3 task 3.1 says the fake's `seek` already moves its position.
- Not acted on (Suggestions not applied): `aria-valuetext` chatter beyond the 1 s throttle; two roots with the same name looking alike; Tasks for Story 6 (select).
