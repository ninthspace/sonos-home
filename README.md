# sonos

Play audio files from your Mac on Sonos speakers without the Sonos app.

A small background service on the Mac serves your music folders to the speakers, keeps track of the rooms, and
offers a web page (and a CLI) to browse, play, queue and group. The speakers fetch each file from the Mac
themselves: no AirPlay, no re-encoding of files Sonos can already play.

## TL;DR

```sh
uv tool install .                                    # needs only uv; ffmpeg comes with it
mkdir -p ~/Library/Application\ Support/sonos
echo 'music_folders = ["~/Dropbox/Music"]' >> ~/Library/Application\ Support/sonos/config.toml   # new install; otherwise edit that file
sonos service install                                # runs at login; allow Local Network access if macOS asks
open http://localhost:8737/                          # pick a room, browse, press Play (a phone on the same Wi-Fi works too)
sonos play ~/Dropbox/Music/some-album/               # or from the CLI, in the default room (Office); the path must be inside a music folder
```

No login: anyone on your network can use the page. Details below.

## Install

Requires only [uv](https://docs.astral.sh/uv/). There is nothing else to install: ffmpeg comes with the tool (no Homebrew), and
the background service needs no Xcode command line tools and no compiler.

**Where ffmpeg comes from.** The `imageio-ffmpeg` package carries a static ffmpeg for Apple silicon and Intel Macs, and `uv`
installs it with the tool, pinned to one version in `pyproject.toml` (`uv.lock` also records the wheels' SHA-256, which `uv sync` and `uv run` in a checkout verify; `uv tool install .` does not read the lock). The tool looks for ffmpeg and ffprobe in this order and uses the
first it finds: the `ffmpeg` / `ffprobe` path in `config.toml`, the bundled copy (ffmpeg only), `PATH`, `/opt/homebrew/bin`,
`/usr/local/bin`. The bundled build has no ffprobe, so files are probed with ffmpeg itself; an ffprobe you have is used when there
is one. `GET /api/health` shows `ffmpeg: ok` or `ffmpeg: missing`.
Set `ffmpeg = "…"` in `config.toml` if you would rather use your own build.

**Licence.** The tool is for your own use on your own Macs. The bundled ffmpeg is a GPL build (it includes x264 and x265; the
tool only uses it for FLAC and MP3 conversion, probing and live encoding) and `imageio-ffmpeg` itself is BSD-2-Clause. If you ever
hand the tool to someone else, they receive that GPL ffmpeg with it: the source is at <https://ffmpeg.org/> and the build
recipe is the `imageio-ffmpeg` project's.

```sh
uv tool install .        # puts `sonos` on your PATH
```

Or run it from this folder with `uv run sonos …`.

## Tell it where your music is

There are no default music folders. Create `~/Library/Application Support/sonos/config.toml`:

```toml
music_folders = ["~/Dropbox/Music", "~/Music"]
port = 8737                 # optional
room = "Office"             # optional: the default room
extension_ids = []          # Chrome extension IDs allowed to call the API (see Chrome extension below)
pause_grace_minutes = 30    # optional: how long a pause keeps the Mac awake
# ffmpeg = "/opt/homebrew/bin/ffmpeg"   # optional: your own build instead of the bundled one
```

Only files inside these folders can be played or browsed. A path outside them is refused, and the error names this
file.

## Run the service

```sh
sonos service run            # in a terminal, on port 8737
sonos service install        # run it at login with launchd, restart it if it stops
sonos service status         # loaded? healthy? where is the log?
sonos service uninstall
```

`install` makes a tiny signed launcher app (`SonosService.app` in the config folder) from a prebuilt launcher that ships with the tool, so macOS attributes Local
Network access to it and not to your Python. The first time, macOS may ask to let it find devices on your local
network: allow it, with the screen unlocked. It refuses an editable checkout; install with `uv tool install .` first.

Open `http://<this Mac>:8737/` in a browser (a phone on the same Wi-Fi works). Pick a room, browse a folder, press
Play. The queue panel shows what is queued and lets you jump to a track or clear it. There is no login: anyone on
your network can use it. Requests from other websites in your own browser are refused.

## Use the CLI

```sh
sonos play ~/Dropbox/Mixes/latest.wav        # play one file in Office
sonos play ~/Dropbox/Album/                  # a folder, recursively, in name order
sonos play -s ~/Dropbox/Album/ more.mp3      # several things, shuffled
sonos play -a more.mp3                       # add to the queue instead of replacing it
sonos -r Bedroom play song.flac              # another room

sonos pause | resume | stop | next | prev
sonos volume 30
sonos status
sonos rooms
sonos rename Office Studio
sonos group Kitchen Lounge | sonos ungroup Kitchen | sonos group --all

sonos send https://shtl-experiment.com/hello-ep/   # play the audio a web page offers (-a adds to the queue)
```

With the service running, every command goes through it. Without it, `sonos play` serves the files itself and stays
running while the room plays (Ctrl-C stops), and the other commands talk to the speakers directly. Playing a folder
queues up to 500 tracks; hidden folders, links that lead outside your music folders and online-only Dropbox files are
skipped, with a reason.

The room is `-r`, else `$SONOS_ROOM`, else `room` in `config.toml`, else **Office**. `--ip 192.168.0.21` picks the
room that speaker belongs to (and skips discovery without the service); `$SONOS_IP` does the same unless you give `-r`.

## Send a web page's audio

`sonos send <url>` (or `POST /api/send`) finds the audio a page offers (a Faircamp release page gives its MP3 tracks with their
titles and the release's artist; any other page gives its `<audio>` sources and links to MP3, AAC, FLAC and Ogg Vorbis) and queues it on the room. S1
players can't fetch `https://` addresses, so each track is queued as `http://<this Mac>:8737/remote/<signed token>/<title>.mp3` (the title and, for a Faircamp release, `by <artist>`, because the speaker labels a track it hasn't read yet from that last part) and the service
fetches the real file when the speaker asks, so it must stay running while the room plays. It only ever fetches addresses it
signed itself, and only public internet addresses: loopback, your LAN and other private ranges are refused.

## Chrome extension

`extension/` is a Manifest V3 extension with a popup that sends the current page's audio to a room (**Send page**, offered when the
page has MP3, AAC, FLAC or Ogg Vorbis links) and streams the tab's own audio to a room (**Stream tab** / **Stop**, on any page, for
YouTube and the like). A streamed tab is encoded as MP3 on this Mac and plays a few seconds behind the tab; it keeps going when
the popup closes or the tab is in the background, and ends when you press Stop, close the tab, or the room switches to another
source. The popup says which. *Also play here* keeps the tab audible on this Mac while it streams, held back about 4 s so it sounds together with the speaker (off by default: capturing a tab mutes it). Only one tab streams at a time. Load it from `chrome://extensions` with *Developer mode* on and
*Load unpacked* pointing at `extension/`. Its ID is fixed by the key in its manifest: `fjgldlknkfejofhiebpnaiepahdhknpp`. Allow
it in `~/Library/Application Support/sonos/config.toml` and restart the service:

```toml
extension_ids = ["fjgldlknkfejofhiebpnaiepahdhknpp"]
```

The extension only talks to `http://localhost:8737`, so the service must run on this Mac on the default port. It asks Chrome for
`activeTab` and `scripting` (to read the page you click it on), `tabCapture` and `offscreen` (to stream a tab) and `storage` (to
remember your room), and for no other website. Requests from any other extension, or from a web page, are refused.

## Formats

Sonos S1 players (Play:1, Play:3, Connect:Amp) play MP3, AAC/M4A, Ogg Vorbis, WMA, FLAC, ALAC, WAV and AIFF, but only
up to **16-bit / 48 kHz, stereo**. Anything beyond that, such as 24-bit WAVs, 96 kHz FLAC, multichannel or Opus, is
converted to 16-bit FLAC with dither: lossless apart from the bit depth and sample-rate reduction. The first track is
converted before it plays; the others when the speaker first asks for them. Converted copies are cached in
`~/Library/Caches/sonos-cli` (or `$SONOS_CLI_CACHE`); `sonos clear-cache` deletes them.

## Notes

- Give the Mac a fixed address (a DHCP reservation in your router): queued tracks carry its IP.
- The Mac stays awake while a room plays or is paused (up to `pause_grace_minutes`) on a file this service serves.
- Files are served over plain HTTP on your LAN, only from your music folders.

## Development

```sh
uv run pytest
```

`sonos service run --fake-rooms` runs the service against an in-memory Sonos system (four rooms) for trying the web
page without speakers. `tests/live.py` is the helper for tests that touch a real room: it mutes the room first and
restores volume and mute afterwards.
