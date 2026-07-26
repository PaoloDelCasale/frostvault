#!/bin/sh
# Drop from root to the configured archive identity (PUID/PGID).
# Defaults match Unraid's nobody:users (99:100). Do not chown vault sources.
# When starting uvicorn, optionally bring the DB schema to HEAD (AUTO_MIGRATE).
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
AUTO_MIGRATE="${AUTO_MIGRATE:-1}"

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

if [ "$(id -u)" -ne 0 ]; then
  maybe_migrate "$@"
  exec "$@"
fi

if ! getent group "${PGID}" >/dev/null 2>&1; then
  groupadd --system --gid "${PGID}" archive
fi
GROUP_NAME="$(getent group "${PGID}" | cut -d: -f1)"

if ! getent passwd "${PUID}" >/dev/null 2>&1; then
  useradd --system --uid "${PUID}" --gid "${GROUP_NAME}" \
    --home-dir /app --shell /usr/sbin/nologin --no-create-home archive
fi
USER_NAME="$(getent passwd "${PUID}" | cut -d: -f1)"

# Writable runtime dirs we own inside the container (tmpfs / host data volume).
# Never rewrite ownership or modes under /sources — operators must permission
# host vault directories explicitly for this UID/GID.
for path in /tmp /run /data; do
  if [ -d "${path}" ]; then
    chown "${USER_NAME}:${GROUP_NAME}" "${path}" || true
  fi
done

# Migrate as the runtime user (needs write access to the DB / backup volume),
# then hand off to the original CMD.
if [ "$#" -gt 0 ] && [ "$1" = "uvicorn" ] && auto_migrate_enabled; then
  echo "AUTO_MIGRATE: ensuring database schema is current before uvicorn"
  gosu "${USER_NAME}" python -m app.migrate_on_start
elif [ "$#" -gt 0 ] && [ "$1" = "uvicorn" ]; then
  echo "AUTO_MIGRATE disabled; skipping schema upgrade on start"
fi

exec gosu "${USER_NAME}" "$@"
