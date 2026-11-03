import shutil
import subprocess
from pathlib import Path

import pytest

from audio import make_tone                  # conftest puts tests/ on the path
from sonos_cli import config, media
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
    subprocess.run([config.binary("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=96000",
                    "-ac", "2", "-c:a", "pcm_s24le", str(src)], check=True)
    track = Track(src, media.probe(src))
    assert track.needs_conversion and track.mime == "audio/flac"

    out = media.convert(track)
    converted = media.probe(out)
    assert (converted.codec, converted.sample_rate, converted.bits) == ("flac", 48_000, 16)
    assert media.convert(track) == out  # second call hits the cache


# ---- probing with ffmpeg alone (the package bundles ffmpeg but not ffprobe) -------------------------------


FFMPEG_TEXT_24BIT_WAV = """\
Input #0, wav, from 'tone.wav':
  Metadata:
    title           : Night Drive
    artist          : GLDR
    encoder         : Lavf61.7.100
  Duration: 01:01:02.50, bitrate: 4608 kb/s
  Stream #0:0: Audio: pcm_s24le ([1][0][0][0] / 0x0001), 96000 Hz, stereo, s32 (24 bit), 4608 kb/s
At least one output file must be specified
"""

FFMPEG_TEXT_MP3_WITH_COVER = """\
Input #0, mp3, from 'a.mp3':
  Metadata:
    album           : Hello EP
  Duration: 00:03:21.07, start: 0.025057, bitrate: 320 kb/s
  Stream #0:0: Audio: mp3 (mp3float), 44100 Hz, stereo, fltp, 320 kb/s
      Metadata:
        artist          : GLDR
  Stream #0:1: Video: mjpeg (Baseline), yuvj420p(pc, 600x600), 90k tbr
      Metadata:
        title           : cover
At least one output file must be specified
"""


def test_parse_ffmpeg_info_reads_a_24_bit_96k_wav_with_its_tags_and_duration():
    parsed = media.parse_probe(media.parse_ffmpeg_info(FFMPEG_TEXT_24BIT_WAV, "tone.wav"), "tone.wav")
    assert (parsed.codec, parsed.sample_rate, parsed.channels, parsed.bits) == ("pcm_s24le", 96_000, 2, 24)
    assert (parsed.title, parsed.artist, parsed.duration) == ("Night Drive", "GLDR", 3662.5)
    assert media.Track(Path("tone.wav"), parsed).needs_conversion


def test_parse_ffmpeg_info_takes_the_first_audio_stream_and_ignores_a_cover_images_tags():
    parsed = media.parse_probe(media.parse_ffmpeg_info(FFMPEG_TEXT_MP3_WITH_COVER, "a.mp3"), "a.mp3")
    assert (parsed.codec, parsed.sample_rate, parsed.channels, parsed.bits) == ("mp3", 44_100, 2, 32)
    assert (parsed.album, parsed.duration) == ("Hello EP", pytest.approx(201.07))
    assert parsed.artist == "GLDR"               # the audio stream's own tag is kept
    assert parsed.title is None                  # the cover image's "title : cover" is not the track's title


@pytest.mark.parametrize("layout, count", [
    ("mono", 1), ("stereo", 2), ("5.1", 6), ("5.1(side)", 6), ("7.1", 8), ("quad", 4), ("3 channels", 3), ("2 channels (FL+FR)", 2),
    ("quad(side)", 4), ("cube", 8), ("hexadecagonal", 16), ("binaural", 2), ("downmix", 2), ("3.1.2", 6), ("5.1.2", 8), ("5.1.4", 10),
    ("7.1.2", 10), ("7.1.4", 12), ("7.2.3", 12), ("9.1.4", 14), ("9.1.6", 16), ("5.0(side)", 5), ("6.1(back)", 7),
])
def test_ffmpeg_channel_layout_words_become_counts(layout, count):
    assert media._channels(layout) == count


def test_a_layout_this_does_not_know_is_converted_not_sent_as_it_is():
    count = media._channels("some-future-layout")
    assert count > media.MAX_CHANNELS                                    # so needs_conversion is True and ffmpeg mixes it down to two
    assert media.needs_conversion(info("flac", channels=count, bits=16), ".flac")


def test_a_four_channel_flac_is_converted_when_probed_with_ffmpeg_alone(tmp_path, without_ffprobe):
    f = tmp_path / "quad.flac"
    subprocess.run([config.binary("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=44100",
                    "-af", "pan=quad(side)|c0=c0|c1=c0|c2=c0|c3=c0", "-c:a", "flac", str(f)], check=True)
    info_ = media.probe(f)
    assert info_.channels == 4 and media.Track(f, info_).needs_conversion


FFMPEG_TEXT_WITH_CHAPTERS = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'book.m4a':
  Metadata:
    album           : The Book
  Duration: 00:10:00.00, start: 0.000000, bitrate: 64 kb/s
  Chapters:
    Chapter #0:0: start 0.000000, end 300.000000
      Metadata:
        title           : Chapter One
    Chapter #0:1: start 300.000000, end 600.000000
      Metadata:
        title           : Chapter Two
  Stream #0:0[0x1](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo, fltp, 64 kb/s (default)
      Metadata:
        handler_name    : SoundHandler
"""


def test_a_chapters_title_is_not_the_tracks_title():
    parsed = media.parse_probe(media.parse_ffmpeg_info(FFMPEG_TEXT_WITH_CHAPTERS, "book.m4a"), "book.m4a")
    assert parsed.title is None and parsed.album == "The Book"
    assert (parsed.codec, parsed.sample_rate, parsed.channels) == ("aac", 44_100, 2)


def test_a_chapters_title_is_not_the_tracks_title_on_a_real_file(tmp_path, without_ffprobe):
    src = make_tone(tmp_path / "t.wav")
    meta = tmp_path / "chapters.txt"
    meta.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=500\ntitle=Chapter One\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=500\nEND=1000\ntitle=Chapter Two\n")
    book = tmp_path / "book.m4a"
    subprocess.run([config.binary("ffmpeg"), "-v", "error", "-i", str(src), "-i", str(meta), "-map_metadata", "1", "-map_chapters", "1",
                    "-c:a", "aac", str(book)], check=True)
    assert "Chapter #" in subprocess.run([config.binary("ffmpeg"), "-hide_banner", "-i", str(book)], capture_output=True, text=True).stderr   # control: the file has chapters
    assert media.probe(book).title is None


def test_a_file_with_no_audio_stream_is_a_value_error_not_a_crash():
    text = "Input #0, png_pipe, from 'x.png':\n  Duration: N/A\n  Stream #0:0: Video: png, rgb24(pc), 8x8\n"
    with pytest.raises(ValueError, match="No audio stream"):
        media.parse_probe(media.parse_ffmpeg_info(text, "x.png"), "x.png")


@pytest.fixture
def without_ffprobe(monkeypatch):
    """A machine with no ffprobe anywhere: probe() must use ffmpeg alone."""
    real = config.find_binary
    monkeypatch.setattr(config, "find_binary", lambda name, configured=None, *a, **k: None if name == "ffprobe" else real(name, configured, *a, **k))


@pytest.mark.parametrize("name, kw, expected", [
    ("a.wav", dict(rate=96_000, bits=24), ("pcm_s24le", 96_000, 2, 24)),
    ("b.wav", dict(rate=44_100, bits=16), ("pcm_s16le", 44_100, 2, 16)),
    ("c.flac", dict(rate=48_000, bits=16), ("flac", 48_000, 2, 16)),
    ("d.flac", dict(rate=96_000, bits=24), ("flac", 96_000, 2, 24)),
    ("e.mp3", dict(rate=44_100), ("mp3", 44_100, 2, None)),
    ("f.wav", dict(rate=44_100, bits=16, channels=1), ("pcm_s16le", 44_100, 1, 16)),
])
def test_probe_with_ffmpeg_alone_reads_real_files(tmp_path, without_ffprobe, name, kw, expected):
    info = media.probe(make_tone(tmp_path / name, **kw))
    got = (info.codec, info.sample_rate, info.channels, info.bits)
    assert got[:3] == expected[:3]
    assert info.duration == pytest.approx(1.0, abs=0.1)
    if expected[3] is not None:
        assert got[3] == expected[3]


def test_probe_with_ffmpeg_agrees_with_ffprobe_on_the_same_files(tmp_path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        pytest.skip("no ffprobe on this machine to compare against")
    cfg = config.get()
    config._cached = config.Config(music_folders=cfg.music_folders, ffprobe=ffprobe, path=cfg.path)
    try:
        for name, kw in [("a.wav", dict(rate=96_000, bits=24)), ("b.flac", dict(rate=44_100, bits=16)), ("c.mp3", dict(rate=44_100)), ("d.wav", dict(channels=1))]:
            f = make_tone(tmp_path / name, **kw)
            via_ffprobe, via_ffmpeg = media.probe(f), media.probe_with_ffmpeg(f)
            assert (via_ffmpeg.codec, via_ffmpeg.sample_rate, via_ffmpeg.channels) == (via_ffprobe.codec, via_ffprobe.sample_rate, via_ffprobe.channels), name
            assert media.Track(f, via_ffmpeg).needs_conversion == media.Track(f, via_ffprobe).needs_conversion, name
            assert via_ffmpeg.bits == via_ffprobe.bits, name
            assert via_ffmpeg.duration == pytest.approx(via_ffprobe.duration, abs=0.05), name
    finally:
        config._cached = None


def test_probe_with_ffmpeg_says_what_is_wrong_with_a_file_that_is_not_audio(tmp_path, without_ffprobe):
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"this is not audio at all" * 50)
    with pytest.raises(ValueError, match="could not read"):
        media.probe(junk)
    with pytest.raises(ValueError, match="could not read"):
        media.probe(tmp_path / "missing.wav")


def test_probe_with_ffmpeg_survives_a_path_that_looks_like_an_option_or_has_odd_characters(tmp_path, without_ffprobe):
    odd = make_tone(tmp_path / "-odd name ünï (1).wav")
    assert media.probe(odd).sample_rate == 44_100


def test_the_bundled_ffmpeg_alone_probes_and_converts_a_24bit_96k_wav(tmp_path, monkeypatch, without_ffprobe):
    """The manual check in epic 01-09 Story 2, as a test: the managed ffmpeg only (nothing on PATH, no Homebrew folders, no ffprobe)."""
    bundled = config.managed_binary("ffmpeg")
    assert bundled
    src = make_tone(tmp_path / "tone.wav", rate=96_000, bits=24)                 # made with whatever ffmpeg the machine has: only the input matters
    monkeypatch.setenv("PATH", "/nonexistent")                                 # the bundled copy is found first, so Homebrew's is never consulted
    monkeypatch.setattr(media, "CACHE_DIR", tmp_path / "cache")
    track = media.Track(src, media.probe(src))
    assert track.needs_conversion
    out = media.convert(track)
    converted = media.probe(out)
    assert (converted.codec, converted.sample_rate, converted.bits) == ("flac", 48_000, 16)
    decode = subprocess.run([bundled, "-v", "error", "-i", str(out), "-f", "null", "-"], capture_output=True, text=True)
    assert decode.returncode == 0 and decode.stderr == ""
