#!/bin/sh
# Drop from root to the configured archive identity (PUID/PGID).
# Defaults match Unraid's nobody:users (99:100). Do not chown vault sources.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"

if [ "$(id -u)" -ne 0 ]; then
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

exec gosu "${USER_NAME}" "$@"
