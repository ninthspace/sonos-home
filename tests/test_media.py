import subprocess
from pathlib import Path

import pytest

from sonos_cli import media
from sonos_cli.media import AudioInfo, Track, find_audio_files, needs_conversion, parse_probe, target_sample_rate
from sonos_cli.player import format_duration
from sonos_cli.server import _parse_range


def info(codec, rate=44_100, channels=2, bits=None):
    return AudioInfo(codec=codec, sample_rate=rate, channels=channels, bits=bits, duration=10.0)


@pytest.mark.parametrize("audio, ext, expected", [
    (info("mp3"), ".mp3", False),
    (info("aac"), ".m4a", False),
    (info("vorbis"), ".ogg", False),
    (info("pcm_s16le", bits=16), ".wav", False),
    (info("pcm_s16be", bits=16), ".aiff", False),
    (info("flac", bits=16), ".flac", False),
    (info("pcm_s24le", bits=24), ".wav", True),
    (info("pcm_f32le", bits=32), ".wav", True),
    (info("pcm_s24be", bits=24), ".aif", True),
    (info("flac", bits=24), ".flac", True),
    (info("alac", bits=24), ".m4a", True),
    (info("pcm_s16le", rate=96_000, bits=16), ".wav", True),
    (info("pcm_s16le", channels=6, bits=16), ".wav", True),
    (info("opus", rate=48_000), ".opus", True),
])
def test_needs_conversion(audio, ext, expected):
    assert needs_conversion(audio, ext) is expected


@pytest.mark.parametrize("rate, expected", [(44_100, 44_100), (48_000, 48_000), (88_200, 44_100),
                                            (96_000, 48_000), (192_000, 48_000), (176_400, 44_100)])
def test_target_sample_rate(rate, expected):
    assert target_sample_rate(rate) == expected


def test_parse_probe_reads_bits_and_tags_case_insensitively():
    data = {
        "streams": [{"codec_name": "flac", "sample_rate": "96000", "channels": 2,
                     "bits_per_raw_sample": "24", "sample_fmt": "s32", "tags": {"TITLE": "Stream"}}],
        "format": {"duration": "61.5", "tags": {"ARTIST": "Someone", "album": "Thing"}},
    }
    parsed = parse_probe(data)
    assert (parsed.codec, parsed.sample_rate, parsed.bits) == ("flac", 96_000, 24)
    assert (parsed.title, parsed.artist, parsed.album, parsed.duration) == ("Stream", "Someone", "Thing", 61.5)


def test_parse_probe_falls_back_to_sample_format():
    data = {"streams": [{"codec_name": "flac", "sample_rate": "44100", "channels": 2,
                         "bits_per_raw_sample": "N/A", "sample_fmt": "s16"}], "format": {}}
    assert parse_probe(data).bits == 16


def test_find_audio_files_recurses_sorts_and_skips_junk(tmp_path):
    for name in ["b/2.wav", "b/10.mp3", "a.flac", "notes.txt", "x.wav.asd", "._a.flac", ".hidden.mp3"]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"")
    found = [p.relative_to(tmp_path).as_posix() for p in find_audio_files([tmp_path])]
    assert found == ["a.flac", "b/10.mp3", "b/2.wav"]


def test_find_audio_files_rejects_missing_path(tmp_path):
    with pytest.raises(FileNotFoundError):
        find_audio_files([tmp_path / "nope"])


@pytest.mark.parametrize("header, expected", [
    ("bytes=0-99", (0, 99)), ("bytes=100-", (100, 999)), ("bytes=-100", (900, 999)),
    ("bytes=900-5000", (900, 999)), ("bytes=1000-", None), ("bytes=5-1", None), ("items=0-1", None),
])
def test_parse_range(header, expected):
    assert _parse_range(header, 1000) == expected


def test_format_duration():
    assert format_duration(3725.4) == "1:02:05"
    assert format_duration(None) is None


def test_convert_24bit_96k_wav_to_16bit_flac(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "CACHE_DIR", tmp_path / "cache")
    src = tmp_path / "tone.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=96000",
                    "-ac", "2", "-c:a", "pcm_s24le", str(src)], check=True)
    track = Track(src, media.probe(src))
    assert track.needs_conversion and track.mime == "audio/flac"

    out = media.convert(track)
    converted = media.probe(out)
    assert (converted.codec, converted.sample_rate, converted.bits) == ("flac", 48_000, 16)
    assert media.convert(track) == out  # second call hits the cache
