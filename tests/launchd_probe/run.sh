#!/bin/bash
# Throwaway launchd probe for spike 01-01 Story 1. Run it from a Terminal on the Mac with the screen unlocked, so macOS can show
# its Local Network and Dropbox (CloudStorage) permission prompts, and click Allow on each.
#   tests/launchd_probe/run.sh a                  # variant A: the uv venv Python run directly by launchd
#   tests/launchd_probe/run.sh b                  # variant B: ad-hoc-signed SonosProbe.app launcher (Local Network keys) -> same Python
# Env: PYEXE (Python to run, default the repo .venv), PROBE_SITE (its site-packages, needed when PYEXE is another venv),
#      PROBE_DIR (work dir), PROBE_FILE (audio file for ffprobe, default ~/Dropbox/opus-opus/z0ne2.wav).
# Output: probe-<label>.log in the work dir (default $TMPDIR/sonos-probe). The agent is unloaded at the end.
set -e
L=${1:?label a|b}; HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$HERE/../.." && pwd)
W=${PROBE_DIR:-${TMPDIR:-/tmp}/sonos-probe}; mkdir -p "$W"; PYEXE=${PYEXE:-$REPO/.venv/bin/python3}
export REPO
if [ "$L" = b ]; then
  A=$W/SonosProbe.app; rm -rf "$A"; mkdir -p "$A/Contents/MacOS"; clang -o "$A/Contents/MacOS/SonosProbe" "$HERE/launcher.c"
  cat > "$A/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>local.sonos-cli.probe</string><key>CFBundleName</key><string>SonosProbe</string>
<key>CFBundleExecutable</key><string>SonosProbe</string><key>CFBundlePackageType</key><string>APPL</string><key>LSUIElement</key><true/>
<key>NSLocalNetworkUsageDescription</key><string>Sonos CLI talks to the Sonos speakers on your network.</string>
<key>NSBonjourServices</key><array><string>_sonos._tcp</string></array></dict></plist>
PL
  codesign --force --sign - --identifier local.sonos-cli.probe "$A"
  ARGS="<string>$A/Contents/MacOS/SonosProbe</string><string>$PYEXE</string>"
else ARGS="<string>$PYEXE</string>"; fi
P=$W/local.sonos-probe-$L.plist; LOGF=$W/probe-$L.log; rm -f "$LOGF"
cat > "$P" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>local.sonos-probe-$L</string>
<key>ProgramArguments</key><array>$ARGS<string>$HERE/probe.py</string></array>
<key>EnvironmentVariables</key><dict><key>PROBE_LOG</key><string>$LOGF</string><key>REPO</key><string>$REPO</string><key>PROBE_SITE</key><string>${PROBE_SITE:-}</string><key>PROBE_FILE</key><string>${PROBE_FILE:-}</string></dict>
<key>RunAtLoad</key><true/></dict></plist>
PL
launchctl bootout gui/$UID/local.sonos-probe-$L 2>/dev/null || true
trap 'launchctl bootout gui/$UID/local.sonos-probe-$L 2>/dev/null || true' EXIT
launchctl bootstrap gui/$UID "$P"; echo "loaded; answer any permission prompt, waiting up to 120 s"
for i in $(seq 1 120); do sleep 1; grep -q "probe end" "$LOGF" 2>/dev/null && break; done
cat "$LOGF"; launchctl bootout gui/$UID/local.sonos-probe-$L 2>/dev/null || true; echo "unloaded; log at $LOGF"
