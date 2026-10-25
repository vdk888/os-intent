#!/usr/bin/env bash
# Create, ONCE, the self-signed code-signing identity "OS-intent Dev" in the login
# keychain, so the OS-intent .app bundles keep the SAME designated requirement across rebuilds
# (TCC keys its Input Monitoring / Accessibility / Automation grants on it; ad-hoc signatures
# change on every build and silently drop the grants).
#
# Idempotent: exits 0 without touching anything when the identity already exists.
# Creates exactly two keychain items in ~/Library/Keychains/login.keychain-db:
#   - certificate "OS-intent Dev" (RSA 2048, self-signed, EKU codeSigning, 10 years)
#   - its private key (ACL: /usr/bin/codesign may use it; any other app asks)
# The certificate is NOT added to any trust store: codesign signs with an untrusted
# self-signed identity, and TCC only needs the requirement (identifier + certificate hash).
# Remove it with:  security delete-identity -c "OS-intent Dev" ~/Library/Keychains/login.keychain-db
#
# First codesign with the new key may show a keychain dialog ("codesign wants to use key
# ..."): click "Always Allow" once.
set -euo pipefail

NAME="${OI_SIGN_IDENTITY:-OS-intent Dev}"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
OPENSSL=/usr/bin/openssl   # LibreSSL: its PKCS#12 (3DES/SHA1) is what `security import` reads

sha1_of() {   # SHA-1 of every certificate named exactly $1 in the login keychain
    security find-certificate -a -c "$1" -Z "$KEYCHAIN" 2>/dev/null | awk '/^SHA-1 hash:/ {print $3}'
}

existing=$(sha1_of "$NAME")
if [ -n "$existing" ]; then
    if [ "$(printf '%s\n' "$existing" | wc -l | tr -d ' ')" -gt 1 ]; then
        echo "error: several certificates named \"$NAME\" in the login keychain:" >&2
        printf '%s\n' "$existing" | sed 's/^/  /' >&2
        echo "keep one (security delete-certificate -Z <sha1>) and rerun" >&2
        exit 1
    fi
    echo "identity present: \"$NAME\" SHA-1 $existing (nothing created)"
    exit 0
fi

TMP=$(mktemp -d "${TMPDIR:-/tmp}/oi-signid.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
chmod 700 "$TMP"

cat > "$TMP/req.cnf" <<CNF
[req]
distinguished_name = dn
x509_extensions    = ext
prompt             = no
[dn]
CN = $NAME
O  = Acme
OU = OS-intent
[ext]
basicConstraints       = critical, CA:false
keyUsage               = critical, digitalSignature
extendedKeyUsage       = critical, codeSigning
subjectKeyIdentifier   = hash
CNF

"$OPENSSL" req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
    -config "$TMP/req.cnf" -keyout "$TMP/key.pem" -out "$TMP/cert.pem" 2>/dev/null
PW=$("$OPENSSL" rand -hex 24)   # throwaway: protects the .p12 for the few ms it exists
"$OPENSSL" pkcs12 -export -name "$NAME" -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
    -out "$TMP/id.p12" -passout "pass:$PW"
security import "$TMP/id.p12" -k "$KEYCHAIN" -f pkcs12 -P "$PW" -T /usr/bin/codesign >/dev/null

sha=$(sha1_of "$NAME")
[ -n "$sha" ] || { echo "error: import reported success but \"$NAME\" is not in $KEYCHAIN" >&2; exit 1; }
echo "created in $KEYCHAIN:"
echo "  certificate \"$NAME\" (self-signed, EKU codeSigning, 10 years) SHA-1 $sha"
echo "  private key \"$NAME\" (ACL: /usr/bin/codesign)"
echo "temp files removed. First codesign may ask for keychain access: click Always Allow."
