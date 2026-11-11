#!/usr/bin/env bash
# ios/prepare: bind the release to clean source, prepare isolated App Store
# Connect authentication, run the GET-only preflight, import the existing
# distribution identity into a temporary keychain, and install the pinned
# existing profiles after verifying them. Never creates or replaces Apple assets.
#
# Inputs (environment): RELEASE_POLICY_INPUT, RELEASE_PLATFORM_INPUT, RELEASE_SHA_INPUT,
# XCODE_CANDIDATES, ASC_KEY_ID, ASC_ISSUER_ID, ASC_PRIVATE_KEY, DIST_CERT_P12,
# DIST_CERT_PASSWORD, KEYCHAIN_PASSWORD (optional), INSTALLER_CERT_P12 and
# INSTALLER_CERT_PASSWORD (Mac Catalyst only), RELEASE_SCRIPTS.
set -euo pipefail
umask 077
# shellcheck source=ios/scripts/lib.sh
. "$RELEASE_SCRIPTS/lib.sh"

[ "$(uname -s)" = Darwin ] || die 'ios/prepare must run on a macOS runner.'
require_vars RELEASE_POLICY_INPUT RELEASE_SHA_INPUT RELEASE_SCRIPTS ASC_KEY_ID ASC_ISSUER_ID ASC_PRIVATE_KEY DIST_CERT_P12 DIST_CERT_PASSWORD
PLATFORM="${RELEASE_PLATFORM_INPUT:-ios}"
case "$PLATFORM" in
  ios) ;;
  mac) require_vars INSTALLER_CERT_P12 INSTALLER_CERT_PASSWORD ;;
  *) die "Unknown platform: $PLATFORM (expected ios or mac)." ;;
esac
VERIFY=("python3" "$RELEASE_SCRIPTS/verify-release.py")
ASC=("node" "$RELEASE_SCRIPTS/asc-readonly.mjs")

echo '::group::Validate release policy'
[ -f "$RELEASE_POLICY_INPUT" ] || die "Release policy not found: $RELEASE_POLICY_INPUT"
POLICY_PATH="$(cd "$(dirname "$RELEASE_POLICY_INPUT")" && pwd)/$(basename "$RELEASE_POLICY_INPUT")"
persist_env RELEASE_POLICY "$POLICY_PATH"
persist_env RELEASE_PLATFORM "$PLATFORM"
PYTHONDONTWRITEBYTECODE=1 "${VERIFY[@]}" validate-policy --platform "$PLATFORM"
"${ASC[@]}" validate-policy --platform "$PLATFORM"
CERT_SHA1="$("${VERIFY[@]}" policy-value --key certificateSha1)"
echo '::endgroup::'

echo '::group::Select Xcode'
SELECTED=''
for candidate in $XCODE_CANDIDATES; do
  if [ -d "$candidate" ]; then
    SELECTED="$candidate"
    break
  fi
done
[ -n "$SELECTED" ] || die 'None of the Xcode candidates is installed.'
sudo xcode-select -s "$SELECTED/Contents/Developer"
echo "Selected: $SELECTED"
xcodebuild -version
echo '::endgroup::'

echo '::group::Bind release to clean source'
SHA="$RELEASE_SHA_INPUT"
BUILD_NUMBER="$(git rev-list --count HEAD)"
PYTHONDONTWRITEBYTECODE=1 "${VERIFY[@]}" prepare-source --sha "$SHA" --build "$BUILD_NUMBER"
persist_env RELEASE_SHA "$SHA"
persist_env RELEASE_BUILD_NUMBER "$BUILD_NUMBER"
persist_env IOS_BUILD_NUMBER "$BUILD_NUMBER"
persist_env CI_BUILD_NUMBER "$BUILD_NUMBER"
if [ "$PLATFORM" = mac ]; then persist_env MAC_BUILD_NUMBER "$BUILD_NUMBER"; fi
echo "Release $SHA build $BUILD_NUMBER ($PLATFORM)"
echo '::endgroup::'

echo '::group::Prepare isolated App Store Connect authentication'
RELEASE_TEMP="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/ios-release.XXXXXX")"
persist_env RELEASE_TEMP "$RELEASE_TEMP"
mkdir "$RELEASE_TEMP/private_keys"
# altool searches ./private_keys; the directory belongs to this run only.
KEY_PATH="$(python3 - <<'PY'
import base64, os, re
from pathlib import Path
key_id, issuer = os.environ['ASC_KEY_ID'], os.environ['ASC_ISSUER_ID']
if not re.fullmatch(r'[A-Za-z0-9]+', key_id):
    raise SystemExit('Invalid API key identifier.')
if not re.fullmatch(r'[A-Za-z0-9-]+', issuer):
    raise SystemExit('Invalid API issuer identifier.')
key = os.environ['ASC_PRIVATE_KEY'].strip().replace('\\n', '\n')
if 'BEGIN PRIVATE KEY' not in key:
    try:
        key = base64.b64decode(key, validate=True).decode()
    except Exception:
        raise SystemExit('Existing API key encoding is unsupported.')
if 'BEGIN PRIVATE KEY' not in key:
    raise SystemExit('Existing API key encoding is unsupported.')
path = Path(os.environ['RELEASE_TEMP']) / 'private_keys' / ('AuthKey_' + key_id + '.p8')
with path.open('x') as file:
    file.write(key.rstrip() + '\n')
print(path)
PY
)"
persist_env APP_STORE_CONNECT_API_KEY_PATH "$KEY_PATH"
persist_env APP_STORE_CONNECT_KEY_ID "$ASC_KEY_ID"
persist_env APP_STORE_CONNECT_ISSUER_ID "$ASC_ISSUER_ID"
echo '::endgroup::'

echo '::group::GET-only app, audience and build-number preflight'
"${ASC[@]}" preflight --platform "$PLATFORM" --sha "$SHA" --build "$BUILD_NUMBER"
echo '::endgroup::'

echo '::group::Import the existing distribution identity into a temporary keychain'
KEYCHAIN_PATH="$RELEASE_TEMP/signing.keychain-db"
persist_env KEYCHAIN_PATH "$KEYCHAIN_PATH"
if [ -z "${KEYCHAIN_PASSWORD:-}" ]; then
  KEYCHAIN_PASSWORD="$(openssl rand -hex 32)"
  echo "::add-mask::$KEYCHAIN_PASSWORD"
fi
printf '%s' "$DIST_CERT_P12" | base64 -d > "$RELEASE_TEMP/cert.p12"
# Public certificate only; checked against both policy pins before any import.
if ! openssl pkcs12 -in "$RELEASE_TEMP/cert.p12" -clcerts -nokeys -passin env:DIST_CERT_PASSWORD -out "$RELEASE_TEMP/certificate.pem" 2>/dev/null; then
  openssl pkcs12 -legacy -in "$RELEASE_TEMP/cert.p12" -clcerts -nokeys -passin env:DIST_CERT_PASSWORD -out "$RELEASE_TEMP/certificate.pem" 2>/dev/null \
    || die 'Distribution certificate bundle could not be opened.'
fi
openssl x509 -in "$RELEASE_TEMP/certificate.pem" -outform DER -out "$RELEASE_TEMP/certificate.der"
"${VERIFY[@]}" certificate --certificate-der "$RELEASE_TEMP/certificate.der"
persist_env RELEASE_CERTIFICATE_DER "$RELEASE_TEMP/certificate.der"
security list-keychains -d user > "$RELEASE_TEMP/original-keychains.txt"
security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"
security set-keychain-settings -lut 21600 "$KEYCHAIN_PATH"
security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"
python3 - "$KEYCHAIN_PATH" "$RELEASE_TEMP/original-keychains.txt" <<'PY'
import shlex, subprocess, sys
from pathlib import Path
subprocess.run(['security', 'list-keychains', '-d', 'user', '-s', sys.argv[1], *shlex.split(Path(sys.argv[2]).read_text())], check=True)
PY
security import "$RELEASE_TEMP/cert.p12" -k "$KEYCHAIN_PATH" -P "$DIST_CERT_PASSWORD" -T /usr/bin/codesign -T /usr/bin/security >/dev/null
if [ "$PLATFORM" = mac ]; then
  printf '%s' "$INSTALLER_CERT_P12" | base64 -d > "$RELEASE_TEMP/installer.p12"
  security import "$RELEASE_TEMP/installer.p12" -k "$KEYCHAIN_PATH" -P "$INSTALLER_CERT_PASSWORD" \
    -T /usr/bin/xcodebuild -T /usr/bin/productbuild -T /usr/bin/productsign -T /usr/bin/security >/dev/null
fi
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH" >/dev/null
security find-identity -v -p codesigning "$KEYCHAIN_PATH" | grep -Fq "$CERT_SHA1" \
  || die 'The imported identity is not the policy certificateSha1.'
if [ "$PLATFORM" = mac ]; then
  security find-identity -v "$KEYCHAIN_PATH" > "$RELEASE_TEMP/identities.txt"
  INSTALLER_SHA1="$("${VERIFY[@]}" installer-identity --identities "$RELEASE_TEMP/identities.txt")"
  persist_env INSTALLER_SHA1 "$INSTALLER_SHA1"
fi
echo '::endgroup::'

echo '::group::Fetch, verify and install the pinned existing profiles'
mkdir "$RELEASE_TEMP/profiles"
"${ASC[@]}" profiles --platform "$PLATFORM" --certificate-der "$RELEASE_TEMP/certificate.der" --directory "$RELEASE_TEMP/profiles"
"${VERIFY[@]}" profiles --platform "$PLATFORM" --directory "$RELEASE_TEMP/profiles" \
  --certificate-der "$RELEASE_TEMP/certificate.der" --destination "$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles"
echo '::endgroup::'
echo 'Prepared. Configure Info.plist values next, then run ios/upload.'
