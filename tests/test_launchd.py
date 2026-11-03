"""launchd install, uninstall and status (spec FR2, AD6). Nothing here touches the real LaunchAgents folder."""

import json
import os
import plistlib
import shutil
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sonos_cli import cli, config, launchd


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("SONOS_CLI_LAUNCH_AGENTS_DIR", str(tmp_path / "LaunchAgents"))
    monkeypatch.setenv("SONOS_CLI_LOG_DIR", str(tmp_path / "Logs"))
    monkeypatch.setenv("SONOS_CLI_CONFIG_DIR", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir()
    config._cached = None
    monkeypatch.setattr(launchd, "is_editable_install", lambda: False)
    return tmp_path


class FakeLaunchctl:
    """Runs codesign for real (the bundle must really be signed) and records launchctl calls."""

    def __init__(self, loaded=False):
        self.loaded, self.calls, self.tool_calls = loaded, [], []

    def __call__(self, cmd, **kw):
        if cmd[0] != "launchctl":
            self.tool_calls.append(cmd[0])
            return subprocess.run(cmd, **kw)
        self.calls.append(cmd[1:])
        if cmd[1] == "print":
            return subprocess.CompletedProcess(cmd, 0 if self.loaded else 113, "", "")
        if cmd[1] == "bootstrap":
            self.loaded = True
        if cmd[1] == "bootout":
            self.loaded = False
        return subprocess.CompletedProcess(cmd, 0, "", "")


def health_stub(port=0, body=None):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            data = json.dumps(body or {"ffmpeg": "ok", "rooms_seen": 4}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def set_port(tmp_path, port):
    (tmp_path / "cfg" / "config.toml").write_text(f"port = {port}\n")
    config._cached = None


# ---- the plist ---------------------------------------------------------------------------------------------

def test_the_plist_runs_the_launcher_with_keepalive_runatload_and_the_log_paths(tmp_path):
    launcher = Path("/Users/x/Library/Application Support/sonos/SonosService.app/Contents/MacOS/SonosService")
    command = ["/Users/x/.local/share/uv/tools/sonos-cli/bin/python", "-m", "sonos_cli", "service", "run"]
    data = plistlib.loads(launchd.plist_content(launcher, command, tmp_path / "service.log"))
    assert data["Label"] == "local.sonos-cli"
    assert data["ProgramArguments"] == [str(launcher), *command]
    assert data["KeepAlive"] is True and data["RunAtLoad"] is True
    assert data["StandardOutPath"] == data["StandardErrorPath"] == str(tmp_path / "service.log")
    assert "/opt/homebrew/bin" in data["EnvironmentVariables"]["PATH"].split(":")      # so ffmpeg is found at login


def test_the_default_paths_are_the_ones_the_spec_names(monkeypatch):
    for var in ("SONOS_CLI_LAUNCH_AGENTS_DIR", "SONOS_CLI_LOG_DIR", "SONOS_CLI_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    home = Path.home()
    assert launchd.plist_path() == home / "Library/LaunchAgents/local.sonos-cli.plist"
    assert launchd.log_path() == home / "Library/Logs/sonos/service.log"
    assert launchd.bundle_path() == home / "Library/Application Support/sonos/SonosService.app"


def test_the_bundle_declares_local_network_use():
    info = plistlib.loads(launchd.info_plist())
    assert info["CFBundleIdentifier"] == "local.sonos-cli.service" and info["CFBundleExecutable"] == "SonosService"
    assert info["NSLocalNetworkUsageDescription"] and "_sonos._tcp" in info["NSBonjourServices"]


# ---- install -----------------------------------------------------------------------------------------------

def test_install_builds_the_bundle_writes_the_plist_and_bootstraps_it(sandbox):
    stub = health_stub()
    set_port(sandbox, stub.server_address[1])
    run = FakeLaunchctl()
    out = []
    assert launchd.install(run=run, print_fn=out.append, health_timeout=5) == 0
    plist = plistlib.loads(launchd.plist_path().read_bytes())
    assert plist["ProgramArguments"][0] == str(launchd.bundle_path() / "Contents/MacOS/SonosService")
    assert plist["ProgramArguments"][1:] == launchd.service_command()
    assert Path(plist["ProgramArguments"][0]).exists()
    assert ["bootstrap", launchd.domain(), str(launchd.plist_path())] in run.calls
    assert run.tool_calls == ["codesign"]                                    # nothing is compiled at install time
    verify = subprocess.run(["codesign", "-dv", str(launchd.bundle_path())], capture_output=True, text=True)
    assert "Identifier=local.sonos-cli.service" in verify.stderr
    assert any("running" in line and "ffmpeg: ok" in line for line in out)
    stub.shutdown()


def test_install_replaces_an_agent_that_is_already_loaded(sandbox):
    stub = health_stub()
    set_port(sandbox, stub.server_address[1])
    run = FakeLaunchctl(loaded=True)
    launchd.install(run=run, print_fn=lambda s: None, health_timeout=5)
    kinds = [c[0] for c in run.calls]
    assert "bootout" in kinds and kinds.index("bootout") < kinds.index("bootstrap")
    stub.shutdown()


def test_install_says_so_when_the_service_does_not_answer(sandbox):
    set_port(sandbox, 1)           # nothing listens there
    out = []
    assert launchd.install(run=FakeLaunchctl(), print_fn=out.append, health_timeout=0.6) == 1
    assert "didn't answer" in out[-1] and str(launchd.log_path()) in out[-1]


def test_install_refuses_an_editable_checkout_unless_told_to(sandbox, monkeypatch):
    monkeypatch.setattr(launchd, "is_editable_install", lambda: True)
    run = FakeLaunchctl()
    with pytest.raises(launchd.InstallError, match="--allow-editable"):
        launchd.install(run=run, print_fn=lambda s: None)
    assert run.calls == [] and not launchd.plist_path().exists()
    stub = health_stub()
    set_port(sandbox, stub.server_address[1])
    assert launchd.install(allow_editable=True, run=run, print_fn=lambda s: None, health_timeout=5) == 0
    stub.shutdown()


def test_the_bundle_is_reused_when_nothing_changed_and_rebuilt_when_the_launcher_changes(sandbox, monkeypatch):
    run = FakeLaunchctl()
    exe = launchd.build_bundle(run)
    assert run.tool_calls == ["codesign"]
    inode = exe.stat().st_ino
    assert launchd.build_bundle(run) == exe and run.tool_calls == ["codesign"] and exe.stat().st_ino == inode
    stamp = launchd.bundle_path().with_name("SonosService.app.hash")
    stamp.write_text("an older launcher")
    launchd.build_bundle(run)
    assert run.tool_calls == ["codesign"] * 2


def test_a_new_launcher_in_an_upgraded_package_rebuilds_the_bundle(sandbox, monkeypatch):
    run = FakeLaunchctl()
    launchd.build_bundle(run)
    assert run.tool_calls == ["codesign"]
    thin = sandbox / "thin-launcher"
    subprocess.run(["lipo", str(SHIPPED), "-thin", "arm64", "-output", str(thin)], check=True)        # a different, valid launcher
    monkeypatch.setattr(launchd, "shipped_launcher", lambda: thin.read_bytes())                     # as after a package upgrade
    launchd.build_bundle(run)
    assert run.tool_calls == ["codesign", "codesign"]
    archs = subprocess.run(["lipo", "-archs", str(launchd.bundle_path() / "Contents/MacOS/SonosService")], capture_output=True, text=True).stdout.split()
    assert archs == ["arm64"]                                                                         # the new launcher is the one in the bundle


def test_the_built_bundle_passes_a_strict_signature_check(sandbox):
    launchd.build_bundle(FakeLaunchctl())
    r = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(launchd.bundle_path())], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_a_failing_codesign_is_an_install_error_with_its_words(sandbox):
    def broken(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "the sealed resource is missing")

    with pytest.raises(launchd.InstallError, match="codesign failed: the sealed resource is missing"):
        launchd.build_bundle(broken)


def test_a_missing_codesign_says_so_instead_of_a_traceback(sandbox):
    def gone(cmd, **kw):
        raise FileNotFoundError("codesign")

    with pytest.raises(launchd.InstallError, match="codesign.*was not found"):
        launchd.build_bundle(gone)


class NoDeveloperTools(FakeLaunchctl):
    """A Mac without the Xcode command line tools: asking for clang or xcode-select is a failure the install must not even attempt."""

    def __call__(self, cmd, **kw):
        if cmd[0] in ("clang", "cc", "gcc", "xcode-select", "make", "swiftc"):
            raise AssertionError(f"install tried to use {cmd[0]}")
        return super().__call__(cmd, **kw)


def test_install_needs_no_developer_tools(sandbox, monkeypatch):
    stub = health_stub()
    set_port(sandbox, stub.server_address[1])
    monkeypatch.setenv("PATH", "/usr/bin:/bin")                              # no Homebrew either
    real_run = subprocess.run

    def guarded(cmd, *a, **k):                                                  # a call that bypasses the injected runner is caught too
        if cmd[0] in ("clang", "cc", "gcc", "xcode-select", "make", "swiftc"):
            raise AssertionError(f"install tried to use {cmd[0]}")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(launchd.subprocess, "run", guarded)
    run = NoDeveloperTools()
    assert launchd.install(run=run, print_fn=lambda s: None, health_timeout=5) == 0
    assert run.tool_calls == ["codesign"]
    r = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(launchd.bundle_path())], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    stub.shutdown()


SHIPPED = Path(launchd.__file__).with_name("launcher")
SOURCE = Path(launchd.__file__).with_name("launcher.c")
BUILD_INFO = Path(launchd.__file__).with_name("launcher.build")


def build_info() -> dict:
    return dict(line.split(" ", 1) for line in BUILD_INFO.read_text().splitlines())


def test_the_shipped_launcher_is_a_universal_binary(tmp_path):
    archs = subprocess.run(["lipo", "-archs", str(SHIPPED)], capture_output=True, text=True, check=True).stdout.split()
    assert sorted(archs) == ["arm64", "x86_64"]


def test_the_shipped_launcher_was_built_from_the_shipped_source():
    import hashlib
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == build_info()["source-sha256"], \
        "launcher.c changed since the binary was built: run tools/build_launcher.sh and commit launcher, launcher.build and launcher.c together"


def test_a_fresh_build_of_the_shipped_source_is_byte_identical_on_the_toolchain_that_made_it(tmp_path):
    if subprocess.run(["xcode-select", "-p"], capture_output=True).returncode != 0:
        pytest.skip("no developer tools here (a maintainer machine has them)")
    clang = subprocess.run(["clang", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    sdk = subprocess.run(["xcrun", "--show-sdk-version"], capture_output=True, text=True).stdout.strip()
    info = build_info()
    if (clang, sdk) != (info["clang"].strip(), info["sdk"].strip()):
        pytest.skip(f"built with {info['clang'].strip()}, SDK {info['sdk'].strip()}; this machine has {clang}, SDK {sdk}, which make different bytes")
    fresh = tmp_path / "launcher"                                            # the output's name goes into the build, so it must match
    subprocess.run(["clang", "-O2", "-arch", "arm64", "-arch", "x86_64", "-mmacosx-version-min=11.0", "-o", str(fresh), str(SOURCE)], check=True)
    assert fresh.read_bytes() == SHIPPED.read_bytes(), "the shipped launcher is not what launcher.c builds to: run tools/build_launcher.sh and commit the result"


def test_the_shipped_launcher_is_what_goes_into_the_bundle(sandbox):
    exe = launchd.build_bundle(FakeLaunchctl())
    archs = subprocess.run(["lipo", "-archs", str(exe)], capture_output=True, text=True, check=True).stdout.split()
    assert sorted(archs) == ["arm64", "x86_64"]                                # the universal binary, signed, not a rebuild for this Mac
    assert not [f for f in launchd.bundle_path().rglob("*") if f.suffix == ".c"]


# ---- uninstall and status --------------------------------------------------------------------------------------

def test_uninstall_unloads_and_removes_the_plist(sandbox):
    stub = health_stub()
    set_port(sandbox, stub.server_address[1])
    run = FakeLaunchctl()
    launchd.install(run=run, print_fn=lambda s: None, health_timeout=5)
    out = []
    assert launchd.uninstall(run=run, print_fn=out.append) == 0
    assert not launchd.plist_path().exists() and ["bootout", f"{launchd.domain()}/local.sonos-cli"] in run.calls
    assert launchd.uninstall(run=run, print_fn=out.append) == 0 and "wasn't installed" in out[-1]
    stub.shutdown()


def test_status_reports_loaded_healthy_and_the_log_path(sandbox):
    stub = health_stub(body={"ffmpeg": "ok", "rooms_seen": 3})
    set_port(sandbox, stub.server_address[1])
    out = []
    assert launchd.status(run=FakeLaunchctl(loaded=True), print_fn=out.append) == 0
    text = "\n".join(out)
    assert "loaded" in text and "healthy" in text and "3 room(s) seen" in text and str(launchd.log_path()) in text
    out.clear()
    assert launchd.status(run=FakeLaunchctl(loaded=False), print_fn=out.append) == 1 and "not loaded" in "\n".join(out)
    stub.shutdown()
    out.clear()
    assert launchd.status(run=FakeLaunchctl(loaded=True), print_fn=out.append) == 1 and "not answering" in "\n".join(out)


def test_the_cli_has_install_uninstall_and_status(sandbox, monkeypatch):
    seen = []
    monkeypatch.setattr(launchd, "install", lambda allow_editable=False: seen.append(("install", allow_editable)) or 0)
    monkeypatch.setattr(launchd, "uninstall", lambda: seen.append("uninstall") or 0)
    monkeypatch.setattr(launchd, "status", lambda: seen.append("status") or 0)
    assert cli.main(["service", "install"]) == 0 and cli.main(["service", "install", "--allow-editable"]) == 0
    assert cli.main(["service", "uninstall"]) == 0 and cli.main(["service", "status"]) == 0
    assert seen == [("install", False), ("install", True), "uninstall", "status"]


def test_an_install_error_is_reported_by_the_cli_not_as_a_traceback(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(launchd, "is_editable_install", lambda: True)
    assert cli.main(["service", "install"]) == 1
    assert "--allow-editable" in capsys.readouterr().err


# ---- the launcher itself ---------------------------------------------------------------------------------------

@pytest.fixture
def launcher(sandbox):
    return launchd.build_bundle(subprocess.run)


def test_the_launcher_runs_its_arguments_and_returns_their_exit_status(launcher):
    assert subprocess.run([str(launcher), "/bin/sh", "-c", "exit 7"]).returncode == 7
    assert subprocess.run([str(launcher), "/bin/echo", "hi"], capture_output=True, text=True).stdout == "hi\n"
    assert subprocess.run([str(launcher)]).returncode == 2                 # nothing to run
    assert subprocess.run([str(launcher), "/no/such/program"]).returncode == 3


def test_the_launcher_passes_sigterm_on_so_launchd_can_stop_the_service(launcher, tmp_path):
    pidfile = tmp_path / "child.pid"
    proc = subprocess.Popen([str(launcher), "/bin/sh", "-c", f"echo $$ > {pidfile}; exec sleep 60"])
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.05)
    child = int(pidfile.read_text())
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(10) == 128 + signal.SIGTERM
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        import os
        os.kill(child, 0)                                                   # the child went too


class SlowToUnload(FakeLaunchctl):
    """bootout returns at once but the job lingers for a few `print` calls; the first bootstrap after is refused."""

    def __init__(self):
        super().__init__(loaded=True)
        self.lingering = 0
        self.refusals = 1

    def __call__(self, cmd, **kw):
        if cmd[0] != "launchctl":
            return super().__call__(cmd, **kw)
        verb = cmd[1]
        self.calls.append(cmd[1:])
        if verb == "bootout":
            self.lingering = 3
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if verb == "print":
            if self.lingering:
                self.lingering -= 1
                if self.lingering == 0:
                    self.loaded = False
            return subprocess.CompletedProcess(cmd, 0 if self.loaded else 113, "", "")
        if verb == "bootstrap":
            if self.loaded:                                         # the old job hasn't gone yet
                return subprocess.CompletedProcess(cmd, 5, "", "Bootstrap failed: 5: Input/output error")
            if self.refusals:
                self.refusals -= 1
                return subprocess.CompletedProcess(cmd, 5, "", "Bootstrap failed: 5: Input/output error")
            self.loaded = True
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_reinstalling_waits_for_the_old_agent_to_go_and_retries_a_refused_bootstrap(sandbox):
    stub = health_stub()
    set_port(sandbox, stub.server_address[1])
    run = SlowToUnload()
    out = []
    assert launchd.install(run=run, print_fn=out.append, health_timeout=5) == 0
    kinds = [c[0] for c in run.calls]
    assert kinds.count("bootstrap") == 2 and kinds.index("bootout") < kinds.index("bootstrap")
    assert any("local network" in line for line in out)                      # the Local Network prompt is mentioned
    stub.shutdown()


def test_the_plist_is_not_group_or_world_writable_whatever_the_umask(sandbox):
    import os
    stub = health_stub()
    set_port(sandbox, stub.server_address[1])
    old = os.umask(0o002)
    try:
        launchd.install(run=FakeLaunchctl(), print_fn=lambda s: None, health_timeout=5)
    finally:
        os.umask(old)
    assert launchd.plist_path().stat().st_mode & 0o777 == 0o644
    stub.shutdown()


def test_a_sigterm_that_arrives_before_the_launcher_knows_its_child_is_remembered_and_passed_on(launcher, tmp_path):
    """The shipped binary, told by its environment to wait 0.5 s between spawning the child and noting its pid; the signal lands in that window."""
    pidfile = tmp_path / "child.pid"
    proc = subprocess.Popen([str(launcher), "/bin/sh", "-c", f"echo $$ > {pidfile}; exec sleep 60"],
                            env={**os.environ, "SONOS_LAUNCHER_SPAWN_DELAY_US": "500000"})
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.02)
    child = int(pidfile.read_text())                      # the child exists; the launcher hasn't noted its pid yet
    t0 = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    try:
        code = proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        os.kill(child, signal.SIGKILL)
        pytest.fail("the early SIGTERM was lost: the launcher and its child kept running")
    assert code == 128 + signal.SIGTERM
    assert time.monotonic() - t0 < 5
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)



class NeverUnloads(FakeLaunchctl):
    def __call__(self, cmd, **kw):
        if cmd[0] == "launchctl" and cmd[1] == "bootout":
            self.calls.append(cmd[1:])
            return subprocess.CompletedProcess(cmd, 0, "", "")          # claims success; the job stays
        return super().__call__(cmd, **kw)


def test_uninstall_says_when_launchd_is_still_stopping_the_service_but_still_removes_the_plist(sandbox, monkeypatch):
    monkeypatch.setattr(launchd.time, "sleep", lambda s: None)
    ticks = iter(range(0, 10_000, 3))
    monkeypatch.setattr(launchd.time, "monotonic", lambda: next(ticks))
    launchd.plist_path().parent.mkdir(parents=True)
    launchd.plist_path().write_bytes(b"x")
    out = []
    assert launchd.uninstall(run=NeverUnloads(loaded=True), print_fn=out.append) == 0
    assert not launchd.plist_path().exists() and any("still stopping" in line for line in out)


def test_install_gives_up_with_a_clear_message_when_the_old_agent_never_unloads(sandbox, monkeypatch):
    monkeypatch.setattr(launchd.time, "sleep", lambda s: None)
    ticks = iter(range(0, 10_000, 3))
    monkeypatch.setattr(launchd.time, "monotonic", lambda: next(ticks))
    with pytest.raises(launchd.InstallError, match="still shutting down"):
        launchd.install(run=NeverUnloads(loaded=True), print_fn=lambda s: None)


@pytest.mark.parametrize("value", ["-1", "99999999999999999999", "4294967296", "abc", "", "0x10", "2000001"])
def test_a_hostile_or_odd_spawn_delay_in_the_environment_is_ignored_not_obeyed(launcher, value):
    began = time.monotonic()
    proc = subprocess.Popen([str(launcher), "/bin/echo", "ok"], stdout=subprocess.PIPE, text=True, env={**os.environ, "SONOS_LAUNCHER_SPAWN_DELAY_US": value})
    try:
        out, _ = proc.communicate(timeout=6)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail(f"the launcher obeyed SONOS_LAUNCHER_SPAWN_DELAY_US={value!r} and sat waiting")
    assert out == "ok\n" and proc.returncode == 0 and time.monotonic() - began < 3
