#!/usr/bin/env bash
# Load the oi-laya LaunchAgent (W13): `oi laya-serve`, one laya server on 127.0.0.1:8781 under the
# memory rule. LAZY: loading does not start it; `oi live` kickstarts it when shadow mode needs it
# (settings.json live.laya_shadow = true). Run it yourself once OI_LAYA_DIR points to your Laya checkout.
# Idempotent: re-running reinstalls the plist.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(cd .. && pwd)"
LABEL=com.osintent.oi-laya
OI="$REPO/.venv/bin/oi"
LAYA_SERVE="${OI_LAYA_DIR:-$HOME/laya-local}/laya/.venv/bin/laya-serve"
LOG_DIR="$HOME/.local/share/os-intent/logs"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

[ -x "$OI" ] || { echo "missing $OI: create the venv first (python3.12 -m venv .venv && .venv/bin/pip install -e .)" >&2; exit 1; }
"$OI" laya-serve --help >/dev/null || { echo "$OI has no 'laya-serve' command: reinstall the package (pip install -e .)" >&2; exit 1; }
[ -x "$LAYA_SERVE" ] || { echo "missing $LAYA_SERVE (set OI_LAYA_DIR to your Laya checkout)" >&2; exit 1; }
mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
chmod 700 "$HOME/.local/share/os-intent"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
sed -e "s#__HOME__#$HOME#g" -e "s#__REPO__#$REPO#g" "$LABEL.plist" > "$PLIST"
plutil -lint "$PLIST" >/dev/null
launchctl bootstrap "$DOMAIN" "$PLIST"

echo "installed: agent $LABEL loaded, not started (lazy: oi live kickstarts it in shadow mode)"
echo "start by hand: launchctl kickstart $DOMAIN/$LABEL · check: $OI laya-serve --check"
echo "log: $LOG_DIR/oi-laya.log · status: ~/.local/share/os-intent/status/oi-laya.json"
