#!/usr/bin/env bash
# Decide whether an altool upload succeeded from its exit status and log.
# Usage: check-upload-log.sh <upload.log> <altool-exit-status>
# Any known failure marker wins over a success marker; success must be explicit.
set -euo pipefail
log="$1"
status="$2"
[ "$status" = 0 ] || { echo "::error::altool exited with status $status." >&2; exit 1; }
if grep -Eiq 'NOT_AUTHORIZED|failed to authenticate|received status code 401|UPLOAD FAILED|Validation failed|Failed to upload package|Failed to determine the Apple ID' "$log"; then
  echo '::error::altool reported an upload failure.' >&2
  exit 1
fi
if ! grep -Eiq 'UPLOAD SUCCEEDED|successfully uploaded' "$log"; then
  echo '::error::altool did not report a successful upload.' >&2
  exit 1
fi
echo 'Upload succeeded.'
