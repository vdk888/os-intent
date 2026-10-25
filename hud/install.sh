#!/usr/bin/env bash
# Build oi-hud as a signed .app bundle in ~/Applications/OS-intent/ and (re)load its LaunchAgent
# (`oi-hud serve`: the HUD socket, the permanent wish hotkey, the modifier taps while visible).
# W9: same pattern as oi-watcher / oi-menubar (W7, scripts/bundle.sh): signed with the stable
# "OS-intent Dev" identity so macOS keeps the grants across rebuilds (Input Monitoring for the
# taps, Speech Recognition + Microphone for wish dictation), shows its prompts and lists it in the
# "+" picker. Support/Info.plist (usage strings) is the bundle's Info.plist.
# Idempotent: re-running rebuilds, re-signs and restarts. ~/.local/bin/oi-hud becomes a symlink into the bundle.
set -euo pipefail
cd "$(dirname "$0")"

LABEL=com.osintent.oi-hud
BIN=oi-hud
SCRIPTS="$(cd "../scripts" && pwd)"
APP_DIR="$HOME/Applications/OS-intent"
APP="$APP_DIR/$BIN.app"
BIN_DIR="$HOME/.local/bin"
LOG_DIR="$HOME/.local/share/os-intent/logs"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

"$SCRIPTS/make-signing-identity.sh"
mkdir -p "$BIN_DIR" "$LOG_DIR" "$HOME/Library/LaunchAgents"
chmod 700 "$HOME/.local/share/os-intent"

# Build + sign first (the running copy keeps running), then swap the agent over.
"$SCRIPTS/bundle.sh" . "$LABEL" "$BIN" "$APP_DIR" >/dev/null
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
# a manual `oi-hud serve` started from a terminal holds the socket: stop it first
pkill -x -u "$(id -u)" -f "$BIN_DIR/$BIN serve" 2>/dev/null || true

# Migration from W5-W8: the bare ad-hoc-signed copy is replaced by a symlink into the bundle.
if [ -f "$BIN_DIR/$BIN" ] && [ ! -L "$BIN_DIR/$BIN" ]; then
    rm -f "$BIN_DIR/$BIN"
    echo "removed old bare binary $BIN_DIR/$BIN (its TCC rows were ad-hoc; grant the app once more)"
fi
ln -sfn "$APP/Contents/MacOS/$BIN" "$BIN_DIR/$BIN"

sed "s#__HOME__#$HOME#g" "$LABEL.plist" > "$PLIST"
plutil -lint "$PLIST" >/dev/null
launchctl bootstrap "$DOMAIN" "$PLIST"

echo "installed: $APP (signed: $(codesign -d -r- "$APP" 2>&1 | sed -n 's/^designated => //p'))"
echo "agent $LABEL loaded -> $APP/Contents/MacOS/$BIN serve; CLI: $BIN_DIR/$BIN -> bundle"
echo "log: $LOG_DIR/$BIN.log"
echo "permissions (README.md): Input Monitoring for oi-hud.app (modifier taps; prompt on the first HUD show),"
echo "  Speech Recognition + Microphone on the first wish dictation; Accessibility optional (hide on typing)"
