#!/usr/bin/env bash
# Stop and remove the oi-hud LaunchAgent, its .app bundle and CLI symlink. Logs, settings and wishes/ are kept.
# The shared "OS-intent Dev" signing identity stays in the login keychain (other apps use it).
set -euo pipefail
LABEL=com.osintent.oi-hud
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist" "$HOME/.local/bin/oi-hud"
rm -rf "$HOME/Applications/OS-intent/oi-hud.app"
rmdir "$HOME/Applications/OS-intent" 2>/dev/null || true
echo "removed agent $LABEL, ~/Applications/OS-intent/oi-hud.app and ~/.local/bin/oi-hud"
echo "kept: ~/.local/share/os-intent/{logs,wishes,settings.json}"
echo "optional: tccutil reset All $LABEL   (clears its privacy grants)"
