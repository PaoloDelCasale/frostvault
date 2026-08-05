from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any


# Throttle counters begin backing off only after this many consecutive failures.
THRESHOLD = 5
# The first backoff after the threshold, doubling on each further failure.
BASE_SECONDS = 30
# The backoff never grows past this ceiling: there is no permanent lockout.
CAP_SECONDS = 15 * 60
# A quiet period this long makes a key forget its accumulated failures.
DECAY_SECONDS = 60 * 60

# ``auth_backoff`` deliberately has only the stable ``ip`` and ``account``
# dimensions. Reauthentication uses namespaced keys inside those dimensions so
# its counters persist independently from Local Sign-in and Invite counters.
_REAUTH_IP_KEY_PREFIX = "reauth:ip:"
_REAUTH_ACCOUNT_KEY_PREFIX = "reauth:account:"


def reauth_ip_key(client_ip: str) -> str:
    """Return the Local Reauthentication-only IP counter key."""
    return f"{_REAUTH_IP_KEY_PREFIX}{client_ip}"


def reauth_account_key(user_id: int) -> str:
    """Return the Local Reauthentication-only account counter key."""
    return f"{_REAUTH_ACCOUNT_KEY_PREFIX}{user_id}"


class BackoffError(Exception):
    """Raised when a throttled ``(scope, key)`` must wait before retrying."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Throttled; retry after {retry_after}s")
        self.retry_after = retry_after


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _load(connection: Any, scope: str, key: str) -> dict[str, Any] | None:
    return connection.execute(
        "SELECT * FROM auth_backoff WHERE scope=%s AND key=%s",
        (scope, key),
    ).fetchone()


def _is_decayed(row: dict[str, Any], now: datetime) -> bool:
    return now - _parse(row["updated_at"]) >= timedelta(seconds=DECAY_SECONDS)


def _backoff_seconds(failure_count: int) -> int:
    if failure_count < THRESHOLD:
        return 0
    delay = BASE_SECONDS * (2 ** (failure_count - THRESHOLD))
    return min(delay, CAP_SECONDS)


def _next_allowed(row: dict[str, Any]) -> datetime | None:
    """Read a stored deadline, or derive it after an interrupted write."""
    if row["next_allowed_at"]:
        return _parse(row["next_allowed_at"])
    delay = _backoff_seconds(int(row["failure_count"]))
    if not delay:
        return None
    return _parse(row["updated_at"]) + timedelta(seconds=delay)


def guard(connection: Any, *, scope: str, key: str) -> None:
    """Raise :class:`BackoffError` while ``(scope, key)`` is still blocked."""
    row = _load(connection, scope, key)
    if not row:
        return
    now = _now()
    if _is_decayed(row, now):
        return
    next_allowed = _next_allowed(row)
    if next_allowed and now < next_allowed:
        raise BackoffError(math.ceil((next_allowed - now).total_seconds()))


def record_failure(connection: Any, *, scope: str, key: str) -> int:
    """Atomically count one failure and return the committed counter value.

    The ``ON CONFLICT ... DO UPDATE`` is a write on the conflicting row, so
    SQLite serializes it and PostgreSQL holds that row lock for the enclosing
    transaction. The resulting count is returned by the same statement rather
    than being calculated from a stale Python-side read.
    """
    now = _now()
    now_iso = now.isoformat()
    decay_cutoff = (now - timedelta(seconds=DECAY_SECONDS)).isoformat()
    row = connection.execute(
        """
        INSERT INTO auth_backoff(scope, key, failure_count, next_allowed_at, updated_at)
        VALUES (%s, %s, 1, NULL, %s)
        ON CONFLICT(scope, key) DO UPDATE SET
            failure_count=CASE
                WHEN updated_at <= %s THEN 1
                ELSE failure_count + 1
            END,
            next_allowed_at=NULL,
            updated_at=excluded.updated_at
        RETURNING failure_count
        """,
        (scope, key, now_iso, decay_cutoff),
    ).fetchone()
    failure_count = int(row["failure_count"])
    delay = _backoff_seconds(failure_count)
    next_allowed_at = (
        (now + timedelta(seconds=delay)).isoformat() if delay else None
    )
    # The UPSERT above holds the row lock in normal transaction-backed callers.
    # Matching the returned counter also prevents an autocommit caller from
    # overwriting a newer concurrent failure's longer backoff.
    connection.execute(
        """
        UPDATE auth_backoff
        SET next_allowed_at=%s
        WHERE scope=%s AND key=%s AND failure_count=%s AND updated_at=%s
        """,
        (next_allowed_at, scope, key, failure_count, now_iso),
    )
    return failure_count


def record_success(connection: Any, *, scope: str, key: str) -> None:
    """Clear any accumulated backoff for ``(scope, key)`` after a success."""
    connection.execute(
        "DELETE FROM auth_backoff WHERE scope=%s AND key=%s",
        (scope, key),
    )
