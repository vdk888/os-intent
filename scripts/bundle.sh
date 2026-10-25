#!/usr/bin/env bash
# Build a SwiftPM executable (release) and wrap it in a minimal, signed .app bundle:
#   <out-dir>/<display-name>.app/Contents/{Info.plist, MacOS/<bin>}
# Why a bundle: TCC prompts, `tccutil reset <service> <bundle-id>` and the "+" picker in
# System Settings only work for apps; a bare executable in ~/.local/bin gets none of them.
# Why the stable identity: TCC keys grants on the designated requirement
# (identifier + certificate root hash), which an ad-hoc signature changes on every build.
#
# usage: bundle.sh <package-dir> <bundle-id> <display-name> <out-dir>
#   <bin> = last dot-component of <bundle-id> (com.osintent.oi-watcher -> oi-watcher),
#           override with OI_BIN=<product>.
#   Info.plist starts from <package-dir>/Support/Info.plist when present (keeps the
#   package's usage-description strings), then the bundle keys below are set.
#   Signing identity: OI_SIGN_IDENTITY (default "OS-intent Dev", created by
#   make-signing-identity.sh). OI_SIGN_IDENTITY=- signs ad-hoc (tests only: TCC grants
#   will NOT survive a rebuild).
# Prints the .app path on the last line of stdout.
set -euo pipefail

[ $# -eq 4 ] || { echo "usage: $0 <package-dir> <bundle-id> <display-name> <out-dir>" >&2; exit 2; }
PKG=$(cd "$1" && pwd)
BUNDLE_ID=$2
NAME=$3
OUT=$4
BIN=${OI_BIN:-${BUNDLE_ID##*.}}
IDENTITY=${OI_SIGN_IDENTITY:-OS-intent Dev}
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

# Resolve the identity to its SHA-1 so codesign never picks an ambiguous match.
if [ "$IDENTITY" = "-" ]; then
    SIGN=-
else
    SIGN=$(security find-certificate -a -c "$IDENTITY" -Z "$KEYCHAIN" 2>/dev/null | awk '/^SHA-1 hash:/ {print $3}')
    if [ -z "$SIGN" ]; then
        echo "error: no signing identity \"$IDENTITY\"; run $(dirname "$0")/make-signing-identity.sh" >&2
        exit 1
    fi
    if [ "$(printf '%s\n' "$SIGN" | wc -l | tr -d ' ')" -gt 1 ]; then
        echo "error: several certificates named \"$IDENTITY\"; keep one" >&2
        exit 1
    fi
fi

swift build -c release --package-path "$PKG" --product "$BIN" >&2
BUILT="$(swift build -c release --package-path "$PKG" --show-bin-path)/$BIN"
[ -x "$BUILT" ] || { echo "error: $BUILT not built" >&2; exit 1; }

REPO_DIR=$(git -C "$PKG" rev-parse --show-toplevel 2>/dev/null || true)
BUILD_NO=$( [ -n "$REPO_DIR" ] && git -C "$PKG" rev-list --count HEAD 2>/dev/null || echo 1)
COMMIT=$( [ -n "$REPO_DIR" ] && git -C "$PKG" rev-parse --short HEAD 2>/dev/null || echo unknown)

mkdir -p "$OUT"
APP="$OUT/$NAME.app"
STAGE="$OUT/.$NAME.app.staging.$$"
rm -rf "$STAGE"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/Contents/MacOS"
install -m 755 "$BUILT" "$STAGE/Contents/MacOS/$BIN"

PL="$STAGE/Contents/Info.plist"
if [ -f "$PKG/Support/Info.plist" ]; then
    cp "$PKG/Support/Info.plist" "$PL"
else
    plutil -create xml1 "$PL"
fi
setstr() { plutil -replace "$1" -string "$2" "$PL"; }
setstr CFBundleIdentifier "$BUNDLE_ID"
setstr CFBundleName "$NAME"
setstr CFBundleDisplayName "$NAME"
setstr CFBundleExecutable "$BIN"
setstr CFBundlePackageType APPL
setstr CFBundleInfoDictionaryVersion 6.0
setstr CFBundleShortVersionString 0.1.0
setstr CFBundleVersion "$BUILD_NO"
setstr OIGitCommit "$COMMIT"
setstr LSMinimumSystemVersion 14.0
setstr NSHumanReadableCopyright "© Acme — OS-intent (local only, nothing leaves this Mac)"
plutil -replace LSUIElement -bool true "$PL"
plutil -lint "$PL" >/dev/null

codesign --force --deep --sign "$SIGN" --identifier "$BUNDLE_ID" --timestamp=none "$STAGE" >&2
codesign --verify --strict "$STAGE" >&2

rm -rf "$APP"
mv "$STAGE" "$APP"
trap - EXIT
codesign -d -r- "$APP" 2>&1 | sed -n 's/^designated => /designated requirement: /p' >&2
echo "$APP"
