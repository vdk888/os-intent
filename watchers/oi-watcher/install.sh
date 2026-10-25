#!/usr/bin/env bash
# Build oi-watcher as a signed .app bundle in ~/Applications/OS-intent/ and (re)load its LaunchAgent.
# W7: a real bundle signed with the stable "OS-intent Dev" identity, so macOS
# keeps its privacy grants across rebuilds, shows its prompts, lists it in the "+" picker and
# accepts `tccutil reset <service> com.osintent.oi-watcher`.
# Idempotent: re-running rebuilds, re-signs and restarts. ~/.local/bin/oi-watcher becomes a symlink
# to the bundle binary (for CLI use).
set -euo pipefail
cd "$(dirname "$0")"

LABEL=com.osintent.oi-watcher
BIN=oi-watcher
SCRIPTS="$(cd "../../scripts" && pwd)"
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

# Migration from W2-W6: the bare ad-hoc-signed copy is replaced by a symlink into the bundle.
if [ -f "$BIN_DIR/$BIN" ] && [ ! -L "$BIN_DIR/$BIN" ]; then
    rm -f "$BIN_DIR/$BIN"
    echo "removed old bare binary $BIN_DIR/$BIN (its TCC rows were ad-hoc; grant the app once more)"
fi
ln -sfn "$APP/Contents/MacOS/$BIN" "$BIN_DIR/$BIN"

sed "s#__HOME__#$HOME#g" "$LABEL.plist" > "$PLIST"
plutil -lint "$PLIST" >/dev/null
launchctl bootstrap "$DOMAIN" "$PLIST"

echo "installed: $APP (signed: $(codesign -d -r- "$APP" 2>&1 | sed -n 's/^designated => //p'))"
echo "agent $LABEL loaded -> $APP/Contents/MacOS/$BIN; CLI: $BIN_DIR/$BIN -> bundle"
echo "log: $LOG_DIR/$BIN.log"
echo "permissions: see README.md \"Re-grant permissions\" (Input Monitoring prompt fires on first start)"
