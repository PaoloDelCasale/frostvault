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
# its counters persist independently from sign-in counters.
_REAUTH_IP_KEY_PREFIX = "reauth:ip:"
_REAUTH_ACCOUNT_KEY_PREFIX = "reauth:account:"
# The single Owner is the only account: every Emergency Password sign-in
# attempt counts against it, whatever username a client submits.
OWNER_ACCOUNT_KEY = "owner"


def reauth_ip_key(client_ip: str) -> str:
    """Return the Local Reauthentication-only IP counter key."""
    return f"{_REAUTH_IP_KEY_PREFIX}{client_ip}"


def reauth_account_key() -> str:
    """Return the Owner's Local Reauthentication-only account counter key."""
    return f"{_REAUTH_ACCOUNT_KEY_PREFIX}owner"


class BackoffError(Exception):
    """Raised when a throttled ``(scope, key)`` must wait before retrying."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Throttled; retry after {retry_after}s")
        self.retry_after = retry_after


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load(connection: Any, scope: str, key: str) -> dict[str, Any] | None:
    return connection.execute(
        "SELECT * FROM auth_backoff WHERE scope=%s AND key=%s",
        (scope, key),
    ).fetchone()


def _is_decayed(row: dict[str, Any], now: datetime) -> bool:
    return now - row["updated_at"] >= timedelta(seconds=DECAY_SECONDS)


def _backoff_seconds(failure_count: int) -> int:
    if failure_count < THRESHOLD:
        return 0
    delay = BASE_SECONDS * (2 ** (failure_count - THRESHOLD))
    return min(delay, CAP_SECONDS)


def _next_allowed(row: dict[str, Any]) -> datetime | None:
    """Read a stored deadline, or derive it after an interrupted write."""
    if row["next_allowed_at"]:
        return row["next_allowed_at"]
    delay = _backoff_seconds(int(row["failure_count"]))
    if not delay:
        return None
    return row["updated_at"] + timedelta(seconds=delay)


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


def record_failure(
    connection: Any, *, scope: str, key: str
) -> tuple[int, datetime | None]:
    """Atomically count one failure and return its committed count and deadline.

    The ``ON CONFLICT ... DO UPDATE`` is a write on the conflicting row, so
    PostgreSQL holds that row lock for the enclosing transaction. Both the incremented count and the matching deadline are
    returned by the same statement rather than being calculated from a stale
    Python-side read.
    """
    now = _now()
    decay_cutoff = now - timedelta(seconds=DECAY_SECONDS)
    # The final value is the capped deadline for every count beyond this range.
    deadline_values = tuple(
        now + timedelta(seconds=_backoff_seconds(count))
        for count in range(THRESHOLD, THRESHOLD + 7)
    )
    row = connection.execute(
        """
        INSERT INTO auth_backoff(scope, key, failure_count, next_allowed_at, updated_at)
        VALUES (%s, %s, 1, NULL, %s)
        ON CONFLICT(scope, key) DO UPDATE SET
            failure_count=CASE
                WHEN auth_backoff.updated_at <= %s THEN 1
                ELSE auth_backoff.failure_count + 1
            END,
            next_allowed_at=CASE
                WHEN auth_backoff.updated_at <= %s
                     OR auth_backoff.failure_count < %s THEN NULL
                ELSE CASE auth_backoff.failure_count
                    WHEN %s THEN %s
                    WHEN %s THEN %s
                    WHEN %s THEN %s
                    WHEN %s THEN %s
                    WHEN %s THEN %s
                    WHEN %s THEN %s
                    ELSE %s
                END
            END,
            updated_at=excluded.updated_at
        RETURNING failure_count, next_allowed_at
        """,
        (
            scope,
            key,
            now,
            decay_cutoff,
            decay_cutoff,
            THRESHOLD - 1,
            THRESHOLD - 1,
            deadline_values[0],
            THRESHOLD,
            deadline_values[1],
            THRESHOLD + 1,
            deadline_values[2],
            THRESHOLD + 2,
            deadline_values[3],
            THRESHOLD + 3,
            deadline_values[4],
            THRESHOLD + 4,
            deadline_values[5],
            deadline_values[6],
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("auth_backoff failure UPSERT returned no row")
    return int(row["failure_count"]), row["next_allowed_at"]


def record_success(connection: Any, *, scope: str, key: str) -> None:
    """Clear any accumulated backoff for ``(scope, key)`` after a success."""
    connection.execute(
        "DELETE FROM auth_backoff WHERE scope=%s AND key=%s",
        (scope, key),
    )
