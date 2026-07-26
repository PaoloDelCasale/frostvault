from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import settings


TOKEN_BYTES = 32


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


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
    if now >= _parse(row["absolute_expires_at"]):
        return None
    if now >= _parse(row["idle_expires_at"]):
        return None
    user = connection.execute(
        """
        SELECT id, username, display_name, is_admin, active, session_version
        FROM users WHERE id=%s
        """,
        (row["user_id"],),
    ).fetchone()
    if not user or not user["active"]:
        return None
    if row["session_version"] != user["session_version"]:
        return None
    connection.execute(
        "UPDATE sessions SET last_seen_at=%s, idle_expires_at=%s WHERE id=%s",
        (
            now.isoformat(),
            (now + timedelta(seconds=settings.session_idle_seconds)).isoformat(),
            row["id"],
        ),
    )
    return {
        "id": row["id"],
        "user": user,
        "vault_id": row["vault_id"],
        "csrf_token": row["csrf_token"],
        "auth_method": row["auth_method"],
        "reauth_at": row["reauth_at"],
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
               u.session_version AS user_session_version,
               u.active AS user_active
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token_hash=%s
        """,
        (_hash_token(raw_token),),
    ).fetchone()
    if not row or row["revoked_at"] or not row["user_active"]:
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


def revoke_session(connection: Any, session_id: str) -> None:
    connection.execute(
        "UPDATE sessions SET revoked_at=%s WHERE id=%s AND revoked_at IS NULL",
        (_now().isoformat(), session_id),
    )
    # Device push subscriptions must not outlive the Session (issue #72 seam 6).
    from .services.notifications import delete_push_subscriptions_for_session

    delete_push_subscriptions_for_session(connection, session_id)


def rotate_session(connection: Any, session_id: str) -> str:
    raw_token = secrets.token_urlsafe(TOKEN_BYTES)
    now = _now()
    connection.execute(
        """
        UPDATE sessions
        SET token_hash=%s, last_seen_at=%s, idle_expires_at=%s
        WHERE id=%s
        """,
        (
            _hash_token(raw_token),
            now.isoformat(),
            (now + timedelta(seconds=settings.session_idle_seconds)).isoformat(),
            session_id,
        ),
    )
    return raw_token


def set_session_vault(connection: Any, session_id: str, vault_id: int | None) -> None:
    connection.execute(
        "UPDATE sessions SET vault_id=%s WHERE id=%s",
        (vault_id, session_id),
    )

