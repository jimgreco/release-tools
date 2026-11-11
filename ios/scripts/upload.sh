#!/usr/bin/env bash
# ios/upload: verify source, archive with policy-owned signing settings, export
# with generated ExportOptions, verify the signed artifact, recheck Apple state,
# then perform the single Apple write (altool upload) and verify processing.
#
# Inputs (environment): RELEASE_SCRIPTS, XCODE_PROJECT or XCODE_WORKSPACE, XCODE_SCHEME,
# XCODE_CONFIGURATION, RELEASE_EXTRA_BUILD_SETTINGS, plus everything ios/prepare exported.
set -euo pipefail
umask 077
# shellcheck source=ios/scripts/lib.sh
. "$RELEASE_SCRIPTS/lib.sh"

require_vars RELEASE_POLICY RELEASE_PLATFORM RELEASE_SHA RELEASE_BUILD_NUMBER RELEASE_TEMP KEYCHAIN_PATH \
  RELEASE_CERTIFICATE_DER APP_STORE_CONNECT_KEY_ID APP_STORE_CONNECT_ISSUER_ID APP_STORE_CONNECT_API_KEY_PATH XCODE_SCHEME
if { [ -n "${XCODE_PROJECT:-}" ] && [ -n "${XCODE_WORKSPACE:-}" ]; } || { [ -z "${XCODE_PROJECT:-}" ] && [ -z "${XCODE_WORKSPACE:-}" ]; }; then
  die 'Provide exactly one of project or workspace.'
fi
PLATFORM="$RELEASE_PLATFORM"
SHA="$RELEASE_SHA"
BUILD="$RELEASE_BUILD_NUMBER"
VERIFY=("python3" "$RELEASE_SCRIPTS/verify-release.py")
ASC=("node" "$RELEASE_SCRIPTS/asc-readonly.mjs")
case "$PLATFORM" in
  ios) EXTENSION=mobileprovision ARTIFACT_GLOB='*.ipa' ALTOOL_TYPE=ios ;;
  mac) EXTENSION=provisionprofile ARTIFACT_GLOB='*.pkg' ALTOOL_TYPE=macos; require_vars INSTALLER_SHA1 ;;
  *) die "Unknown platform: $PLATFORM" ;;
esac

echo '::group::Verify configured source'
"${VERIFY[@]}" source --sha "$SHA" --build "$BUILD"
echo '::endgroup::'

echo '::group::Archive using only verified existing assets'
ARCHIVE="$RELEASE_TEMP/App-$PLATFORM.xcarchive"
python3 "$RELEASE_SCRIPTS/archive.py" --platform "$PLATFORM" --project "${XCODE_PROJECT:-}" --workspace "${XCODE_WORKSPACE:-}" \
  --scheme "$XCODE_SCHEME" --configuration "${XCODE_CONFIGURATION:-Release}" --archive-path "$ARCHIVE" \
  --keychain "$KEYCHAIN_PATH" --build "$BUILD" --sha "$SHA"
echo '::endgroup::'

echo '::group::Export without provisioning updates'
EXPORT_OPTIONS="$RELEASE_TEMP/ExportOptions-$PLATFORM.plist"
if [ "$PLATFORM" = mac ]; then
  "${VERIFY[@]}" export-options --platform mac --installer-sha1 "$INSTALLER_SHA1" --output "$EXPORT_OPTIONS"
else
  "${VERIFY[@]}" export-options --output "$EXPORT_OPTIONS"
fi
EXPORT_DIR="$RELEASE_TEMP/export-$PLATFORM"
xcodebuild -exportArchive -archivePath "$ARCHIVE" -exportOptionsPlist "$EXPORT_OPTIONS" -exportPath "$EXPORT_DIR"
ARTIFACT="$(python3 - "$EXPORT_DIR" "$ARTIFACT_GLOB" <<'PY'
import sys
from pathlib import Path
files = list(Path(sys.argv[1]).glob(sys.argv[2]))
if len(files) != 1:
    raise SystemExit('Expected exactly one exported artifact.')
print(files[0])
PY
)"
persist_env RELEASE_ARTIFACT_PATH "$ARTIFACT"
echo '::endgroup::'

echo '::group::Verify signed artifact and recheck current Apple scope'
if [ "$PLATFORM" = mac ]; then
  "${VERIFY[@]}" artifact --platform mac --pkg "$ARTIFACT" --sha "$SHA" --build "$BUILD"
else
  "${VERIFY[@]}" artifact --ipa "$ARTIFACT" --sha "$SHA" --build "$BUILD"
fi
# Recheck revocation/profile state immediately before the only Apple write.
FINAL="$RELEASE_TEMP/final-profiles-$PLATFORM"
mkdir "$FINAL"
"${ASC[@]}" profiles --platform "$PLATFORM" --certificate-der "$RELEASE_CERTIFICATE_DER" --directory "$FINAL"
for path in "$RELEASE_TEMP/profiles/"*."$EXTENSION"; do
  cmp "$path" "$FINAL/$(basename "$path")"
done
for path in "$FINAL/"*."$EXTENSION"; do
  [ -f "$RELEASE_TEMP/profiles/$(basename "$path")" ] || die 'Profile set changed since prepare.'
done
"${ASC[@]}" preflight --platform "$PLATFORM" --sha "$SHA" --build "$BUILD"
echo '::endgroup::'

echo '::group::Upload one verified build'
cd "$RELEASE_TEMP"
set +e
xcrun altool --upload-app --type "$ALTOOL_TYPE" --file "$ARTIFACT" \
  --apiKey "$APP_STORE_CONNECT_KEY_ID" --apiIssuer "$APP_STORE_CONNECT_ISSUER_ID" > "$RELEASE_TEMP/upload.log" 2>&1
ALTOOL_STATUS=$?
set -e
cat "$RELEASE_TEMP/upload.log"
bash "$RELEASE_SCRIPTS/check-upload-log.sh" "$RELEASE_TEMP/upload.log" "$ALTOOL_STATUS"
cd - >/dev/null
echo '::endgroup::'

echo '::group::Verify processing and approved internal group availability'
"${ASC[@]}" verify-upload --platform "$PLATFORM" --sha "$SHA" --build "$BUILD"
echo '::endgroup::'
