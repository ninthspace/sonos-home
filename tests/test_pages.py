"""Finding playable audio on a page (spec FR13)."""

from pathlib import Path

import pytest

from sonos_cli import pages
from sonos_cli.pages import NoAudio, absolute_url, extract

FIX = Path(__file__).parent / "fixtures"
RELEASE = "https://shtl-experiment.com/hello-ep/"


def test_a_faircamp_release_page_gives_one_mp3_per_track_in_order_with_its_title_and_no_opus():
    title, tracks = extract((FIX / "shtl-hello-ep.html").read_text(encoding="utf-8"), RELEASE)
    assert title == "Hello EP"
    assert [t.title for t in tracks] == ["Halyard", "Undertow"]
    assert [t.url for t in tracks] == [
        "https://shtl-experiment.com/hello-ep/1/mp3-v5/Ui6CBz2DJ_s/01%20Halyard.mp3",
        "https://shtl-experiment.com/hello-ep/2/mp3-v5/Y5THJcXnju0/02%20Undertow.mp3"]
    assert all(t.mime == "audio/mpeg" and t.protocol_info == "http-get:*:audio/mpeg:*" for t in tracks)
    assert not any("opus" in t.url for t in tracks)


def test_a_generic_page_gives_audio_then_links_in_document_order_absolute_encoded_and_without_opus():
    title, tracks = extract((FIX / "generic-page.html").read_text(encoding="utf-8"), "https://example.com/dir/page.html")
    assert title == "A page & its audio"
    assert [(t.url, t.title, t.mime) for t in tracks] == [
        ("https://example.com/dir/media/first%20one.mp3", "first one", "audio/mpeg"),
        ("https://example.com/abs/second.ogg", "second", "audio/ogg"),
        ("https://example.com/dir/downloads/Caf%C3%A9%20%E2%80%93%20%C3%9Cn%C3%AFcode%20%E6%97%A5%E6%9C%AC.flac", "The flac & friends", "audio/flac"),
        ("https://other.example/x/third.m4a?v=1&w=2", "Third", "audio/mp4"),
        ("https://cdn.example/dir/track%20four.aac", "four", "audio/aac"),
    ]
    assert not any(t.url.endswith((".opus", ".txt")) or t.url.startswith(("javascript", "data")) for t in tracks)
    assert len({t.url for t in tracks}) == len(tracks)                       # the duplicate link was dropped


def test_a_page_with_nothing_playable_raises_a_clear_error():
    for html in ("<html><body><p>hello</p></body></html>", '<audio><source src="x.opus" type="audio/ogg; codecs=opus"></audio>',
                 '<a href="song.opus">x</a><a href="y.wav">y</a>', ""):
        with pytest.raises(NoAudio, match="No audio Sonos can play"):
            extract(html, "https://x.test/")


@pytest.mark.parametrize("src, expected", [
    ("a b.mp3", "https://h.test/p/a%20b.mp3"), ("/root/é.mp3", "https://h.test/root/%C3%A9.mp3"),
    ("//cdn.test/x%20y.mp3", "https://cdn.test/x%20y.mp3"), ("../up.mp3", "https://h.test/up.mp3"),
    ("x.mp3#frag", "https://h.test/p/x.mp3"), ("x.mp3?a=1 2", "https://h.test/p/x.mp3?a=1%202"),
    ("ftp://h.test/x.mp3", None), ("file:///etc/passwd", None), ("javascript:alert(1)", None), ("data:audio/mp3;base64,AA", None),
    ("", None), ("   ", None), ("#top", None),
])
def test_urls_are_made_absolute_and_percent_encoded_and_only_http_survives(src, expected):
    assert absolute_url(src, "https://h.test/p/page.html") == expected


def test_html_entities_in_titles_and_unusual_markup_do_not_break_extraction():
    html = ('<div class="track"><a class="title" href="1/">Tom &amp; Jerry &lt;live&gt;</a>'
            '<audio><source src="t.mp3" type="audio/mpeg"></div><div class="track"><a class="title">Second</a>'
            '<audio src="u.flac"></audio></div><div class="track"><a class="title">No audio here</a></div>')
    _, tracks = extract(html, "http://h.test/r/")
    assert [(t.title, t.mime) for t in tracks] == [("Tom & Jerry <live>", "audio/mpeg"), ("Second", "audio/flac")]


def test_an_audio_element_outside_a_track_div_does_not_steal_the_next_tracks_title():
    html = '<audio src="intro.mp3"></audio><div class="track"><a class="title">Real</a><audio src="r.mp3"></audio></div>'
    _, tracks = extract(html, "http://h.test/")
    assert [t.title for t in tracks] == ["Real"]            # Faircamp's own tracks win; the stray audio isn't mixed in


def test_the_mp3_is_preferred_over_other_playable_alternatives_of_one_track():
    for html in ('<audio><source src="a.flac"><source src="a.ogg" type="audio/ogg; codecs=vorbis"><source src="a.mp3" type="audio/mpeg"></audio>',
                 '<audio><source src="a.mp3" type="audio/mpeg"><source src="a.flac"><source src="a.ogg" type="audio/ogg; codecs=vorbis"></audio>'):
        assert [t.url for t in extract(html, "http://h.test/")[1]] == ["http://h.test/a.mp3"]
    assert [t.url for t in extract('<audio><source src="a.ogg" type="audio/ogg; codecs=vorbis"><source src="a.flac"></audio>', "http://h.test/")[1]] == ["http://h.test/a.flac"]


def test_a_huge_page_and_an_enormous_track_list_are_capped():
    with pytest.raises(NoAudio, match="too large"):
        extract("<!-- -->" + "x" * (pages.MAX_HTML_BYTES + 1), "http://h.test/")
    html = "".join(f'<a href="t{i}.mp3">t{i}</a>' for i in range(700))
    assert len(extract(html, "http://h.test/")[1]) == 500


def test_ogg_declared_as_opus_is_skipped_whatever_its_file_extension_and_vorbis_is_kept():
    with pytest.raises(NoAudio):
        extract('<audio><source src="a.ogg" type="audio/ogg; codecs=opus"></audio>', "http://h.test/")
    _, tracks = extract('<audio><source src="a.ogg" type="audio/ogg; codecs=opus"><source src="b.ogg" type="audio/ogg; codecs=vorbis"></audio>', "http://h.test/")
    assert [(t.url, t.mime) for t in tracks] == [("http://h.test/b.ogg", "audio/ogg")]


def test_a_track_title_is_used_once_not_repeated_on_a_second_audio_in_the_same_track():
    html = '<div class="track"><a class="title">Title</a><audio src="a.mp3"></audio><audio src="b-side.mp3"></audio></div>'
    _, tracks = extract(html, "http://h.test/")
    assert [t.title for t in tracks] == ["Title", "b-side"]


# ---- the release's artist --------------------------------------------------------------------------------------

def test_every_track_of_a_faircamp_release_carries_the_releases_artist():
    _, tracks = extract((FIX / "shtl-hello-ep.html").read_text(encoding="utf-8"), RELEASE)
    assert [t.artist for t in tracks] == ["Minim", "Minim"]


def _release(artists_html):
    return (f'<html><head><title>R</title></head><body><div class="abstract"><h1>R</h1>{artists_html}</div>'
            '<div class="tracks"><div class="track"><a class="title" href="1/">One</a><audio><source type="audio/mpeg" src="1/a.mp3"></audio></div></div></body></html>')


@pytest.mark.parametrize("markup,artist", [
    ('<div class="release_artists"><a href="../a/">SHTL</a></div>', "SHTL"),
    ('<div class="release_artists"><a href="../a/">Ann</a>, <a href="../b/">Bo &amp; Co</a></div>', "Ann, Bo & Co"),
    ('<div class="release_artists">\n  <span><a href="../a/"> Spaced\n out </a></span>\n</div>', "Spaced out"),
    ('<div class="release_artists"></div>', ""),
    ('', ""),
])
def test_the_release_artist_is_the_text_of_the_release_artists_div_and_empty_when_there_is_none(markup, artist):
    _, tracks = extract(_release(markup), RELEASE)
    assert [t.artist for t in tracks] == [artist]


def test_text_after_the_release_artists_div_is_not_part_of_the_artist():
    _, tracks = extract(_release('<div class="release_artists"><div>Inner</div><a>X</a></div><p>Not me</p>'), RELEASE)
    assert tracks[0].artist == "InnerX"


def test_pages_that_are_not_faircamp_releases_get_no_artist():
    html = '<div class="release_artists">Someone</div><audio src="/a.mp3"></audio><a href="b.flac">b</a>'
    _, tracks = extract(html, "https://example.com/")
    assert [t.artist for t in tracks] == ["", ""]


def test_a_very_long_release_artist_is_cut_to_the_cap_so_it_cannot_be_repeated_in_every_track_in_full():
    _, tracks = extract(_release(f'<div class="release_artists"><a>{"A" * 5000}</a></div>'), RELEASE)
    assert tracks[0].artist == "A" * pages.MAX_ARTIST and pages.MAX_ARTIST == 200
