#!/usr/bin/env bash
# Load the oi-live LaunchAgent (`oi live`: Markov predictions on the HUD, W8). No build: it runs
# <repo>/.venv/bin/oi from the checkout this script sits in.
# Idempotent: re-running reinstalls the plist and restarts the driver.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(cd .. && pwd)"
LABEL=com.osintent.oi-live
OI="$REPO/.venv/bin/oi"
LOG_DIR="$HOME/.local/share/os-intent/logs"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

[ -x "$OI" ] || { echo "missing $OI: create the venv first (python3.12 -m venv .venv && .venv/bin/pip install -e .)" >&2; exit 1; }
"$OI" live --help >/dev/null || { echo "$OI has no 'live' command: reinstall the package (pip install -e .)" >&2; exit 1; }
mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
chmod 700 "$HOME/.local/share/os-intent"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
sed -e "s#__HOME__#$HOME#g" -e "s#__REPO__#$REPO#g" "$LABEL.plist" > "$PLIST"
plutil -lint "$PLIST" >/dev/null
launchctl bootstrap "$DOMAIN" "$PLIST"

echo "installed: agent $LABEL loaded ($OI live)"
echo "log: $LOG_DIR/oi-live.log · status: ~/.local/share/os-intent/status/oi-live.json · hud-log/: every shown moment"
