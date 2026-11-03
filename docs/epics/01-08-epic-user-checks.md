# User checks: hearing it work in the house

**Source spec**: docs/specifications/01-spec-sonos-home-player.md  
**Date**: 2026-11-02  
**Status**: In Progress  
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

- After `sonos service install`, restart the Mac and log in. Without opening a terminal, open `http://localhost:8737/`: every room that is online is listed (the Office pair counts once), and playing a file on Office is audible. `[user]`

---

## A room that is unplugged comes back
**Story**: 6  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR4, NFR5

**Acceptance Criteria**:

- With `uv run sonos service run` going in a terminal and `curl -s http://127.0.0.1:8737/api/rooms` showing Office, unplug Office (the Play:1 pair). Within 60 s `/api/rooms` shows Office with `"online": false`. Plug it back in. Within 60 s of it joining the network, `/api/rooms` shows Office with `"online": true` again, with no restart. `[user]`

---

## The service installs, restarts and uninstalls
**Story**: 7  
**Status**: Complete  
**Blocked by**: —  
**Satisfies**: FR2, NFR3, AD6  
**Audit**: pass after fixes (2026-11-03), 2 rounds: round 1 (3 Warnings: the queue-label and artist changes made during the checks were recorded nowhere with their tests; the CPU-time amendment wasn't in the contract's form; the run reported as "no tab" was probably taken with a tab still open in the background, so the user re-took it with 0 polls) and round 2 (1 Warning that the red passes' restore wasn't stated; Suggestions: a red attributed to the wrong test, a citation form, the spec's wording for an unreachable room, the measured pid) — all fixed; the last round's fixes are covered by the Evidence and tests and were not audited a third time  
**Signal**: measure-what-you-mean — project — criterion 4 told the user to average `ps`'s `pcpu` column, a smoothed recent-use estimate that one burst skews (1.5 % average against 0.79 % real), and the first run on the original code really was over the limit at 1.5 % (criterion 4; a user check that sets a numeric limit should name a cumulative measure, such as growth of CPU time over elapsed time)  
**Signal**: user-check-condition-unproven — process — a run reported as "no UI open" was taken with a tab still polling in the background, which only the service log showed (criterion 4; a user check whose condition can fail silently needs a step that proves it, here a poll count of 0 in the log, before the figures count)  
**Amended**: criterion 4 measures CPU as the growth of `ps`'s CPU-time column over the elapsed time, not an average of its `pcpu` column, which is a smoothed recent-use estimate that one burst skews (one 7.2 reading among four 0.1s averaged 1.5 % while the CPU time grew 0.79 %) (2026-11-03) — cited: NFR3, criterion 4 (the user chose CPU time in chat, 2026-11-03; the first run's `pcpu` samples are in its Evidence)

**Acceptance Criteria**:

- From the installed tool (`uv tool install` of this repo, so it isn't an editable checkout), with the Mac unlocked, run `sonos service install`. The first run may show a macOS Local Network prompt for SonosService: allow it, and note which prompts appeared and for which app. Then `curl -s http://127.0.0.1:8737/api/health` shows `"ffmpeg":"ok"`, and `curl -s http://127.0.0.1:8737/api/rooms` lists every room that is online (the Office pair counts once). Run `sonos service install` a second time with it loaded: it should replace the agent and the same two `curl`s should still work. `[user]`
  - Evidence (user, 2026-11-03): `uv tool install .` installed sonos-cli 0.1.0 with imageio-ffmpeg 0.8.1 and the `sonos` executable. `sonos service install` wrote `~/Library/LaunchAgents/local.sonos-cli.plist` and printed "The service is running on port 8737 (ffmpeg: ok)"; one macOS Local Network prompt appeared, for SonosService, and was allowed. `/api/health` returned `{"ffmpeg":"ok","rooms_seen":3,"host":"127.0.0.1","port":8737,"sleep_assertion":"released"}`; `/api/rooms` listed Bedroom (.31), Lounge (.26) and Office (.21 with .27 hidden, counted once), all online and STOPPED. Kitchen (.29) was not listed: epic 01-01 had already found it offline (no HTTP or ping answer), the user did not know why it was missing now and chose to ignore it, so it is treated as offline. A second `sonos service install` printed the same lines and replaced the agent (the service log shows the first service, pid 54719, shutting down cleanly and a new one, 56035, starting); `/api/rooms` and `/api/health` returned the same results; `pgrep -fl MacOS/SonosService` showed one launcher (pid 56034, running `python -m sonos_cli service run`) — red: n/a — user
- Find the launcher with `pgrep -f MacOS/SonosService`, then the service Python it started with `pgrep -P <launcher pid>`. `kill -9` the Python one. The launcher exits and launchd starts a fresh one: `pgrep -f MacOS/SonosService` now prints a different pid, `pgrep -P <that new pid>` shows the new Python, and `curl -s http://127.0.0.1:8737/api/health` answers 200 again within 10 s. `[user]`
  - Evidence (user, 2026-11-03): launcher 56034, its one child (Python) 56035. `kill -9 56035; sleep 5; pgrep -f MacOS/SonosService; pgrep -P $(pgrep -f MacOS/SonosService); curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8737/api/health` printed 57123, 57124 and 200: a different launcher, a new Python child, and health printed 200 about 5 s after the kill (the `curl` ran after `sleep 5` and two `pgrep`s), inside the 10 s limit. The service log shows the old process (56035) replaced by 57124 — red: n/a — user
- `sonos service status` prints "loaded", "healthy" and the log path. After `sonos service uninstall`, `~/Library/LaunchAgents/local.sonos-cli.plist` is gone and `curl -s http://127.0.0.1:8737/api/health` is refused. `[user]`
  - Evidence (user, 2026-11-03): `sonos service status` printed "launchd agent: loaded (/Users/chris/Library/LaunchAgents/local.sonos-cli.plist)", "service: healthy on port 8737 (ffmpeg ok, 3 room(s) seen)" and "log: /Users/chris/Library/Logs/sonos/service.log". `sonos service uninstall` printed "Removed the service."; `ls ~/Library/LaunchAgents/local.sonos-cli.plist` then said "No such file or directory", and `curl -s http://127.0.0.1:8737/api/health` printed nothing. Checked afterwards in the session: the same request gives curl exit 7 (connection refused) and no SonosService or `sonos_cli service` process remains — red: n/a — user
- With the service installed and no UI open, `ps -o rss=,time= -p PID` (PID is the Python process from `pgrep -P`, not the launcher's) sampled once a minute for 5 minutes stays under 150 MB (153600 kB) and averages under 1 % CPU, taken as the growth of the CPU-time column over the elapsed time. With one UI tab polling (once the UI exists, epic 01-03), the same holds. `[user]`
  - Evidence (user, 2026-11-03), part 1 of 2, **with one UI tab**: first run on the original code: five `pcpu` samples averaged 2.1 %, and a 120 s CPU-time measurement gave 1.46 % and then 1.58 % (0.9 requests/s, one tab) — over the limit. Cause found: a watched room that was doing nothing was read from the speaker every 2 s, about four SOAP calls each; fixed in `rooms.py` (a watched room that is stopped or unreachable is read every 6 s, `idle_poll_interval`; playing, paused and pinned rooms stay at 2 s), spec line 122 amended, tests `tests/test_rooms.py::test_a_watched_room_that_is_doing_nothing_is_read_every_idle_interval_not_every_poll_interval`, `::test_a_watched_room_that_is_playing_is_still_read_every_poll_interval`, `::test_a_watched_room_playing_something_else_is_also_read_every_poll_interval` and `::test_a_watched_room_that_stops_answering_is_retried_every_idle_interval_even_if_it_was_playing`; reds: removing the backoff failed the `doing_nothing` and `stops_answering` tests; backing off playing rooms failed `is_playing` and `playing_something_else`; backing off any room not playing ours failed `playing_something_else`; counting an unreachable room as busy failed `stops_answering`; backing off pinned rooms failed the older `tests/test_rooms.py::test_a_pinned_room_is_polled_even_when_it_is_stopped_and_unwatched_until_it_is_unpinned`. Each red pass ended with `rooms.py` and `tests/test_rooms.py` byte-identical to before (checksums compared) Run on the fixed code, Python process under the launcher, tab polling `/state` every 2 s (10 requests in 20 s in the service log), `ps -o rss=,pcpu=,time=` once a minute, five samples (four one-minute intervals, 240 s): RSS 44400, 45408, 44368, 42832, 32608 kB (at most 44.3 MiB); CPU time 3.17, 3.71, 4.30, 4.77, 5.06 s = 1.89 s = **0.79 %** (`pcpu` read 0.1, 0.1, 7.2, 0.1, 0.1) — under 150 MB and 1 % — red: n/a — user
  - Evidence (user, 2026-11-03), part 2 of 2, **no UI open**: an earlier run reported as "no tab" (CPU time 6.62 to 7.67 s = 0.35 %) was set aside because the service log showed a tab still polling, so a tab was probably open in the background; it stands only as an upper bound for a hidden tab. Re-taken on the reinstalled service with every `localhost:8737` tab closed: the count of `/state` requests in the service log did not move over 20 s (0). Python process under the launcher (pid 79845; the last request in the service log came before the window and none followed it), six samples of `rss=` and `time=` a minute apart (five intervals, 300 s): RSS 76032, 76032, 68656, 31152, 26592, 42720 kB (at most 76032 kB, 74.3 MiB); CPU time 1.81, 1.97, 2.13, 2.31, 2.50, 2.66 s = 0.85 s = **0.28 %** (0.27, 0.27, 0.30, 0.32, 0.27 % per minute). Under 150 MB (153600 kB) and 1 % — red: n/a — user

