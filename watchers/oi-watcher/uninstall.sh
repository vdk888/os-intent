#!/usr/bin/env bash
# Stop and remove the oi-watcher LaunchAgent, its .app bundle and CLI symlink. Logs and ActivityWatch data are kept.
# The shared "OS-intent Dev" signing identity stays in the login keychain (other apps use it).
set -euo pipefail
LABEL=com.osintent.oi-watcher
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist" "$HOME/.local/bin/oi-watcher"
rm -rf "$HOME/Applications/OS-intent/oi-watcher.app"
rmdir "$HOME/Applications/OS-intent" 2>/dev/null || true
echo "removed agent $LABEL, ~/Applications/OS-intent/oi-watcher.app and ~/.local/bin/oi-watcher"
echo "optional: tccutil reset All $LABEL   (clears its privacy grants)"
