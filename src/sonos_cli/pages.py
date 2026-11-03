"""Find the audio a web page offers, in a form a Sonos S1 can play (FR13).

Faircamp release pages come first: one entry per track, the MP3 source with the track's title and the release's artist, in order. Any other
page yields its `<audio>`/`<source>` URLs and its links to .mp3/.m4a/.aac/.flac/.ogg. Opus and anything else S1 can't
play is skipped. URLs are made absolute and percent-encoded.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

MAX_HTML_BYTES = 5_000_000
MAX_TRACKS = 500
MAX_ARTIST = 200

MIME_BY_EXT = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg"}
# In the order we'd rather play a page's alternatives.
PREFERENCE = ["audio/mpeg", "audio/mp4", "audio/aac", "audio/flac", "audio/ogg"]
URL_SAFE = "/%:@!$&'()*+,;=-._~"


class NoAudio(ValueError):
    """The page has nothing a Sonos can play."""


@dataclass(frozen=True)
class PageTrack:
    url: str
    title: str
    mime: str
    artist: str = ""                                        # Faircamp's release artist(s); empty when the page names none

    @property
    def protocol_info(self) -> str:
        return f"http-get:*:{self.mime}:*"


def absolute_url(src: str, base_url: str) -> str | None:
    """`src` made absolute against the page and percent-encoded; None for anything that isn't http(s)."""
    src = (src or "").strip()
    if not src or src.startswith(("data:", "javascript:", "blob:", "#")):
        return None
    try:
        parts = urlsplit(urljoin(base_url, src))
        parts.port                                          # raises ValueError for 99999, abc, and the like
    except ValueError:                                      # a malformed link on a page we don't control
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return urlunsplit((parts.scheme, parts.netloc, quote(parts.path, safe=URL_SAFE), quote(parts.query, safe=URL_SAFE + "?"), ""))


def _mime_for(url: str, declared: str = "") -> str | None:
    """The playable MIME type for a source, or None (Opus, WebM, anything Sonos S1 can't play)."""
    declared = (declared or "").lower().strip()
    ext = posixpath.splitext(unquote(urlsplit(url).path))[1].lower()
    if "opus" in declared or ext == ".opus":
        return None
    if declared:
        base = declared.split(";")[0].strip()
        if base in ("audio/mpeg", "audio/mp3"):
            return "audio/mpeg"
        if base in ("audio/mp4", "audio/x-m4a", "audio/m4a"):
            return "audio/mp4"
        if base in ("audio/aac", "audio/flac", "audio/x-flac"):
            return "audio/flac" if "flac" in base else "audio/aac"
        if base == "audio/ogg" and ("vorbis" in declared or ext == ".ogg"):
            return "audio/ogg"
        if base in ("audio/ogg", "application/ogg"):
            return "audio/ogg" if ext in (".ogg", ".oga", "") and "opus" not in declared else None
    return MIME_BY_EXT.get(ext)


def _title_from(url: str) -> str:
    return posixpath.splitext(posixpath.basename(unquote(urlsplit(url).path)))[0] or "Track"


class _Collector(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.page_title = ""
        self._in_title = False
        self._in_audio = False
        self._audio_sources: list[tuple[str, str]] = []     # (url, mime)
        self._pending_title = ""
        self._capturing_title = False
        self._link: dict | None = None
        self.faircamp: list[PageTrack] = []
        self.audio: list[PageTrack] = []
        self.links: list[PageTrack] = []
        self._seen_faircamp_track = False
        self._track_depth: list[int] = []
        self._depth = 0
        self._base_set = False
        self.release_artist = ""
        self._artist_depth = 0                              # the div depth of an open <div class="release_artists">, else 0
        self._artist_text = ""

    # ---- events -------------------------------------------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        classes = a.get("class", "").split()
        if tag == "title" and not self.page_title:
            self._in_title = True
        if tag == "base" and a.get("href") and not self._base_set:
            self._base_set = True                           # the first <base href> decides where relative links point
            self.base = absolute_url(a["href"], self.base) or self.base
        if tag == "div":
            self._depth += 1
            if "release_artists" in classes and not self.release_artist and not self._artist_depth:
                self._artist_depth, self._artist_text = self._depth, ""
            if "track" in classes:
                self._track_depth.append(self._depth)
                self._seen_faircamp_track = True
                self._pending_title = ""
        if tag == "a" and "title" in classes and self._track_depth:
            self._capturing_title = True
            self._pending_title = ""
        if tag == "a" and a.get("href"):
            self._link = {"href": a["href"], "text": ""}
        if tag == "audio":
            if self._in_audio:                                  # an <audio> that was never closed
                self._in_audio = False
                self._finish_audio()
            self._in_audio = True
            self._audio_sources = []
            if a.get("src"):
                self._add_source(a["src"], a.get("type", ""))
        if tag == "source" and self._in_audio and a.get("src"):
            self._add_source(a["src"], a.get("type", ""))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "div":
            if self._in_audio and self._track_depth and self._track_depth[-1] == self._depth:
                self._in_audio = False                          # the track ended before its <audio> was closed
                self._finish_audio()
            if self._track_depth and self._track_depth[-1] == self._depth:
                self._track_depth.pop()
            if self._artist_depth and self._artist_depth == self._depth:
                self.release_artist = " ".join(self._artist_text.split())[:MAX_ARTIST]
                self._artist_depth = 0
            self._depth -= 1
        if tag == "a":
            self._capturing_title = False
            if self._link is not None:
                self._finish_link()
        if tag == "audio" and self._in_audio:
            self._in_audio = False
            self._finish_audio()

    def handle_data(self, data):
        if self._in_title:
            self.page_title += data
        if self._artist_depth:
            self._artist_text += data
        if self._capturing_title:
            self._pending_title += data
        if self._link is not None:
            self._link["text"] += data

    # ---- helpers --------------------------------------------------------------------------------------------
    def _add_source(self, src: str, declared: str) -> None:
        url = absolute_url(src, self.base)
        mime = _mime_for(url, declared) if url else None
        if url and mime:
            self._audio_sources.append((url, mime))

    def flush(self) -> None:
        if self._in_audio:
            self._in_audio = False
            self._finish_audio()

    def _finish_audio(self) -> None:
        if not self._audio_sources:
            return
        best = min(self._audio_sources, key=lambda s: PREFERENCE.index(s[1]) if s[1] in PREFERENCE else 99)
        title = self._pending_title.strip() or _title_from(best[0])
        track = PageTrack(best[0], title, best[1])
        (self.faircamp if self._track_depth else self.audio).append(track)
        self._pending_title = ""

    def _finish_link(self) -> None:
        link, self._link = self._link, None
        url = absolute_url(link["href"], self.base)
        mime = _mime_for(url) if url else None
        if url and mime:
            self.links.append(PageTrack(url, " ".join(link["text"].split()) or _title_from(url), mime))


def extract(html: str, base_url: str) -> tuple[str, list[PageTrack]]:
    """The page's title and its playable tracks, in order (at most MAX_TRACKS). Raises NoAudio when there are none."""
    if len(html.encode("utf-8", "ignore")) > MAX_HTML_BYTES:
        raise NoAudio("The page is too large to read")
    c = _Collector(base_url)
    c.feed(html)
    c.close()
    c.flush()
    if c.faircamp and c.release_artist:
        tracks = [replace(t, artist=c.release_artist) for t in c.faircamp]
    else:
        tracks = c.faircamp or _dedupe(c.audio + c.links)
    if not tracks:
        raise NoAudio("No audio Sonos can play was found on that page (it needs MP3, AAC, FLAC or Ogg Vorbis links)")
    return " ".join(c.page_title.split()), tracks[:MAX_TRACKS]


def _dedupe(tracks: list[PageTrack]) -> list[PageTrack]:
    seen, out = set(), []
    for t in tracks:
        if t.url not in seen:
            seen.add(t.url)
            out.append(t)
    return out
