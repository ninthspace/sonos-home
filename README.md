# sonos

Play audio files from this Mac on Sonos speakers without the Sonos app.

`sonos play` serves the chosen files from a small temporary web server on the
Mac and tells the room to play them, so the speakers fetch each file
themselves. There is no AirPlay, no re-encoding of files Sonos can already
play, and no lag on pause or skip.

## Install

Requires [uv](https://docs.astral.sh/uv/) and ffmpeg (`brew install ffmpeg`).

```sh
uv tool install -e .     # puts `sonos` on your PATH
```

Or run it from this folder with `uv run sonos …`.

## Use

```sh
sonos play ~/Dropbox/Mixes/latest.wav        # play one file in Office
sonos play ~/Dropbox/Album/                  # a folder, recursively, in name order
sonos play -s ~/Dropbox/Album/ more.mp3      # several things, shuffled
sonos -r Bedroom play song.flac              # another room

sonos pause | resume | stop | next | prev
sonos volume 30
sonos status
sonos rooms
```

The room defaults to **Office** (override with `-r` or `$SONOS_ROOM`).
`--ip 192.168.0.21` skips discovery.

`sonos play` stays running while the room plays, because the speakers fetch
each track from it. It exits when the queue finishes, when the room switches
to something else, or after 30 minutes paused (`--idle-minutes`). Ctrl-C
stops playback. While it runs, the Mac is kept from idle-sleeping.

## Formats

Sonos S1 players (Play:1, Play:3, Connect:Amp) play MP3, AAC/M4A, Ogg Vorbis,
WMA, FLAC, ALAC, WAV and AIFF, but only up to **16-bit / 48 kHz, stereo**.
Anything beyond that, such as 24-bit WAVs, 96 kHz FLAC, multichannel or Opus,
is converted to 16-bit FLAC with dither. That is lossless apart from the bit
depth and sample-rate reduction. The first track is converted before playback
starts; the rest are converted in the background.

Converted copies are cached in `~/Library/Caches/sonos-cli` (or
`$SONOS_CLI_CACHE`). `sonos clear-cache` deletes them.

## Notes

- macOS must allow your terminal app **Local Network** access (System
  Settings → Privacy & Security → Local Network), or the speakers can't be
  reached.
- Files are served only under random per-run URLs; nothing else on the Mac
  is exposed. They're plain HTTP on your LAN while `sonos play` runs.
- Starting `sonos play` replaces the room's Sonos queue.

## Development

```sh
uv run pytest
```
