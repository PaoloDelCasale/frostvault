#!/bin/sh
# Start as a configurable archive identity without ever changing /etc at
# runtime. Compose starts directly as numeric PUID:PGID; the image bakes in
# only the documented Unraid default (99:100). A root docker-run fallback uses
# gosu with numeric IDs, never runtime account creation. PUID=0 PGID=0 is an
# explicit opt-in (#679): root, but without the capabilities that bypass file
# permissions and with no-new-privileges, checked before anything starts.
# Do not chown vault sources.
set -eu

DEFAULT_PUID=99
DEFAULT_PGID=100
PUID="${PUID:-${DEFAULT_PUID}}"
PGID="${PGID:-${DEFAULT_PGID}}"

fail() {
  printf '%s\n' "ERROR: $*" >&2
  exit 64
}

validate_numeric_id() {
  variable_name="$1"
  value="$2"

  # Canonical decimal form avoids accepting signs, whitespace or octal-looking
  # values. 0 is accepted only as the explicit PUID=0 PGID=0 pair below.
  case "${value}" in
    0)
      return 0
      ;;
    ''|0*|*[!0-9]*)
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
if [ "${PUID}" = 0 ] || [ "${PGID}" = 0 ]; then
  if [ "${PUID}:${PGID}" != "0:0" ]; then
    fail "PUID and PGID must both be 0 to run as root, or both canonical positive decimal IDs from 1 through 2147483647 (for example PUID=1000 PGID=1000)."
  fi
fi
RUNTIME_IDENTITY="${PUID}:${PGID}"

is_web_command() {
  [ "${1:-}" = "uvicorn" ] || {
    [ "${1:-}" = "python" ] && [ "${2:-}" = "-m" ] && [ "${3:-}" = "app.server" ]
  }
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

if [ "${RUNTIME_IDENTITY}" = "0:0" ]; then
  # Root mode: no identity switch. Refuse to start while root still holds a
  # capability that bypasses file permissions or could gain one.
  assert_runtime_identity
  python -m app.runtime_privileges || exit 64
  if is_web_command "$@"; then
    assert_writable_runtime_directories
  fi
  exec "$@"
fi

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
if is_web_command "$@"; then
  assert_writable_runtime_directories
fi
exec "$@"
