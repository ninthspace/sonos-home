"""A small HTTP server the Sonos player fetches tracks from.

Only registered tracks are served, each under a random token, so nothing
else on the Mac is reachable. Range requests are supported because Sonos
uses them when seeking.
"""

from __future__ import annotations

import re
import secrets
import socket
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

from .media import Track, convert

CHUNK = 256 * 1024


class TrackServer:
    def __init__(self, host_ip: str, port: int = 0, verbose: bool = False):
        self._tracks: dict[str, tuple[Track, Future[Path]]] = {}
        self._converter = ThreadPoolExecutor(max_workers=2, thread_name_prefix="convert")
        self.verbose = verbose
        handler = type("Handler", (_Handler,), {"owner": self})
        self._httpd = ThreadingHTTPServer((host_ip, port), handler)
        self._httpd.daemon_threads = True
        self.base_url = f"http://{host_ip}:{self._httpd.server_address[1]}/t/"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._converter.shutdown(wait=False, cancel_futures=True)

    def add(self, track: Track) -> str:
        """Register a track (starting conversion if needed) and return its URL."""
        token = secrets.token_urlsafe(12)
        if track.needs_conversion:
            future = self._converter.submit(convert, track)
        else:
            future = Future()
            future.set_result(track.path)
        self._tracks[token] = (track, future)
        name = quote(track.path.stem + track.served_extension)
        return f"{self.base_url}{token}/{name}"

    def ready(self, url: str, timeout: float | None = None) -> Path:
        """Block until the file behind `url` is ready to serve."""
        token = url.removeprefix(self.base_url).split("/", 1)[0]
        return self._tracks[token][1].result(timeout)

    def lookup(self, token: str) -> tuple[Track, Future[Path]] | None:
        return self._tracks.get(token)


class _Handler(BaseHTTPRequestHandler):
    owner: TrackServer
    protocol_version = "HTTP/1.1"

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def do_GET(self) -> None:
        self._serve(send_body=True)

    def _serve(self, send_body: bool) -> None:
        match = re.fullmatch(r"/t/([A-Za-z0-9_-]+)/[^/]*", unquote(self.path).split("?")[0])
        entry = match and self.owner.lookup(match.group(1))
        if not entry:
            self.send_error(404)
            return
        track, future = entry
        try:
            path = future.result()  # waits if the track is still being converted
        except Exception as exc:  # noqa: BLE001 - report conversion failures to the client
            print(f"! {exc}", file=sys.stderr)
            self.send_error(500)
            return

        size = path.stat().st_size
        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header:
            parsed = _parse_range(range_header, size)
            if parsed is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = parsed
            status = 206

        self.send_response(status)
        self.send_header("Content-Type", track.mime)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not send_body:
            return
        try:
            with path.open("rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Sonos routinely drops connections when skipping or seeking

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        if self.owner.verbose:
            sys.stderr.write(f"[http] {self.client_address[0]} {format % args}\n")


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or not any(match.groups()):
        return None
    first, last = match.groups()
    if first == "":
        start, end = max(0, size - int(last)), size - 1
    else:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    if start > end or start >= size:
        return None
    return start, end


def local_ip_towards(remote_ip: str) -> str:
    """The Mac's address on the network the Sonos player is on."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((remote_ip, 1400))
        return s.getsockname()[0]
