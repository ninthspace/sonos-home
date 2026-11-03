#!/bin/sh
# Rebuilds src/sonos_cli/launcher, the universal (arm64 + x86_64) Mach-O the LaunchAgent bundle is made from, out of launcher.c.
# A maintainer step: it needs the Xcode command line tools, which installing the tool does NOT. Run it after changing launcher.c and
# commit the result. It also writes src/sonos_cli/launcher.build: the source's SHA-256 (tests/test_launchd.py fails when launcher.c
# changes without a rebuild) and the toolchain that made the binary (the byte-for-byte check against a fresh build only runs on that
# same toolchain: another SDK or linker makes different bytes from the same source). Rebuilding changes the bundle's digest, so every
# user is asked for Local Network access once more: only rebuild when launcher.c really changed.
set -eu
cd "$(dirname "$0")/.."
clang -O2 -arch arm64 -arch x86_64 -mmacosx-version-min=11.0 -o src/sonos_cli/launcher src/sonos_cli/launcher.c
{
  echo "source-sha256 $(shasum -a 256 src/sonos_cli/launcher.c | cut -d' ' -f1)"
  echo "clang $(clang --version | head -1)"
  echo "sdk $(xcrun --show-sdk-version)"
} > src/sonos_cli/launcher.build
lipo -archs src/sonos_cli/launcher
