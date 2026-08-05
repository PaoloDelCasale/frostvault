#!/bin/sh
# Start as a configurable non-root archive identity without ever changing
# /etc at runtime. Compose starts directly as numeric PUID:PGID; the image
# bakes in only the documented Unraid default (99:100). A root docker-run
# fallback uses gosu with numeric IDs, never runtime account creation.
# Do not chown vault sources.
set -eu

DEFAULT_PUID=99
DEFAULT_PGID=100
PUID="${PUID:-${DEFAULT_PUID}}"
PGID="${PGID:-${DEFAULT_PGID}}"
AUTO_MIGRATE="${AUTO_MIGRATE:-1}"

fail() {
  printf '%s\n' "ERROR: $*" >&2
  exit 64
}

validate_numeric_id() {
  variable_name="$1"
  value="$2"

  # Canonical decimal form avoids accepting signs, whitespace, octal-looking
  # values, or root. Root would defeat the container's non-root contract.
  case "${value}" in
    ''|0|0*|*[!0-9]*)
      fail "${variable_name} must be a canonical positive decimal ID from 1 through 2147483647. Set PUID and PGID to matching non-root host IDs (for example PUID=1000 PGID=1000)."
      ;;
  esac

  if ! python - "${value}" <<'PY'
import sys

try:
    value = int(sys.argv[1], 10)
except ValueError:
    raise SystemExit(1)
raise SystemExit(not 1 <= value <= 2_147_483_647)
PY
  then
    fail "${variable_name} must be a canonical positive decimal ID from 1 through 2147483647. Set PUID and PGID to matching non-root host IDs (for example PUID=1000 PGID=1000)."
  fi
}

validate_numeric_id "PUID" "${PUID}"
validate_numeric_id "PGID" "${PGID}"
RUNTIME_IDENTITY="${PUID}:${PGID}"

auto_migrate_enabled() {
  case "$(printf '%s' "${AUTO_MIGRATE}" | tr '[:upper:]' '[:lower:]')" in
    0|false|no|off) return 1 ;;
    *) return 0 ;;
  esac
}

maybe_migrate() {
  # Only when launching the web app. One-shot admin commands (alembic,
  # backup_upgrade, shell) keep full control of schema timing.
  if [ "$#" -eq 0 ] || [ "$1" != "uvicorn" ]; then
    return 0
  fi
  if ! auto_migrate_enabled; then
    echo "AUTO_MIGRATE disabled; skipping schema upgrade on start"
    return 0
  fi
  echo "AUTO_MIGRATE: ensuring database schema is current before uvicorn"
  python -m app.migrate_on_start
}

assert_runtime_identity() {
  current_identity="$(id -u):$(id -g)"
  if [ "${current_identity}" != "${RUNTIME_IDENTITY}" ]; then
    fail "container is running as ${current_identity}, but PUID:PGID is ${RUNTIME_IDENTITY}. Start it with --user ${RUNTIME_IDENTITY}, or use the supplied Compose manifests."
  fi
}

assert_writable_runtime_directories() {
  for path in /tmp /run /data; do
    if [ ! -d "${path}" ] || [ ! -w "${path}" ] || [ ! -x "${path}" ]; then
      fail "${path} must be a writable directory for PUID:PGID ${RUNTIME_IDENTITY}. With a read-only root filesystem, mount /data read-write and mount /tmp and /run as tmpfs owned by that identity."
    fi
  done
}

if [ "$(id -u)" -eq 0 ]; then
  # A direct docker run normally retains the capabilities needed for gosu and
  # can prepare mounted runtime directories. The hardened Compose manifests
  # instead set user: PUID:PGID because cap_drop: ALL removes those privileges.
  if ! gosu "${RUNTIME_IDENTITY}" true >/dev/null 2>&1; then
    fail "cannot switch from root to ${RUNTIME_IDENTITY}; CAP_SETUID/CAP_SETGID are unavailable. With cap_drop: ALL, start the container with --user ${RUNTIME_IDENTITY}; the supplied Compose manifests already do this."
  fi

  # Only writable runtime mounts may be prepared. Never change ownership or
  # modes under /sources; operators must permission Source Volumes explicitly.
  for path in /tmp /run /data; do
    if [ -d "${path}" ]; then
      chown "${RUNTIME_IDENTITY}" "${path}" 2>/dev/null || true
    fi
  done

  exec gosu "${RUNTIME_IDENTITY}" "$0" "$@"
fi

assert_runtime_identity
if [ "$#" -gt 0 ] && [ "$1" = "uvicorn" ]; then
  assert_writable_runtime_directories
fi
maybe_migrate "$@"
exec "$@"
