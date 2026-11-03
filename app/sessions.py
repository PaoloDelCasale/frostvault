from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import settings


TOKEN_BYTES = 32


class SessionTransitionError(RuntimeError):
    """A concurrent or expired Session prevented an atomic transition."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _session_is_expired(row: dict[str, Any], now: datetime) -> bool:
    return now >= _parse(row["absolute_expires_at"]) or now >= _parse(
        row["idle_expires_at"]
    )


def _expire_session_if_needed(
    connection: Any,
    *,
    session_id: str,
    now: datetime,
) -> bool:
    """Atomically revoke an observed-expired Session once.

    The expiry predicate is repeated in SQL so a concurrent request that slid
    the idle deadline cannot be invalidated from an older process snapshot.
    """
    changed = connection.execute(
        """
        UPDATE sessions
        SET revoked_at=%s, transition_version=transition_version+1
        WHERE id=%s AND revoked_at IS NULL
          AND (absolute_expires_at <= %s OR idle_expires_at <= %s)
        RETURNING id
        """,
        (
            now.isoformat(),
            session_id,
            now.isoformat(),
            now.isoformat(),
        ),
    ).fetchone()
    return changed is not None


def create_session(
    connection: Any,
    *,
    user_id: int,
    auth_method: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> str:
    raw_token = secrets.token_urlsafe(TOKEN_BYTES)
    now = _now()
    user = connection.execute(
        "SELECT session_version FROM users WHERE id=%s",
        (user_id,),
    ).fetchone()
    if not user:
        raise ValueError("Cannot create a session for an unknown user")
    connection.execute(
        """
        INSERT INTO sessions(
            id, user_id, token_hash, auth_method, csrf_token, session_version,
            created_at, last_seen_at, idle_expires_at, absolute_expires_at,
            reauth_at, ip, user_agent
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(uuid.uuid4()),
            user_id,
            _hash_token(raw_token),
            auth_method,
            secrets.token_urlsafe(TOKEN_BYTES),
            user["session_version"],
            now.isoformat(),
            now.isoformat(),
            (now + timedelta(seconds=settings.session_idle_seconds)).isoformat(),
            (now + timedelta(seconds=settings.session_absolute_seconds)).isoformat(),
            now.isoformat(),
            ip,
            user_agent,
        ),
    )
    return raw_token


def resolve_session(connection: Any, raw_token: str | None) -> dict[str, Any] | None:
    if not raw_token:
        return None
    row = connection.execute(
        "SELECT * FROM sessions WHERE token_hash=%s",
        (_hash_token(raw_token),),
    ).fetchone()
    if not row:
        return None
    if row["revoked_at"]:
        return None
    now = _now()
    if _session_is_expired(row, now):
        _expire_session_if_needed(connection, session_id=row["id"], now=now)
        return None
    user = connection.execute(
        """
        SELECT id, username, display_name, session_version
        FROM users WHERE id=%s
        """,
        (row["user_id"],),
    ).fetchone()
    if not user:
        return None
    if row["session_version"] != user["session_version"]:
        return None
    # Repeat liveness predicates when sliding the deadline.  A logout or expiry
    # committed by another process between the SELECT and this UPDATE must win.
    touched = connection.execute(
        """
        UPDATE sessions
        SET last_seen_at=%s, idle_expires_at=%s
        WHERE id=%s AND revoked_at IS NULL
          AND absolute_expires_at > %s AND idle_expires_at > %s
        RETURNING id
        """,
        (
            now.isoformat(),
            (now + timedelta(seconds=settings.session_idle_seconds)).isoformat(),
            row["id"],
            now.isoformat(),
            now.isoformat(),
        ),
    ).fetchone()
    if not touched:
        return None
    return {
        "id": row["id"],
        "user": user,
        "vault_id": row["vault_id"],
        "csrf_token": row["csrf_token"],
        "auth_method": row["auth_method"],
        "reauth_at": row["reauth_at"],
        "transition_version": row["transition_version"],
    }


def csrf_token_for(connection: Any, raw_token: str | None) -> str | None:
    """Return the per-session CSRF token for a live session, else ``None``.

    This is a read-only lookup used by the CSRF middleware; unlike
    :func:`resolve_session` it never touches ``last_seen_at``.
    """
    if not raw_token:
        return None
    row = connection.execute(
        """
        SELECT s.csrf_token AS csrf_token,
               s.revoked_at AS revoked_at,
               s.idle_expires_at AS idle_expires_at,
               s.absolute_expires_at AS absolute_expires_at,
               s.session_version AS session_session_version,
               u.session_version AS user_session_version
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token_hash=%s
        """,
        (_hash_token(raw_token),),
    ).fetchone()
    if not row or row["revoked_at"]:
        return None
    if row["session_session_version"] != row["user_session_version"]:
        return None
    now = _now()
    if now >= _parse(row["absolute_expires_at"]) or now >= _parse(
        row["idle_expires_at"]
    ):
        return None
    return row["csrf_token"]


def mark_reauthenticated(connection: Any, session_id: str) -> None:
    connection.execute(
        "UPDATE sessions SET reauth_at=%s WHERE id=%s",
        (_now().isoformat(), session_id),
    )


def is_reauth_recent(
    reauth_at: str | None, *, now: datetime, window_seconds: int
) -> bool:
    """Whether the last Reauthentication is within the sensitive-action window."""
    if not reauth_at:
        return False
    return now - _parse(reauth_at) <= timedelta(seconds=window_seconds)


def revoke_session(connection: Any, session_id: str) -> bool:
    """Revoke a Session atomically."""
    changed = connection.execute(
        """
        UPDATE sessions
        SET revoked_at=%s, transition_version=transition_version+1
        WHERE id=%s AND revoked_at IS NULL
        RETURNING id
        """,
        (_now().isoformat(), session_id),
    ).fetchone()
    if not changed:
        return False
    return True


def rotate_session(
    connection: Any,
    session_id: str,
    *,
    reauthenticated: bool = False,
) -> str:
    """Rotate a live Session token and optional Reauthentication atomically."""
    raw_token = secrets.token_urlsafe(TOKEN_BYTES)
    now = _now()
    reauth_assignment = ", reauth_at=%s" if reauthenticated else ""
    params: list[Any] = [
        _hash_token(raw_token),
        now.isoformat(),
        (now + timedelta(seconds=settings.session_idle_seconds)).isoformat(),
    ]
    if reauthenticated:
        params.append(now.isoformat())
    params.extend((session_id, now.isoformat(), now.isoformat()))
    changed = connection.execute(
        f"""
        UPDATE sessions
        SET token_hash=%s,
            last_seen_at=%s,
            idle_expires_at=%s,
            transition_version=transition_version+1{reauth_assignment}
        WHERE id=%s AND revoked_at IS NULL
          AND absolute_expires_at > %s AND idle_expires_at > %s
        RETURNING id
        """,
        tuple(params),
    ).fetchone()
    if not changed:
        raise SessionTransitionError("Cannot rotate an inactive Session")
    return raw_token


def set_session_vault(
    connection: Any,
    session_id: str,
    vault_id: int | None,
    *,
    expected_version: int | None = None,
) -> dict[str, Any] | None:
    """Select a Vault without overwriting a newer Session transition.

    This internal optimistic lock protects concurrent selections and token
    rotations. It is not a browser cache capability and is never exposed.
    """
    now = _now()
    query = """
        UPDATE sessions
        SET vault_id=%s, transition_version=transition_version+1
        WHERE id=%s AND revoked_at IS NULL
          AND absolute_expires_at > %s AND idle_expires_at > %s
    """
    params: list[Any] = [
        vault_id,
        session_id,
        now.isoformat(),
        now.isoformat(),
    ]
    if expected_version is not None:
        query += " AND transition_version=%s"
        params.append(expected_version)
    query += " RETURNING id, vault_id, transition_version"
    return connection.execute(query, tuple(params)).fetchone()

