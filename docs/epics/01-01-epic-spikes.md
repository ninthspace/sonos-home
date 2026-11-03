# Spikes: launchd permissions, S1 stream playback, Chrome tab capture

**Source spec**: docs/specifications/01-spec-sonos-home-player.md  
**Date**: 2026-11-02  
**Status**: Complete  
**Blocked by**: —

## launchd service can reach speakers, Dropbox and ffmpeg
**Story**: 1  
**Status**: Complete  
**Blocked by**: —  
**Satisfies**: FR2, AD6  
**Effort**: high  
**Amended**: criterion 1 asks for every online speaker instead of all 5 rooms, because Kitchen (192.168.0.29) was offline and gets no HTTP or ping from Terminal either (2026-11-03) — cited: the user's decision this session, and the probe logs  
**Audit**: pass (2026-11-03), 3 findings fixed
**Signal**: evidence-claims-unlogged — project — Story 1 Evidence cited reachability facts and prompt outcomes with no saved log (criterion 1; save terminal checks to a log as they are run)

**Acceptance Criteria**:

- A throwaway LaunchAgent started by launchd (not from a terminal) runs a probe script. The script logs SoCo discovery finding every speaker that is online (Office, Bedroom, Lounge; Kitchen was offline), an HTTP GET to `192.168.0.21:1400/xml/device_description.xml` returning 200, `ffprobe` (found by absolute path) reading a file under `~/Dropbox`, and the outcome of each permission prompt shown. `[manual]`
  - Evidence: `tests/launchd_probe/run.sh a` and `b` (agents started by launchd: Python's parent is launchd (`ppid 1`) for A and the launcher for B; run after you unlocked the Mac and reported granting every permission pop-up, without naming them, so which prompts appeared isn't recorded). Variant A logged discovery of Office, Bedroom and Lounge, `GET device_description.xml: 200`, `ffprobe rc 0` on `~/Dropbox/opus-opus/z0ne2.wav` (found by absolute path, duration 1358 s) and a 1 MB read from Dropbox. Variant B logged the same. The log shows 3 names (the Office pair counts once). A Terminal check saved in `tests/launchd_probe/reachability.log` shows Office's second member (.27) answering HTTP and ping and four `_sonos._tcp` instances; Kitchen (.29) gets no HTTP and no ping, so launchd isn't the cause. The criterion was amended to every online speaker, at your choice. Permission outcomes: before the grant, errno 65 and a blocked Dropbox read with no prompt shown (screen locked); after it, everything passes. — red: n/a — manual
- The same probe still passes after the executable's underlying Python is replaced (simulating `uv tool upgrade`), or the re-grant step needed is written down. `[manual]`
  - Evidence: with the Python replaced by a different build (uv's 3.11.13 in place of 3.12.11), variant A fails (`discover: []`, `GET ERR … errno 65`, no prompt) while variant B passes in full (3 rooms, GET 200, ffprobe, Dropbox read). A copy of the original Python at a new path passes under A, so only a changed binary loses the grant. The re-grant step for A is untested: no prompt appears, so it would presumably mean toggling Local Network in System Settings. B avoids the need. — red: n/a — manual
- AD6 in the spec is amended to name the chosen executable (wrapper bundle or uv tool Python), with the evidence from the two criteria above, and the throwaway agent is unloaded and removed. `[manual]`
  - Evidence: spec AD6 now names the signed app-bundle wrapper, with the results above. All probe agents are unloaded (`launchctl list` shows none) and their plists were written only to scratch directories. `tests/launchd_probe/` stays in the repo as the record and the starting point for story 8's installer. — red: n/a — manual

### Probe LaunchAgent
**Task**: 1.1  
**Description**: Write the probe script and a throwaway plist in the scratch dir; load it with `launchctl bootstrap gui/$UID`; collect its log.  
**Status**: Complete

### Executable identity
**Task**: 1.2  
**Description**: Try the uv tool Python directly and an ad-hoc-signed minimal `.app` wrapper with `NSLocalNetworkUsageDescription`/`NSBonjourServices`; record which keeps its grants.  
**Status**: Complete

---

## S1 plays remote HTTPS MP3 and a live mp3radio stream from the Mac
**Story**: 2  
**Status**: Complete  
**Blocked by**: —  
**Satisfies**: AD4, AD5, NFR2  
**Amended**: criterion 3 measured the ingest delay from the encoder's output byte count against the MP3 bitrate rather than a marked frame, and the Sonos buffer from the time to PLAYING after `play_uri` rather than position reporting (2026-11-03) — cited: spec NFR2, whose real figure is measured by the user (`[user]`)  
**Audit**: pass (2026-11-03), 8 findings fixed  
**Signal**: spike-evidence-unsaved — project — live-run console output wasn't saved, so the auditor couldn't confirm several Evidence claims (Story 2 criteria 1–3; save live-run output to a log file under the repo or scratchpad)  
**Effort**: high

**Acceptance Criteria**:

- On muted Office (live-test rules: mute every member, restore volume and mute in a separate `finally`), a queued `https://shtl-experiment.com/hello-ep/1/mp3-v5/…/01%20Halyard.mp3` URL reaches PLAYING with its position advancing over 10 s. Or the failure is recorded, and AD5 is amended to proxy. `[manual]`
  - Evidence: failure recorded. On muted Office (both members muted, volume and mute restored in a `finally`, checked at 76/unmuted after each run), `add_to_queue` + `play_from_queue(0)` of `https://shtl-experiment.com/hello-ep/1/mp3-v5/Ui6CBz2DJ_s/01%20Halyard.mp3` stayed STOPPED with an empty title for 17.5 s. `play_uri` with https and with `http://` (the site answers http with a 301) did the same for 10 s each, and `x-rincon-mp3radio://shtl-experiment.com/…` and `x-rincon-mp3radio://http://…` were refused with UPnP 701 (neither tested HTTPS). The 301, the Let's Encrypt/TLS 1.3 certificate and the firmware version were read in the session (`curl`, the earlier discovery). The console output of `remote.py`, `variants.py` and `drive.py` wasn't saved, so the STOPPED states, 701 errors, PLAYING time and positions here are from the session transcript, and only the server side (`proto.log`) is on disk. The server itself returns 200 audio/mpeg over TLS 1.3 (Let's Encrypt). AD5 is amended to proxy (spec AD5, AD7, FR13, FR17; epics 01-02 Stories 4 and 6, 01-06 Story 1). Scripts: `remote.py`, `variants.py` in the session scratchpad. — red: n/a — manual
- On muted Office, `x-rincon-mp3radio://<mac-ip>:<port>/live/test.mp3` is served by a prototype of AD4: a WAV piped through ffmpeg into a ring buffer, sent with no chunked encoding. It stays PLAYING for 60 s. The prototype logs the request headers Sonos sends (HEAD/GET, `Icy-MetaData`) and any reconnects. `[manual]`
  - Evidence: on muted Office, `live_proto.py` (ffmpeg MP3 320 into a bounded ring buffer, raw-socket HTTP/1.0-style responses, no chunking) served `x-rincon-mp3radio://192.168.0.204:8765/live/test.mp3` from a looped WAV. The state went TRANSITIONING to PLAYING 4.28 s after `play_uri`, and was PLAYING with position advancing in real time at t+10, 20, … 70 s (pos 0:06 … 1:08). Headers logged: two GETs and no HEAD. The first has `CONNECTION: close`, `ACCEPT: */*`, `USER-AGENT: Linux UPnP/1.0 Sonos/57.23-74170 (ZPS1)`, `X-Sonos-SWGen: 1` and was closed by Sonos within about 65 ms (proto.log). The second, the streaming one, adds `Icy-MetaData: 1` and the `Nullsoft Winamp3` user-agent suffix, and ran to the end of the test. No reconnects, no Range. Volume and mute restored to 76/unmuted. — red: n/a — manual
- A first delay figure is recorded in the spec under NFR2: the time from writing a marked frame into ingest to that frame being sent to the speaker, plus the Sonos buffer as estimated from position reporting. AD4 is amended with anything the headers or reconnects showed. `[manual]`
  - Evidence: spec NFR2 now records the figure, from the same 70 s run. Logged every 100th chunk: ingest to send 0.042–0.121 s. The ingest figure is inferred from output bytes against the 40 kB/s bitrate, not from a marked frame. With the 8-chunk startup burst (about 0.26 s) and a Sonos buffer of about 3.3–4.3 s (the 4.28 s PLAYING time is polled once a second), the estimate is 3.7–4.7 s. AD4 is amended with the two-GET behaviour, mixed-case header names and the 4.3 s start. — red: n/a — manual

### Remote URL check
**Task**: 2.1  
**Description**: Queue one shtl-experiment.com MP3 on muted Office and watch transport state and position.  
**Status**: Complete

### Live stream prototype
**Task**: 2.2  
**Description**: Minimal ring-buffer MP3 server in the scratch dir; play it as `x-rincon-mp3radio` on muted Office; log requests and timings.  
**Status**: Complete

---

## Chrome tab capture keeps streaming in the background, under automation
**Story**: 3  
**Status**: Complete  
**Blocked by**: —  
**Satisfies**: FR15, AD4  
**Audit**: pass (2026-11-03), 1 finding fixed
**Signal**: harness-flags-undocumented — project — the measured run used flags the README and spec omitted (Story 3 criterion 2; re-run with exactly the documented flags)

**Acceptance Criteria**:

- A prototype MV3 extension (tabCapture, offscreen document, `AudioContext({sampleRate: 48000})`, AudioWorklet producing s16le) streams a local page playing a 440 Hz tone to a local WebSocket sink for 60 s, with the tab in the background. The sink receives 48 kHz stereo PCM with no gap over 200 ms, and the audio decodes to 440 Hz ± 5 Hz. `[manual]`
  - Evidence: `tests/extension_harness/` (`driver.py`, `sink.py`, `ext/`). A 440 Hz tab was captured for 60 s with a second tab in front of it. The sink received 3044 messages, 60.88 s of audio over 60.86 s wall time, as 48 kHz stereo s16le. The largest gap between messages was 36 ms, and none exceeded 200 ms. After the audit the harness was changed to match its docs (no `--load-extension`, no `--disable-features`) and the 60 s run was repeated: 3044 messages, 60.88 s of audio over 60.86 s, largest gap 42.7 ms, 440.0 Hz in the first, middle and last 5 s. Both channels carry the same signal, since the test page is mono, so this proves the 2-channel format, not channel separation. The peak was 440.0 Hz in the first, middle and last 5 s. (An earlier run that appeared to have a 14.5 s gap was the sink concatenating two connections; the sink now resets per connection.) — red: n/a — manual
- The automation method `do` will use for `[browser]` extension criteria is written into the spec's Testing Strategy, and shown to start a capture without a human click. Candidates: Chrome launched with `--load-extension` and `--allowlisted-extension-id`/`--auto-accept-this-tab-capture`, via Playwright or chrome-devtools. `[manual]`
  - Evidence: spec Testing Strategy now names the method (CDP `Extensions.loadUnpacked`, `--allowlisted-extension-id`, `--auto-accept-this-tab-capture`, a pinned manifest `key`, and `startCapture` evaluated in the service worker). The run above started with no click. Without the allowlist flag the same call fails with "Extension has not been invoked for the current page", and `--load-extension` is ignored by Chrome 156, so the harness doesn't use it. — red: n/a — manual

### Prototype extension and sink
**Task**: 3.1  
**Description**: Build the prototype in the scratch dir; a Python WebSocket sink records PCM to WAV for analysis.  
**Status**: Complete

---
