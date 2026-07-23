"""Persistent sliding-window limiter for owner username lookups."""
from __future__ import annotations

import math
import time
from typing import Any


WINDOW_SECONDS = 60.0
MAX_ATTEMPTS = 10


def check_lookup_rate_limit(
    connection: Any,
    *,
    user_id: int,
    client_ip: str,
    backend: str | None = None,
    now: float | None = None,
) -> int | None:
    """Atomically record one lookup attempt.

    The database is the shared seam between application processes. A lock row
    serializes requests for one authenticated-user/IP pair; the attempt rows
    retain the timestamps needed for a true sliding window. SQLite takes its
    immediate write lock explicitly, while PostgreSQL locks only that pair's
    row with ``FOR UPDATE``.
    """
    now = time.time() if now is None else now
    backend = backend or getattr(connection, "backend", "postgresql")
    key = (int(user_id), client_ip)

    if backend == "sqlite":
        # SQLite serializes writers at the database level. This must happen
        # before inspecting the window, otherwise two workers could both admit
        # the last available attempt.
        connection.begin_immediate()

    connection.execute(
        """
        INSERT INTO lookup_rate_limit_keys(user_id, client_ip)
        VALUES (%s, %s)
        ON CONFLICT(user_id, client_ip) DO NOTHING
        """,
        key,
    )
    if backend != "sqlite":
        connection.execute(
            """
            SELECT user_id FROM lookup_rate_limit_keys
            WHERE user_id=%s AND client_ip=%s
            FOR UPDATE
            """,
            key,
        ).fetchone()

    cutoff = now - WINDOW_SECONDS
    connection.execute(
        """
        DELETE FROM lookup_rate_limit_attempts
        WHERE user_id=%s AND client_ip=%s AND attempted_at <= %s
        """,
        (*key, cutoff),
    )
    count = connection.execute(
        """
        SELECT COUNT(*) AS total
        FROM lookup_rate_limit_attempts
        WHERE user_id=%s AND client_ip=%s
        """,
        key,
    ).fetchone()["total"]
    if count >= MAX_ATTEMPTS:
        oldest = connection.execute(
            """
            SELECT MIN(attempted_at) AS attempted_at
            FROM lookup_rate_limit_attempts
            WHERE user_id=%s AND client_ip=%s
            """,
            key,
        ).fetchone()["attempted_at"]
        return max(1, math.ceil(WINDOW_SECONDS - (now - oldest)))

    connection.execute(
        """
        INSERT INTO lookup_rate_limit_attempts(user_id, client_ip, attempted_at)
        VALUES (%s, %s, %s)
        """,
        (*key, now),
    )
    return None
