#!/usr/bin/env bash
# Assemble the Linux distribution tarball from a built PyInstaller binary.
# Usage: packaging/linux/make-tarball.sh [version]  (run from repo root)
set -euo pipefail
ver="${1:-dev}"
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"

work="$(mktemp -d)"
stage="$work/gamestat"
mkdir -p "$stage"

cp dist/gamestat                       "$stage/gamestat"
cp packaging/linux/gamestat.desktop    "$stage/gamestat.desktop"
cp packaging/linux/gamestat-tui.desktop "$stage/gamestat-tui.desktop"
cp packaging/assets/icon.png           "$stage/icon.png"
cp packaging/linux/install.sh          "$stage/install.sh"
cp README.md                           "$stage/README.md" 2>/dev/null || true
# Include the Rust TUI if it has been built.
if [ -f tui/target/release/gamestat-tui ]; then
  cp tui/target/release/gamestat-tui   "$stage/gamestat-tui"
  chmod +x "$stage/gamestat-tui"
fi
chmod +x "$stage/gamestat" "$stage/install.sh"

mkdir -p out
tar -C "$work" -czf "out/gamestat-linux-x86_64.tar.gz" gamestat
rm -rf "$work"
echo "wrote out/gamestat-linux-x86_64.tar.gz ($ver)"
