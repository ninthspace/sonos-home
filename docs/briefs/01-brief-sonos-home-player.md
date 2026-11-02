# Product Brief: Sonos Home Player

**Date**: 2026-11-02  
**Source**: conversation (2026-11-02 session that built `sonos play`); README.md

## Problem

The house has five Sonos S1 players: Office (a Play:1 stereo pair), Bedroom (Play:1), Kitchen (Play:3) and Lounge (Connect:Amp). They don't support AirPlay 2, and the Sonos app is the only built-in way to play anything that isn't a streaming service. The user's own music lives as files in Dropbox, synced to their Macs, and much of it is 24-bit WAV, which S1 players can't play. Their released music is on their own Faircamp site (https://shtl-experiment.com/), and they also want to send YouTube and other browser audio to the speakers.

`sonos play` (built this session) already plays local files on a room. But it's a foreground terminal command that has to stay running, and it has no way to pick files, save playlists, manage rooms or play anything from a browser.

## Vision

An always-on service on each of the user's Macs that turns the speakers into a home player they control from any browser on the home network, a phone included. They can browse their music folders, build playlists, group and rename rooms, send a page from their site to a room in one click, and stream any browser tab to the speakers.

## Users and Value

One user (the owner), on their own Macs and phone, on their home Wi-Fi. The value: play their own music at full quality, and anything in a browser, on the speakers without the Sonos app.

## Key Features

### Essential

- Background service, started at login, that serves files and a web UI and controls Sonos rooms.
- Web UI: browse music folders, play or queue, choose a room, see what's playing, transport controls, volume.
- Room management: rename, group, ungroup.
- Playlists saved on the Mac.
- Send a web page's audio (first of all the user's Faircamp release pages) straight to a room.
- Chrome extension: send the current page, or stream the current tab's audio (YouTube) live to a room.

### Enhancing

- Show and jump around the room's queue.
- Cover art for local files.
- Homebrew tap for installation.

## Constraints

- Only the user's Macs: installed with `uv tool install git+<repo>`; no code signing or notarisation.
- Sonos S1 firmware (11.16.1): 16-bit / 48 kHz maximum, no Opus, no AirPlay 2.
- macOS Local Network privacy must allow the service to reach the speakers.
- ffmpeg (Homebrew) is an accepted dependency.
- No login on the web UI (the user's choice); everyone on the home Wi-Fi can use it.

## User Journeys

1. At the desk: open the UI, browse `~/Dropbox/…/GLDR`, click **Play folder** on Office; 24-bit masters play as 16-bit FLAC.
2. On the sofa: open the bookmarked UI on the phone, choose Lounge, set the volume to 25, group Kitchen with it.
3. On shtl-experiment.com: open a release, click the extension's **Send page to Office**; the release's tracks play straight from the site.
4. On YouTube: click **Stream this tab to Office**; the audio plays in the Office a few seconds behind the video.

## Approaches considered

1. **Background service + web UI + Chrome extension (chosen).** One Python codebase grown from the existing CLI. The UI works on every Mac and the phone, and nothing needs signing.
2. **Native Swift menu-bar app.** The best Mac integration, but a full rewrite, no phone control, and signing headaches across Macs. Rejected for now.
3. **AirConnect (AirPlay-to-UPnP bridge) + Apple Music for local files.** Little to build, but a 2-second lag on everything, re-encoding of local files, and no playlists or room management of our own. Kept as a fallback for audio from an app.

## Assumptions

- "Any Mac" means only the user's own Macs, so `uv` plus Homebrew ffmpeg is an acceptable install.
- Local files always reach the speakers unchanged where S1 can play them, and as 16-bit FLAC otherwise (existing behaviour).
- Browser audio arrives a few seconds late and compressed; acceptable for music, not video.
- Plays sent to Sonos aren't counted by shtl-experiment.com's `plays.js`; acceptable.
