import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))   # tests/fakes.py, tests/live.py

from sonos_cli import config


def closed_port() -> int:
    """A port nothing listens on, so the CLI doesn't find the developer's real service running on 8737."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def isolated_config(tmp_path_factory, monkeypatch):
    """Never read the developer's real ~/Library/Application Support/sonos/config.toml."""
    d = tmp_path_factory.mktemp("sonos-config")
    (d / "config.toml").write_text(f"port = {closed_port()}\n")
    monkeypatch.delenv("SONOS_IP", raising=False)
    monkeypatch.delenv("SONOS_ROOM", raising=False)
    monkeypatch.setenv("SONOS_CLI_CONFIG_DIR", str(d))
    config.reload()
    yield
    config._cached = None          # forget it without reading anything: the real config may be mid-edit
