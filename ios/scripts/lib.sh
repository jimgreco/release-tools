#!/usr/bin/env bash
# Shared helpers for the iOS release composite actions. Sourced, not executed.
# Bash 3.2 compatible (macOS /bin/bash). Never echo secret values.

die() {
  echo "::error::$*" >&2
  exit 1
}

# Export NAME=VALUE in this shell and for later workflow steps.
persist_env() {
  local name="$1" value="$2"
  case "$value" in
    *$'\n'* | *$'\r'*) die "Refusing multi-line value for $name." ;;
  esac
  export "$name=$value"
  if [ -n "${GITHUB_ENV:-}" ]; then
    printf '%s=%s\n' "$name" "$value" >> "$GITHUB_ENV"
  fi
}

require_vars() {
  local name
  for name in "$@"; do
    [ -n "${!name:-}" ] || die "Missing required release input: $name"
  done
}

# Restore the user keychain search list saved by prepare.
restore_keychain_list() {
  local saved="$1"
  [ -f "$saved" ] || return 0
  python3 - "$saved" <<'PY'
import shlex, subprocess, sys
from pathlib import Path
subprocess.run(['security', 'list-keychains', '-d', 'user', '-s', *shlex.split(Path(sys.argv[1]).read_text())], check=True)
PY
}
