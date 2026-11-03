"""Generate small real audio files with ffmpeg for integration tests."""

import subprocess
from pathlib import Path

from sonos_cli import config

CODECS = {".wav": {16: "pcm_s16le", 24: "pcm_s24le"}, ".flac": {16: "flac", 24: "flac"}}


def make_tone(path: Path, seconds: float = 1.0, rate: int = 44100, bits: int = 16, freq: int = 440,
              channels: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [config.binary("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
           f"sine=frequency={freq}:duration={seconds}:sample_rate={rate}", "-ac", str(channels)]
    if path.suffix == ".mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", "128k"]
    elif path.suffix == ".flac" and bits == 24:
        cmd += ["-c:a", "flac", "-sample_fmt", "s32"]
    else:
        cmd += ["-c:a", CODECS[path.suffix][bits]]
    subprocess.run(cmd + [str(path)], check=True)
    return path
