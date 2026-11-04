"""Fetching remote pages and audio on behalf of the speakers (FR13, AD5).

S1 players can't fetch the HTTPS URLs most sites use, so a track from a web page is queued as
`http://<mac>:<port>/remote/<token>` and this module fetches the real URL when the speaker asks. The token is the URL plus an
HMAC of it under a per-install secret, so the service only ever fetches URLs it signed itself. `/api/send` is reachable from
the LAN without a login, so every fetch (the page and each audio file) also refuses anything that isn't a public internet
address: loopback, private ranges, link-local and so on, checked for the address actually connected to and again on every
redirect.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import socket
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import anyio
import httpx

MAX_REDIRECTS = 5
PAGE_LIMIT = 5_000_000
PAGE_DEADLINE = 30.0        # seconds for a whole page fetch, however slowly the origin trickles it
USER_AGENT = "sonos-cli (personal Sonos player)"


class RemoteError(Exception):
    status = 502


class Forbidden(RemoteError):
    status = 403


def load_secret(path: Path) -> bytes:
    """The per-install signing secret; created (0600) on first use and stable across restarts."""
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    data = secrets.token_bytes(32)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return data


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(url: str, secret: bytes) -> str:
    return f"{_b64(url.encode())}.{_b64(hmac.new(secret, url.encode(), hashlib.sha256).digest())}"


def verify(token: str, secret: bytes) -> str:
    try:
        raw, mac = token.split(".", 1)
        url = _unb64(raw).decode()
        good = hmac.compare_digest(_unb64(mac), hmac.new(secret, url.encode(), hashlib.sha256).digest())
    except (ValueError, UnicodeDecodeError):
        raise Forbidden("bad token") from None
    if not good:
        raise Forbidden("bad token")
    return url


_EXT = {"audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/flac": ".flac", "audio/ogg": ".ogg"}


NAME_CAP = 240                       # the longest proxy URL file name, encoded


def display_name(title: str, mime: str) -> str:
    """A percent-encoded file name for a track's proxy URL. Sonos titles a track from the last part of its URL.

    Capped at 240 encoded characters (the queue entry carries the whole URL, and it is sent to the speaker twice)."""
    ext = _EXT.get(mime, "")
    stem = "".join(c if (c.isprintable() or c == "\u200d") and c not in "/\\?#%" else " " for c in title)
    stem = " ".join(stem.split())[:240]                          # every character encodes to at least one character: nothing longer can fit
    if stem.lower() == ext or not stem:
        stem = "track"
    while stem and len(quote(stem + ext, safe="")) > NAME_CAP:
        stem = stem[:max(0, len(stem) - max(1, (len(quote(stem + ext, safe="")) - NAME_CAP) // 12))]       # at most 12 encoded characters per character
    return quote((stem.rstrip() or "track") + ext, safe="")


def track_label(uri: str, title: str, artist: str) -> tuple[str, str]:
    """The (title, artist) to show for a queue item or the playing track (docs/quick/08).

    The service names a page track's proxy URL `Title by Artist.ext` (`display_name`; `Title.ext` when the page names no
    artist). An S1 speaker titles a track it hasn't read tags for by that file name, with no artist; once it has read the tags
    it reports their title and, when the file has a track-artist tag, their artist. Many releases tag only the album artist, so
    the artist then comes back empty. So, for one of our /remote/ URLs: the speaker's artist always wins; with none, an unread
    track's name is split at its one " by " (not at none or several: nothing is guessed), and a read track whose title starts
    the name, followed by " by ", gets the rest as its artist. Anything else is left as the speaker reported it."""
    parts = urlsplit(uri).path.split("/")
    if len(parts) != 4 or parts[1] != "remote" or not parts[3]:
        return title, artist
    name = unquote(parts[3])
    stem = name
    for ext in _EXT.values():
        if name.lower().endswith(ext) and len(name) > len(ext):
            stem = name[:-len(ext)]
            break
    unread = not title or title == name
    if artist:
        return (stem if unread else title), artist
    if len(parts[3]) > NAME_CAP - 15:
        # `display_name` may have cut this name at its cap, which can cut into the artist: a fragment must never pass for the
        # whole artist, so a name this long gives none. A cut name is at least 226 encoded characters: each trim stops within
        # one character (12 encoded at most) of the cap, and the one trailing space it may leave (3) is then stripped.
        return (stem if unread else title), ""
    if unread:
        pieces = stem.split(" by ")
        if len(pieces) == 2 and pieces[0].strip() and pieces[1].strip():
            return pieces[0], pieces[1]
        return stem, ""
    if stem.startswith(title + " by ") and stem[len(title) + 4:].strip():
        return title, stem[len(title) + 4:]
    return title, ""


def is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def default_resolve(host: str, port: int) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


class Fetcher:
    """Public-internet-only HTTP: resolves a name, checks every address, connects to the checked address."""

    def __init__(self, resolve: Callable[[str, int], Iterable[str]] = default_resolve,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 15.0):
        self.resolve = resolve
        self.transport = transport
        self.timeout = timeout

    async def _pin(self, url: str) -> tuple[str, str, str]:
        """(url to connect to, Host header, host name) with the host replaced by a checked public IP."""
        try:
            parts = urlsplit(url)
            explicit_port = parts.port
        except ValueError:
            raise Forbidden("that isn't a valid address") from None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise Forbidden("only http and https addresses can be fetched")
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in parts.netloc):
            raise Forbidden("that isn't a valid address")
        try:
            host = parts.hostname.encode("idna").decode("ascii")        # the xn-- form; refuses empty and over-long labels
        except UnicodeError:
            raise Forbidden("that isn't a valid address") from None
        port = explicit_port or (443 if parts.scheme == "https" else 80)
        try:
            # getaddrinfo blocks, and a dead network can make it take many seconds: never on the event loop
            addresses = await anyio.to_thread.run_sync(lambda: list(self.resolve(host, port)))
        except OSError:
            raise RemoteError(f"couldn't find {host}: check the address") from None
        if not addresses or not all(is_public(a) for a in addresses):
            raise Forbidden(f"{host} isn't a public internet address")
        ip = addresses[0]
        host_for_url = f"[{ip}]" if ":" in ip else ip
        netloc = f"{host_for_url}:{port}" if explicit_port else host_for_url
        host_header = (f"[{host}]" if ":" in host else host) + (f":{explicit_port}" if explicit_port else "")     # ASCII, no credentials
        return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, "")), host_header, host

    async def open(self, method: str, url: str, headers: dict[str, str] | None = None):
        """Send the request, following redirects by hand and checking each hop.

        Returns (client, response, final url) with the response still streaming. Asks for identity encoding: the bytes
        are passed on to a speaker as they are, so they must not arrive compressed."""
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity", **(headers or {})}
        for _ in range(MAX_REDIRECTS + 1):
            target, host_header, hostname = await self._pin(url)
            client = httpx.AsyncClient(transport=self.transport, timeout=self.timeout, follow_redirects=False, verify=True)
            try:
                req = client.build_request(method, target, headers={**headers, "Host": host_header},
                                           extensions={"sni_hostname": hostname})
                resp = await client.send(req, stream=True)
            except (UnicodeError, ValueError):
                await client.aclose()
                raise Forbidden("that isn't a valid address") from None
            except httpx.HTTPError as e:
                await client.aclose()
                raise RemoteError(f"couldn't fetch {hostname}: {e.__class__.__name__}") from None
            if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                url = urljoin(url, resp.headers["location"])
                await resp.aclose()
                await client.aclose()
                continue
            return client, resp, url
        raise RemoteError("too many redirects")

    async def get_text(self, url: str, limit: int = PAGE_LIMIT) -> tuple[str, str]:
        """The page as text plus the address it finally came from (after redirects), for resolving relative links."""
        try:
            with anyio.fail_after(PAGE_DEADLINE):
                return await self._get_text(url, limit)
        except TimeoutError:
            raise RemoteError("the page took too long to arrive") from None

    async def _get_text(self, url: str, limit: int) -> tuple[str, str]:
        client, resp, final_url = await self.open("GET", url)
        try:
            if resp.status_code != 200:
                raise RemoteError(f"the page answered {resp.status_code}")
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    raise RemoteError("the page is too large")
            encoding = resp.encoding or "utf-8"
            return body.decode(encoding, errors="replace"), final_url
        finally:
            await resp.aclose()
            await client.aclose()
