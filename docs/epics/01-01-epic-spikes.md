# Spikes: launchd permissions, S1 stream playback, Chrome tab capture

**Source spec**: docs/specifications/01-spec-sonos-home-player.md  
**Date**: 2026-11-02  
**Status**: Pending  
**Blocked by**: —

## launchd service can reach speakers, Dropbox and ffmpeg
**Story**: 1  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR2, AD6  
**Effort**: high

**Acceptance Criteria**:

- A throwaway LaunchAgent started by launchd (not from a terminal) runs a probe script. The script logs SoCo discovery finding all 5 rooms, an HTTP GET to `192.168.0.21:1400/xml/device_description.xml` returning 200, `ffprobe` (found by absolute path) reading a file under `~/Dropbox`, and the outcome of each permission prompt shown. `[manual]`
- The same probe still passes after the executable's underlying Python is replaced (simulating `uv tool upgrade`), or the re-grant step needed is written down. `[manual]`
- AD6 in the spec is amended to name the chosen executable (wrapper bundle or uv tool Python), with the evidence from the two criteria above, and the throwaway agent is unloaded and removed. `[manual]`

### Probe LaunchAgent
**Task**: 1.1  
**Description**: Write the probe script and a throwaway plist in the scratch dir; load it with `launchctl bootstrap gui/$UID`; collect its log.  
**Status**: Pending

### Executable identity
**Task**: 1.2  
**Description**: Try the uv tool Python directly and an ad-hoc-signed minimal `.app` wrapper with `NSLocalNetworkUsageDescription`/`NSBonjourServices`; record which keeps its grants.  
**Status**: Pending

---

## S1 plays remote HTTPS MP3 and a live mp3radio stream from the Mac
**Story**: 2  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: AD4, AD5, NFR2  
**Effort**: high

**Acceptance Criteria**:

- On muted Office (live-test rules: mute every member, restore volume and mute in a separate `finally`), a queued `https://shtl-experiment.com/hello-ep/1/mp3-v5/…/01%20Halyard.mp3` URL reaches PLAYING with its position advancing over 10 s. Or the failure is recorded, and AD5 is amended to proxy. `[manual]`
- On muted Office, `x-rincon-mp3radio://<mac-ip>:<port>/live/test.mp3` is served by a prototype of AD4: a WAV piped through ffmpeg into a ring buffer, sent with no chunked encoding. It stays PLAYING for 60 s. The prototype logs the request headers Sonos sends (HEAD/GET, `Icy-MetaData`) and any reconnects. `[manual]`
- A first delay figure is recorded in the spec under NFR2: the time from writing a marked frame into ingest to that frame being sent to the speaker, plus the Sonos buffer as estimated from position reporting. AD4 is amended with anything the headers or reconnects showed. `[manual]`

### Remote URL check
**Task**: 2.1  
**Description**: Queue one shtl-experiment.com MP3 on muted Office and watch transport state and position.  
**Status**: Pending

### Live stream prototype
**Task**: 2.2  
**Description**: Minimal ring-buffer MP3 server in the scratch dir; play it as `x-rincon-mp3radio` on muted Office; log requests and timings.  
**Status**: Pending

---

## Chrome tab capture keeps streaming in the background, under automation
**Story**: 3  
**Status**: Pending  
**Blocked by**: —  
**Satisfies**: FR15, AD4

**Acceptance Criteria**:

- A prototype MV3 extension (tabCapture, offscreen document, `AudioContext({sampleRate: 48000})`, AudioWorklet producing s16le) streams a local page playing a 440 Hz tone to a local WebSocket sink for 60 s, with the tab in the background. The sink receives 48 kHz stereo PCM with no gap over 200 ms, and the audio decodes to 440 Hz ± 5 Hz. `[manual]`
- The automation method `do` will use for `[browser]` extension criteria is written into the spec's Testing Strategy, and shown to start a capture without a human click. Candidates: Chrome launched with `--load-extension` and `--allowlisted-extension-id`/`--auto-accept-this-tab-capture`, via Playwright or chrome-devtools. `[manual]`

### Prototype extension and sink
**Task**: 3.1  
**Description**: Build the prototype in the scratch dir; a Python WebSocket sink records PCM to WAV for analysis.  
**Status**: Pending

---
