#!/usr/bin/env bash
# Install gamestat into your user profile (no root needed).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

bindir="$HOME/.local/bin"
apps="$HOME/.local/share/applications"
icons="$HOME/.local/share/icons/hicolor/256x256/apps"
mkdir -p "$bindir" "$apps" "$icons"

install -m755 "$here/gamestat" "$bindir/gamestat"
install -m644 "$here/icon.png" "$icons/gamestat.png"
install -m644 "$here/gamestat.desktop" "$apps/gamestat.desktop"
update-desktop-database "$apps" 2>/dev/null || true
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

echo "✓ Installed gamestat → $bindir/gamestat"
case ":$PATH:" in
  *":$bindir:"*) : ;;
  *) echo "  ⚠ $bindir is not on your PATH — add it to run 'gamestat' from a terminal." ;;
esac
echo "  Launch the app:  gamestat app   (or find 'gamestat' in your app menu)"
echo "  Report only:     gamestat"
echo "  Uninstall gamestat itself:  rm $bindir/gamestat $apps/gamestat.desktop $icons/gamestat.png"
