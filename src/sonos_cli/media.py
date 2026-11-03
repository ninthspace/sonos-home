"""Find audio files, probe them (ffprobe, or ffmpeg itself), and convert what Sonos S1 can't play.

Sonos S1 players play MP3, AAC/M4A, Ogg Vorbis, WMA, FLAC, ALAC, WAV and AIFF,
but only up to 16-bit / 48 kHz and two channels. Anything outside that is
converted to 16-bit FLAC (lossless) and cached.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".mp4", ".aac", ".flac", ".wav", ".aif", ".aiff",
    ".ogg", ".oga", ".opus", ".wma",
}

MAX_SAMPLE_RATE = 48_000
MAX_CHANNELS = 2
MAX_BITS = 16

# Codecs Sonos S1 decodes itself. Lossless ones are also subject to MAX_BITS.
LOSSY_NATIVE = {"mp3", "aac", "vorbis", "wmav2"}
LOSSLESS_NATIVE = {"flac", "alac", "pcm_s16le", "pcm_s16be"}

MIME_BY_EXTENSION = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".wma": "audio/x-ms-wma",
}

BITS_BY_SAMPLE_FMT = {"u8": 8, "u8p": 8, "s16": 16, "s16p": 16, "s32": 32, "s32p": 32,
                      "flt": 32, "fltp": 32, "dbl": 64, "dblp": 64, "s64": 64, "s64p": 64}

CACHE_DIR = Path(os.environ.get("SONOS_CLI_CACHE", Path.home() / "Library/Caches/sonos-cli"))
CACHE_VERSION = "1"


@dataclass(frozen=True)
class AudioInfo:
    codec: str
    sample_rate: int
    channels: int
    bits: int | None
    duration: float | None
    title: str | None = None
    artist: str | None = None
    album: str | None = None


@dataclass(frozen=True)
class Track:
    path: Path
    info: AudioInfo

    @property
    def needs_conversion(self) -> bool:
        return needs_conversion(self.info, self.path.suffix.lower())

    @property
    def served_extension(self) -> str:
        return ".flac" if self.needs_conversion else self.path.suffix.lower()

    @property
    def mime(self) -> str:
        return MIME_BY_EXTENSION[self.served_extension]

    @property
    def title(self) -> str:
        return self.info.title or self.path.stem


def find_audio_files(paths: list[Path]) -> list[Path]:
    """Expand files and folders (recursively) into a sorted list of audio files."""
    found: list[Path] = []
    for path in paths:
        path = path.expanduser()
        if path.is_dir():
            found.extend(sorted(
                (p for p in path.rglob("*") if _is_audio_file(p)),
                key=lambda p: str(p.relative_to(path)).casefold(),
            ))
        elif path.is_file():
            if not _is_audio_file(path):
                raise ValueError(f"Not a supported audio file: {path}")
            found.append(path)
        else:
            raise FileNotFoundError(f"No such file or folder: {path}")
    return found


def _is_audio_file(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in AUDIO_EXTENSIONS
        and not any(part.startswith(".") for part in path.parts[-1:])
    )


def probe(path: Path) -> AudioInfo:
    """What a file is. Uses ffprobe when there is one (config, PATH, Homebrew); otherwise ffmpeg itself, which is what ships with
    the package, so nothing has to be installed."""
    ffprobe = config.find_binary("ffprobe", config.get().ffprobe)
    if ffprobe is None:
        return probe_with_ffmpeg(path)
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,sample_rate,channels,bits_per_sample,"
                          "bits_per_raw_sample,sample_fmt:stream_tags:format=duration:format_tags",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"ffprobe could not read {path}: {result.stderr.strip()}")
    return parse_probe(json.loads(result.stdout), path)


_LAYOUT_CHANNELS = {"mono": 1, "stereo": 2, "downmix": 2, "binaural": 2, "quad": 4, "hexagonal": 6, "octagonal": 8, "cube": 8, "hexadecagonal": 16}


def _channels(layout: str) -> int:
    """ffmpeg's channel layout words ("stereo", "quad(side)", "5.1(side)", "7.1.4", "3 channels") as a count.

    A layout this does not know is counted as more than stereo, so the file gets converted (down to two channels) rather than
    sent to the speaker as it is."""
    layout = layout.strip().lower()
    m = re.fullmatch(r"(\d+) channels?(?: \(.*\))?", layout)
    if m:
        return int(m.group(1))
    name = re.sub(r"\(.*\)$", "", layout)                 # "quad(side)", "5.1(side)": the suffix says where, not how many
    if name in _LAYOUT_CHANNELS:
        return _LAYOUT_CHANNELS[name]
    m = re.fullmatch(r"\d+(?:\.\d+)+", name)             # 5.1, 7.1.4, 9.1.6: every part is channels
    if m:
        return sum(int(part) for part in name.split("."))
    return MAX_CHANNELS + 1


_STREAM = re.compile(r"Stream #\d+:\d+[^:]*: Audio: (?P<codec>[\w-]+)[^,]*?(?: \([^)]*\))*, (?P<rate>\d+) Hz, (?P<layout>[^,]+?)(?:, (?P<fmt>[a-z0-9]+)(?: \((?P<bits>\d+) bit\))?)?(?:,|$)")


def parse_ffmpeg_info(text: str, path: Path | str = "?") -> dict:
    """The facts `ffmpeg -i` prints on stderr, in the shape parse_probe takes from ffprobe's JSON."""
    streams, fmt_tags, stream_tags, duration = [], {}, {}, None
    where = "format"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Input #"):
            where = "format"
        elif stripped.startswith("Duration:"):
            m = re.match(r"Duration: (\d+):(\d\d):(\d\d(?:\.\d+)?)", stripped)
            if m:
                duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        elif stripped.startswith("Chapter #"):
            where = "other"                                    # a chapter's title is not the track's
        elif stripped.startswith("Stream #"):
            m = _STREAM.search(stripped)
            where = "audio" if m and not streams else "other"
            if m and not streams:
                entry = {"codec_name": m.group("codec"), "sample_rate": m.group("rate"), "channels": _channels(m.group("layout")),
                         "sample_fmt": m.group("fmt") or ""}
                if m.group("bits"):
                    entry["bits_per_raw_sample"] = m.group("bits")
                streams.append(entry)
        elif stripped == "Metadata:":
            continue
        elif ":" in stripped and line.startswith("    ") and not stripped.startswith(("Chapter", "Side data")):
            key, _, value = stripped.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key in ("title", "artist", "album") and value:
                (fmt_tags if where == "format" else stream_tags if where == "audio" else {}).setdefault(key, value)
    if streams:
        streams[0]["tags"] = stream_tags
    return {"streams": streams, "format": {"duration": duration, "tags": fmt_tags}}


def probe_with_ffmpeg(path: Path) -> AudioInfo:
    result = subprocess.run([config.binary("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path)], capture_output=True, text=True, errors="replace")
    if "Input #0" not in result.stderr:                    # ffmpeg exits 1 here whatever happens (it was given no output): the text tells
        raise ValueError(f"ffmpeg could not read {path}: {result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'no output'}")
    return parse_probe(parse_ffmpeg_info(result.stderr, path), path)


def parse_probe(data: dict, path: Path | str = "?") -> AudioInfo:
    streams = data.get("streams") or []
    if not streams:
        raise ValueError(f"No audio stream in {path}")
    stream = streams[0]
    fmt = data.get("format") or {}
    tags = {k.lower(): v for k, v in (stream.get("tags") or {}).items()}
    tags.update({k.lower(): v for k, v in (fmt.get("tags") or {}).items()})
    return AudioInfo(
        codec=stream["codec_name"],
        sample_rate=int(stream.get("sample_rate") or 0),
        channels=int(stream.get("channels") or 0),
        bits=_bits(stream),
        duration=_float(fmt.get("duration")),
        title=tags.get("title") or None,
        artist=tags.get("artist") or None,
        album=tags.get("album") or None,
    )


def _bits(stream: dict) -> int | None:
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        value = _int(stream.get(key))
        if value:
            return value
    return BITS_BY_SAMPLE_FMT.get(stream.get("sample_fmt", ""))


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def needs_conversion(info: AudioInfo, extension: str) -> bool:
    if extension not in MIME_BY_EXTENSION:  # e.g. .opus
        return True
    if info.sample_rate > MAX_SAMPLE_RATE or info.channels > MAX_CHANNELS:
        return True
    if info.codec in LOSSY_NATIVE:
        return False
    if info.codec in LOSSLESS_NATIVE:
        return (info.bits or MAX_BITS) > MAX_BITS
    return True


def target_sample_rate(rate: int) -> int:
    if rate <= MAX_SAMPLE_RATE:
        return rate
    return 48_000 if rate % 48_000 == 0 else 44_100


def cached_conversion_path(track: Track) -> Path:
    stat = track.path.stat()
    key = f"{CACHE_VERSION}|{track.path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    return CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.flac"


def convert(track: Track) -> Path:
    """Convert to 16-bit FLAC Sonos S1 can play, reusing a cached copy if present."""
    out = cached_conversion_path(track)
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".partial.flac")
    rate = target_sample_rate(track.info.sample_rate)
    cmd = [config.binary("ffmpeg"), "-v", "error", "-nostdin", "-y", "-i", str(track.path),
           "-map", "0:a:0", "-map_metadata", "0",
           "-af", f"aresample=out_sample_rate={rate}:out_sample_fmt=s16:dither_method=triangular",
           "-c:a", "flac"]
    if track.info.channels > MAX_CHANNELS:
        cmd += ["-ac", str(MAX_CHANNELS)]
    result = subprocess.run([*cmd, str(tmp)], capture_output=True, text=True)
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed on {track.path}: {result.stderr.strip()}")
    tmp.rename(out)
    return out


def clear_cache() -> int:
    if not CACHE_DIR.exists():
        return 0
    removed = 0
    for f in CACHE_DIR.glob("*.flac"):
        removed += f.stat().st_size
        f.unlink()
    return removed
