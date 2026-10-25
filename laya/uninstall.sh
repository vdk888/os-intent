#!/usr/bin/env bash
# Stop and remove the oi-laya LaunchAgent (the server it runs stops with it). Reports and models are kept.
set -euo pipefail
LABEL=com.osintent.oi-laya
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "removed agent $LABEL"
echo "kept: ~/.local/share/os-intent/{reports,models,logs,status}"
