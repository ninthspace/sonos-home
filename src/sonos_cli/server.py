"""Helpers shared by the service and the CLI's in-process fallback.

`EmbeddedServer` runs the service's own media routes (stable URLs, Range, conversion on first fetch, the path
guard) on a free port inside the CLI process, for when no service is running. There is no second file server.
"""

from __future__ import annotations

import re
import socket
import threading
import time


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


class EmbeddedServer:
    def __init__(self, library, cfg, host_ip: str, port: int = 0):
        self.library, self.cfg, self.host_ip, self.requested_port = library, cfg, host_ip, port
        self.port = 0
        self._server = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        import uvicorn
        from . import service
        # The speaker addresses us by the IP it can reach, which may not be the default route's address (VPN, two networks).
        app = service.create_app(self.cfg, library=self.library, port=self.requested_port or None, media_only=True,      # nothing but the files being played
                                 allowed_hosts=service.default_allowed_hosts() | {self.host_ip})
        self._server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=self.requested_port, log_level="warning"))
        self._thread = threading.Thread(target=self._run, name="embedded-media", daemon=True)
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                break
            if not self._thread.is_alive():                       # e.g. the port is in use: uvicorn exits the thread
                raise ValueError(f"Can't serve files on port {self.requested_port or 'any'}: the port may be in use or not allowed")
            time.sleep(0.05)
        else:
            raise ValueError("the file server did not start")
        self.port = self._server.servers[0].sockets[0].getsockname()[1]

    def _run(self) -> None:
        try:
            self._server.run()
        except SystemExit:                                        # uvicorn exits the thread when it can't bind
            pass

    @property
    def base_url(self) -> str:
        return f"http://{self.host_ip}:{self.port}"

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(5)
