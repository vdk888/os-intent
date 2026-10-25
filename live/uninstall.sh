#!/usr/bin/env bash
# Stop and remove the oi-live LaunchAgent. hud-log/, status/, models/ and logs are kept.
set -euo pipefail
LABEL=com.osintent.oi-live
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "removed agent $LABEL"
echo "kept: ~/.local/share/os-intent/{hud-log,status,models,logs}"
