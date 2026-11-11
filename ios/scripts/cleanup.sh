#!/usr/bin/env bash
# ios/cleanup: restore the keychain search list, delete this run's temporary
# keychain, and remove this run's private signing and API key files.
# Safe to run when prepare failed part way (callers use `if: always()`).
set -uo pipefail
# shellcheck source=ios/scripts/lib.sh
. "$RELEASE_SCRIPTS/lib.sh"

status=0
if [ -n "${RELEASE_TEMP:-}" ] && [ -d "$RELEASE_TEMP" ]; then
  restore_keychain_list "$RELEASE_TEMP/original-keychains.txt" || status=1
fi
if [ -n "${KEYCHAIN_PATH:-}" ] && [ -f "$KEYCHAIN_PATH" ]; then
  security delete-keychain "$KEYCHAIN_PATH" || status=1
fi
if [ -n "${RELEASE_TEMP:-}" ] && [ -d "$RELEASE_TEMP" ]; then
  rm -f "$RELEASE_TEMP/cert.p12" "$RELEASE_TEMP/installer.p12" "$RELEASE_TEMP"/private_keys/*.p8 "$RELEASE_TEMP"/AuthKey_*.p8 || status=1
fi
exit "$status"
