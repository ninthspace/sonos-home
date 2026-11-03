"""`sonos service install | uninstall | status`: run the service as a launchd LaunchAgent (FR2, AD6).

launchd starts a small ad-hoc-signed app bundle (`SonosService.app`) whose launcher spawns the service's Python as a
child. macOS then attributes Local Network access to the bundle, not to the Python binary, so the permission survives
`uv tool upgrade` (spike 01-01 Story 1). The agent never points at an editable checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from importlib import metadata, resources
from pathlib import Path
from typing import Callable

from . import config as config_mod
from .client import ServiceClient, ServiceError, ServiceUnavailable

LABEL = "local.sonos-cli"
BUNDLE_ID = "local.sonos-cli.service"
BUNDLE_NAME = "SonosService.app"
EXECUTABLE = "SonosService"


class InstallError(ValueError):
    pass


def launch_agents_dir() -> Path:
    return Path(os.environ.get("SONOS_CLI_LAUNCH_AGENTS_DIR") or Path.home() / "Library/LaunchAgents")


def plist_path() -> Path:
    return launch_agents_dir() / f"{LABEL}.plist"


def log_path() -> Path:
    return Path(os.environ.get("SONOS_CLI_LOG_DIR") or Path.home() / "Library/Logs/sonos") / "service.log"


def bundle_path() -> Path:
    return config_mod.config_dir() / BUNDLE_NAME


def is_editable_install() -> bool:
    """True when the installed package points at a source checkout (`uv sync`, `pip install -e`)."""
    try:
        text = metadata.distribution("sonos-cli").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return False
    if not text:
        return False
    try:
        return bool(json.loads(text).get("dir_info", {}).get("editable"))
    except ValueError:
        return False


def service_command(python: Path | None = None) -> list[str]:
    return [str(python or sys.executable), "-m", "sonos_cli", "service", "run"]


def plist_content(launcher: Path, command: list[str], log: Path, path_env: str | None = None) -> bytes:
    """The LaunchAgent: the bundle's launcher runs `command`; launchd restarts it and starts it at login."""
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(launcher), *command],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PATH": path_env or "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    })


def info_plist() -> bytes:
    return plistlib.dumps({
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": "SonosService",
        "CFBundleExecutable": EXECUTABLE,
        "CFBundlePackageType": "APPL",
        "LSUIElement": True,
        "NSLocalNetworkUsageDescription": "Sonos CLI talks to the Sonos speakers on your network.",
        "NSBonjourServices": ["_sonos._tcp"],
    })


def shipped_launcher() -> bytes:
    """The prebuilt universal launcher that ships in the package."""
    return resources.files("sonos_cli").joinpath("launcher").read_bytes()


def build_bundle(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> Path:
    """Make the launcher bundle from the shipped universal binary and ad-hoc sign it. Needs only `codesign`, which every Mac has.

    Reused as-is when the binary and Info.plist haven't changed, so the Local Network grant stays."""
    binary = shipped_launcher()
    digest = hashlib.sha256(binary + info_plist()).hexdigest()
    bundle = bundle_path()
    stamp = bundle.with_name(BUNDLE_NAME + ".hash")                # outside the bundle: a stray file would break the seal
    exe = bundle / "Contents" / "MacOS" / EXECUTABLE
    if exe.exists() and stamp.exists() and stamp.read_text() == digest:
        return exe
    shutil.rmtree(bundle, ignore_errors=True)
    stamp.unlink(missing_ok=True)
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    (bundle / "Contents" / "Info.plist").write_bytes(info_plist())
    exe.write_bytes(binary)
    exe.chmod(0o755)
    cmd = ["codesign", "--force", "--sign", "-", "--identifier", BUNDLE_ID, str(bundle)]
    try:
        r = run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise InstallError("`codesign` was not found. It ships with macOS (/usr/bin/codesign); is this a Mac?") from None
    if r.returncode != 0:
        raise InstallError(f"codesign failed: {(r.stderr or r.stdout).strip()[:300]}")
    stamp.write_text(digest)
    return exe


def _launchctl(args: list[str], run=subprocess.run) -> subprocess.CompletedProcess:
    return run(["launchctl", *args], capture_output=True, text=True)


def domain() -> str:
    return f"gui/{os.getuid()}"


def is_loaded(run=subprocess.run) -> bool:
    return _launchctl(["print", f"{domain()}/{LABEL}"], run).returncode == 0


def _wait_until_unloaded(run, seconds: float = 8.0) -> bool:
    deadline = time.monotonic() + seconds
    while is_loaded(run):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.2)
    return True


def install(allow_editable: bool = False, run=subprocess.run, health_timeout: float = 20.0,
            print_fn: Callable[[str], None] = print) -> int:
    if is_editable_install() and not allow_editable:
        raise InstallError("This copy of sonos is an editable install of a source checkout, and the service would break "
                           "when the checkout moves or changes. Install it properly (`uv tool install <repo>`) and run "
                           "`sonos service install` from that, or pass --allow-editable to install from here anyway.")
    cfg = config_mod.get()
    launcher = build_bundle(run)
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    plist = plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plist_content(launcher, service_command(), log))
    os.chmod(plist, 0o644)                                      # launchd refuses agents that are group/world writable
    if is_loaded(run):
        _launchctl(["bootout", f"{domain()}/{LABEL}"], run)
        if not _wait_until_unloaded(run):
            raise InstallError("The old service is still shutting down. Wait a few seconds and run install again.")
    r = _launchctl(["bootstrap", domain(), str(plist)], run)
    if r.returncode != 0:                                       # bootout can still be settling: one more try
        time.sleep(1.0)
        r = _launchctl(["bootstrap", domain(), str(plist)], run)
    if r.returncode != 0:
        raise InstallError(f"launchctl bootstrap failed: {(r.stderr or r.stdout).strip()[:300]}")
    print_fn(f"Installed {plist}")
    print_fn("The first time, macOS may ask to let SonosService find devices on your local network. Allow it, with the "
             "screen unlocked; without that the service runs but sees no speakers. Re-allow it if a later upgrade "
             "rebuilds the launcher.")
    client = ServiceClient(cfg.port, timeout=3)
    deadline = time.monotonic() + health_timeout
    while time.monotonic() < deadline:
        try:
            health = client.get("/api/health")
            print_fn(f"The service is running on port {cfg.port} (ffmpeg: {health.get('ffmpeg')}); log: {log}")
            return 0
        except (ServiceUnavailable, ServiceError):
            time.sleep(0.5)
    print_fn(f"Installed, but the service didn't answer on port {cfg.port} within {health_timeout:.0f} s. See {log}.")
    return 1


def uninstall(run=subprocess.run, print_fn: Callable[[str], None] = print) -> int:
    if is_loaded(run):
        _launchctl(["bootout", f"{domain()}/{LABEL}"], run)
        if not _wait_until_unloaded(run):
            print_fn("launchd is still stopping the service; the plist is removed, so it won't come back at login.")
    plist = plist_path()
    existed = plist.exists()
    plist.unlink(missing_ok=True)
    print_fn("Removed the service." if existed else "The service wasn't installed.")
    return 0


def status(run=subprocess.run, print_fn: Callable[[str], None] = print) -> int:
    cfg = config_mod.get()
    loaded = is_loaded(run)
    healthy, detail = False, ""
    try:
        health = ServiceClient(cfg.port, timeout=3).get("/api/health")
        healthy, detail = True, f"ffmpeg {health.get('ffmpeg')}, {health.get('rooms_seen')} room(s) seen"
    except (ServiceUnavailable, ServiceError) as e:
        detail = str(e)
    print_fn(f"launchd agent: {'loaded' if loaded else 'not loaded'} ({plist_path()})")
    print_fn(f"service:       {'healthy' if healthy else 'not answering'} on port {cfg.port} ({detail})")
    print_fn(f"log:           {log_path()}")
    return 0 if loaded and healthy else 1
