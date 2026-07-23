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


def guard(connection: Any, *, scope: str, key: str) -> None:
    """Raise :class:`BackoffError` while ``(scope, key)`` is still blocked."""
    row = _load(connection, scope, key)
    if not row:
        return
    now = _now()
    if _is_decayed(row, now):
        return
    if not row["next_allowed_at"]:
        return
    next_allowed = _parse(row["next_allowed_at"])
    if now < next_allowed:
        raise BackoffError(math.ceil((next_allowed - now).total_seconds()))


def record_failure(connection: Any, *, scope: str, key: str) -> None:
    """Count one failure and extend the backoff once the threshold is reached."""
    now = _now()
    row = _load(connection, scope, key)
    if row and not _is_decayed(row, now):
        failure_count = int(row["failure_count"]) + 1
    else:
        failure_count = 1
    delay = _backoff_seconds(failure_count)
    next_allowed_at = (
        (now + timedelta(seconds=delay)).isoformat() if delay else None
    )
    connection.execute(
        """
        INSERT INTO auth_backoff(scope, key, failure_count, next_allowed_at, updated_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT(scope, key) DO UPDATE SET
            failure_count=excluded.failure_count,
            next_allowed_at=excluded.next_allowed_at,
            updated_at=excluded.updated_at
        """,
        (scope, key, failure_count, next_allowed_at, now.isoformat()),
    )


def record_success(connection: Any, *, scope: str, key: str) -> None:
    """Clear any accumulated backoff for ``(scope, key)`` after a success."""
    connection.execute(
        "DELETE FROM auth_backoff WHERE scope=%s AND key=%s",
        (scope, key),
    )
