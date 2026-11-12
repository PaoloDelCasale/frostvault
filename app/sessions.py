"""Owner Sessions: expiring, revocable credentials, one per device.

Every Session represents the single Owner. A Session is valid only while its
``session_version`` equals ``owner.session_version``: bumping the Owner's
version (for example after a changed Emergency Password) revokes them all.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import settings


TOKEN_BYTES = 32

# The authentication channels of a Session and the audit actor each one acts as.
AUTH_METHOD_ACTORS = {"oidc": "owner", "local": "emergency"}


class SessionTransitionError(RuntimeError):
    """A concurrent or expired Session prevented an atomic transition."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(raw_token: str) -> bytes:
    return hashlib.sha256(raw_token.encode("utf-8")).digest()


def session_actor(session: dict[str, Any] | None) -> str:
    """The audit actor of a Session: ``owner`` (OIDC) or ``emergency``.

    Only signed-in requests have one; FrostVault's own work audits as
    ``system`` without a Session.
    """
    method = (session or {}).get("auth_method")
    if method not in AUTH_METHOD_ACTORS:
        raise ValueError("an audit actor needs a signed-in Session")
    return AUTH_METHOD_ACTORS[method]


def _session_is_expired(row: dict[str, Any], now: datetime) -> bool:
    return now >= row["absolute_expires_at"] or now >= row["idle_expires_at"]


def _expire_session_if_needed(
    connection: Any,
    *,
    session_id: Any,
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
        (now, session_id, now, now),
    ).fetchone()
    return changed is not None


def owner_principal(row: dict[str, Any]) -> dict[str, Any]:
    """The Owner as exposed to request handlers and ``/api/me``."""
    return {
        "id": int(row["id"]),
        "display_name": row["display_name"],
        "session_version": int(row["session_version"]),
    }


def create_session(
    connection: Any,
    *,
    auth_method: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> str:
    """Start a Session for the Owner and return its raw token (never stored)."""
    if auth_method not in AUTH_METHOD_ACTORS:
        raise ValueError(f"invalid auth method: {auth_method}")
    raw_token = secrets.token_urlsafe(TOKEN_BYTES)
    now = _now()
    created = connection.execute(
        """
        INSERT INTO sessions(
            token_hash, auth_method, csrf_token, session_version,
            created_at, last_seen_at, idle_expires_at, absolute_expires_at,
            reauth_at, ip, user_agent
        )
        SELECT %s, %s, %s, o.session_version, %s, %s, %s, %s, %s, %s, %s
        FROM owner o WHERE o.id = 1
        RETURNING id
        """,
        (
            _hash_token(raw_token),
            auth_method,
            secrets.token_urlsafe(TOKEN_BYTES),
            now,
            now,
            now + timedelta(seconds=settings.session_idle_seconds),
            now + timedelta(seconds=settings.session_absolute_seconds),
            now,
            ip,
            user_agent,
        ),
    ).fetchone()
    if created is None:
        raise ValueError("Cannot create a session before the Owner exists")
    return raw_token


def resolve_session(connection: Any, raw_token: str | None) -> dict[str, Any] | None:
    if not raw_token:
        return None
    row = connection.execute(
        """
        SELECT s.*, o.id AS owner_id, o.display_name AS owner_display_name,
               o.session_version AS owner_session_version
        FROM sessions s JOIN owner o ON o.id = 1
        WHERE s.token_hash=%s
        """,
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
    if row["session_version"] != row["owner_session_version"]:
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
            now,
            now + timedelta(seconds=settings.session_idle_seconds),
            row["id"],
            now,
            now,
        ),
    ).fetchone()
    if not touched:
        return None
    owner = owner_principal({
        "id": row["owner_id"],
        "display_name": row["owner_display_name"],
        "session_version": row["owner_session_version"],
    })
    return {
        "id": row["id"],
        "owner": owner,
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
        SELECT s.csrf_token AS csrf_token
        FROM sessions s
        JOIN owner o ON o.id = 1
        WHERE s.token_hash=%s AND s.revoked_at IS NULL
          AND s.session_version = o.session_version
          AND s.absolute_expires_at > %s AND s.idle_expires_at > %s
        """,
        (_hash_token(raw_token), _now(), _now()),
    ).fetchone()
    return row["csrf_token"] if row else None


def mark_reauthenticated(connection: Any, session_id: Any) -> None:
    connection.execute(
        "UPDATE sessions SET reauth_at=%s WHERE id=%s",
        (_now(), session_id),
    )


def is_reauth_recent(
    reauth_at: datetime | None, *, now: datetime, window_seconds: int
) -> bool:
    """Whether the last Reauthentication is within the sensitive-action window."""
    if not reauth_at:
        return False
    return now - reauth_at <= timedelta(seconds=window_seconds)


def revoke_session(connection: Any, session_id: Any) -> bool:
    """Revoke a Session atomically."""
    changed = connection.execute(
        """
        UPDATE sessions
        SET revoked_at=%s, transition_version=transition_version+1
        WHERE id=%s AND revoked_at IS NULL
        RETURNING id
        """,
        (_now(), session_id),
    ).fetchone()
    return changed is not None


def rotate_session(
    connection: Any,
    session_id: Any,
    *,
    reauthenticated: bool = False,
) -> str:
    """Rotate a live Session token and optional Reauthentication atomically."""
    raw_token = secrets.token_urlsafe(TOKEN_BYTES)
    now = _now()
    changed = connection.execute(
        """
        UPDATE sessions
        SET token_hash=%s,
            last_seen_at=%s,
            idle_expires_at=%s,
            reauth_at=CASE WHEN %s THEN %s ELSE reauth_at END,
            transition_version=transition_version+1
        WHERE id=%s AND revoked_at IS NULL
          AND absolute_expires_at > %s AND idle_expires_at > %s
        RETURNING id
        """,
        (
            _hash_token(raw_token),
            now,
            now + timedelta(seconds=settings.session_idle_seconds),
            reauthenticated,
            now,
            session_id,
            now,
            now,
        ),
    ).fetchone()
    if not changed:
        raise SessionTransitionError("Cannot rotate an inactive Session")
    return raw_token


def set_session_vault(
    connection: Any,
    session_id: Any,
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
    params: list[Any] = [vault_id, session_id, now, now]
    if expected_version is not None:
        query += " AND transition_version=%s"
        params.append(expected_version)
    query += " RETURNING id, vault_id, transition_version"
    return connection.execute(query, tuple(params)).fetchone()
