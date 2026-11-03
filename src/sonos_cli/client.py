"""A small client for the local service's JSON API (FR8).

The CLI uses it whenever the service accepts a connection on 127.0.0.1:<port>. Only a refused connection
means "no service"; any other failure, and any HTTP error, is reported to the user, never hidden by a fallback.
"""

from __future__ import annotations

import http.client
import json
from typing import Any


class ServiceUnavailable(Exception):
    """Nothing is listening: the connection was refused."""


class ServiceError(ValueError):
    """The service answered with an error (or couldn't be reached for a reason other than 'refused')."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ServiceClient:
    def __init__(self, port: int, host: str = "127.0.0.1", timeout: float = 60.0):
        self.host, self.port, self.timeout = host, port, timeout

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    def request(self, method: str, path: str, body: dict | None = None) -> Any:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        headers = {}
        payload = None
        if body is not None or method != "GET":
            payload = json.dumps(body if body is not None else {}).encode()
            headers = {"Content-Type": "application/json", "Origin": self.origin}
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except ConnectionRefusedError:
            raise ServiceUnavailable(f"nothing is listening on {self.host}:{self.port}") from None
        except OSError as e:                          # timeouts, resets: the service is there but something is wrong
            raise ServiceError(f"the service at {self.origin} didn't answer properly: {e}") from e
        finally:
            conn.close()
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        if resp.status >= 400:
            message = data.get("error") if isinstance(data, dict) and "error" in data else raw.decode("utf-8", "replace")[:300]
            raise ServiceError(message or f"HTTP {resp.status}", resp.status)
        return data

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, body: dict | None = None) -> Any:
        return self.request("POST", path, body or {})
