"""The Chrome extension's manifest and the service's Origin check for it (spec FR14, NFR6)."""

import base64
import hashlib
import json
from pathlib import Path

from starlette.testclient import TestClient

from sonos_cli import config, service
from sonos_cli.library import Library

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "extension" / "manifest.json").read_text())
BASE = "http://127.0.0.1:8737"
JSON = {"Content-Type": "application/json"}


def extension_id(key: str) -> str:
    """Chrome derives an extension's ID from its public key: the first 16 bytes of the SHA-256, as letters a to p."""
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def test_the_manifest_asks_for_exactly_the_permissions_the_extension_uses():
    assert MANIFEST["manifest_version"] == 3
    assert sorted(MANIFEST["permissions"]) == ["activeTab", "offscreen", "scripting", "storage", "tabCapture"]
    assert MANIFEST["host_permissions"] == ["http://localhost:8737/*"]
    assert "optional_permissions" not in MANIFEST and "content_scripts" not in MANIFEST and "externally_connectable" not in MANIFEST
    assert MANIFEST["action"]["default_popup"] == "popup.html" and (ROOT / "extension" / "popup.html").is_file()
    assert MANIFEST["background"]["service_worker"] == "background.js" and (ROOT / "extension" / "background.js").is_file()


def test_the_pinned_key_gives_the_id_the_harness_and_the_readme_use():
    expected = (ROOT / "tests" / "extension_harness" / "extid.txt").read_text().strip()
    assert extension_id(MANIFEST["key"]) == expected
    assert expected in (ROOT / "README.md").read_text()                       # the id the README tells the user to allow


def app_for(tmp_path, ids):
    cfg = config.Config(path=tmp_path / "config.toml", extension_ids=tuple(ids))
    app = service.create_app(cfg, library=Library.create([]), allowed_hosts={"127.0.0.1"})
    return TestClient(app, base_url=BASE)


def test_the_service_accepts_the_configured_extension_and_refuses_another_origin(tmp_path):
    mine = extension_id(MANIFEST["key"])
    other = "a" * 32
    client = app_for(tmp_path, [mine])
    body = {"html": "<p>nothing</p>", "base_url": "http://x.test/"}
    ok = client.post("/api/send/preview", json=body, headers={**JSON, "Origin": f"chrome-extension://{mine}"})
    assert ok.status_code == 422 and "No audio" in ok.json()["error"]            # past the Origin check; the page just has no audio
    for origin in (f"chrome-extension://{other}", "https://evil.example", "chrome-extension://", None):
        headers = {**JSON, **({"Origin": origin} if origin else {})}
        assert client.post("/api/send/preview", json=body, headers=headers).status_code == 403, origin
    unconfigured = app_for(tmp_path, [])
    assert unconfigured.post("/api/send/preview", json=body, headers={**JSON, "Origin": f"chrome-extension://{mine}"}).status_code == 403
