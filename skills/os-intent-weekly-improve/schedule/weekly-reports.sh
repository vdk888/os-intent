#!/bin/sh
# OS-intent weekly reports: step 1 of the os-intent-weekly-improve skill, also what the optional launchd job runs.
# Produces the two LOCAL reports and nothing else: it changes no setting, no form, no catalogue, sends nothing over
# the network, and never applies a change. `--notify` adds a macOS notification (osascript, local) at the end.
#
#   OI        the oi command (default: `oi` on the PATH)
#   OI_DATA_DIR  the data dir (default ~/.local/share/os-intent), as for oi itself
#
# Test days = the last 7 token days before today (UTC); train = up to 14 token days before them (needs >= 14 days).
# Only days that have a token file are passed (a missing day would stop the replay). 3-13 days: --test-last 2;
# fewer: skipped.
set -u
OI="${OI:-oi}"
D="${OI_DATA_DIR:-$HOME/.local/share/os-intent}"
OUT="$D/weekly-improve"
NOTIFY=0
[ "${1:-}" = "--notify" ] && NOTIFY=1

umask 077
mkdir -p "$OUT"
chmod 700 "$OUT"

"$OI" improve-report --last 7 || echo "weekly-reports: improve-report failed" >&2

REL="skipped (oi relevance-eval not available)"
if "$OI" relevance-eval --help >/dev/null 2>&1; then
    TODAY=$(date -u +%F)
    # shellcheck disable=SC2012  # file names are filtered to YYYY-MM-DD.jsonl below
    DAYS=$(ls "$D/tokens" 2>/dev/null | sed -n 's/^\([0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]\)\.jsonl$/\1/p' \
           | awk -v t="$TODAY" '$0 < t' | sort | tail -n 21)
    N=$(printf '%s\n' "$DAYS" | grep -c . || true)
    REL="skipped (only $N token days)"
    for TEST in 7 2; do
        MIN=$((TEST == 7 ? 14 : 3))     # 7 test days only with at least 7 train days
        if [ "$N" -ge "$MIN" ]; then
            # shellcheck disable=SC2086  # one argument per day
            if "$OI" relevance-eval --days $DAYS --test-last "$TEST" --alpha 1,3,10 --depth o1h,o2hs \
                   --starters --proactive --json > "$OUT/relevance-latest.json.tmp"; then
                mv "$OUT/relevance-latest.json.tmp" "$OUT/relevance-latest.json"
                REL="$OUT/relevance-latest.json (test days: $TEST)"
                break
            fi
            rm -f "$OUT/relevance-latest.json.tmp"
            REL="failed (see above)"
        fi
    done
fi
echo "relevance: $REL"

if [ "$NOTIFY" = 1 ]; then
    osascript -e 'display notification "Weekly reports are ready. Ask your agent to run the OS-intent weekly improvement." with title "OS-intent"' \
        >/dev/null 2>&1 || true
fi
exit 0
