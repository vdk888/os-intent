#!/usr/bin/env bash
# Stop and remove the oi-menubar LaunchAgent, its .app bundle and CLI symlink. settings.json and logs are kept.
# The shared "OS-intent Dev" signing identity stays in the login keychain (other apps use it).
set -euo pipefail
LABEL=com.osintent.oi-menubar
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist" "$HOME/.local/bin/oi-menubar"
rm -rf "$HOME/Applications/OS-intent/oi-menubar.app"
rmdir "$HOME/Applications/OS-intent" 2>/dev/null || true
echo "removed agent $LABEL, ~/Applications/OS-intent/oi-menubar.app and ~/.local/bin/oi-menubar"
echo "optional: tccutil reset All $LABEL   (clears its privacy grants)"
