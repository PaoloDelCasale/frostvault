"""Global User, Identity and Invite administration (issue #135).

Framework-agnostic rules behind the ``/api/admin`` User endpoints, so the
invariants stay directly testable and are enforced in exactly one place:

* the last-active-administrator invariant, applied to demotion *and*
  deactivation, checked and written inside one locked transaction;
* safe Identity unlinking that never leaves a User without any way to sign in;
* Invite listing and revocation that never expose token material and race
  safely with redemption.

No FastAPI imports on purpose: :mod:`app.main` stays a thin HTTP boundary.
"""
from __future__ import annotations

from typing import Any

from ..config import settings
from .audit_events import record_audit_event


class AdministrationError(Exception):
    """A recoverable failure while administering Users.

    ``reason`` is a short machine-readable code callers may surface safely
    (``last_admin``, ``self_demotion``, ``self_deactivation``, ``no_changes``,
    ``not_found``).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def list_users(connection: Any) -> list[dict[str, Any]]:
    """Return every User with its non-sensitive authentication capabilities.

    Reports *whether* a local password is configured and how many external
    Identities are linked, never the password hash, tokens or claims.
    """
    rows = connection.execute(
        """
        SELECT u.id, u.username, u.display_name, u.is_admin, u.active,
               u.created_at,
               (u.password_hash IS NOT NULL) AS has_password,
               (
                   SELECT COUNT(*) FROM vault_members vm
                   WHERE vm.user_id=u.id
               ) AS vault_count,
               (
                   SELECT COUNT(*) FROM user_identities ui
                   WHERE ui.user_id=u.id
               ) AS identity_count
        FROM users u
        ORDER BY lower(u.username)
        """
    ).fetchall()
    return [
        {
            **_public_user(row),
            "created_at": row["created_at"],
            "has_password": bool(row["has_password"]),
            "vault_count": int(row["vault_count"]),
            "identity_count": int(row["identity_count"]),
        }
        for row in rows
    ]


def create_user(
    connection: Any,
    *,
    username: str,
    display_name: str,
    actor_user_id: int,
    password_hash: str | None = None,
    is_admin: bool = False,
) -> dict[str, Any]:
    """Create a local or passwordless User.

    ``password_hash=None`` creates a passwordless User on purpose: Break-glass
    Login refuses a null hash, so the account can only be reached once an
    external Identity is bound through an Invite (ADR-0003).
    """
    row = connection.execute(
        """
        INSERT INTO users(username, display_name, password_hash, is_admin)
        VALUES (%s, %s, %s, %s)
        RETURNING id, username, display_name, is_admin, active, created_at
        """,
        (username, display_name, password_hash, is_admin),
    ).fetchone()
    record_audit_event(
        connection,
        event="admin_user_created",
        actor_user_id=actor_user_id,
        outcome="success",
        visibility="admin",
        target_user_id=row["id"],
        username=username,
        is_admin=bool(is_admin),
        has_password=password_hash is not None,
    )
    return {
        **_public_user(row),
        "created_at": row["created_at"],
        "has_password": password_hash is not None,
        "vault_count": 0,
        "identity_count": 0,
    }


def _lock_active_administrators(connection: Any) -> None:
    """Take the write lock that serializes last-administrator decisions.

    SQLite has no row locks, so it takes the database-wide write lock;
    PostgreSQL locks every active-administrator row (``FOR UPDATE`` cannot
    wrap a ``COUNT(*)`` aggregate). Either way the predicate on the UPDATE
    below observes serialized state.
    """
    backend = getattr(connection, "backend", None) or settings.db_backend
    if backend == "sqlite":
        connection.begin_immediate()
    else:
        connection.execute(
            "SELECT id FROM users WHERE is_admin=TRUE AND active=TRUE FOR UPDATE"
        ).fetchall()


def update_user(
    connection: Any,
    *,
    user_id: int,
    actor_user_id: int,
    active: bool | None = None,
    is_admin: bool | None = None,
    display_name: str | None = None,
    password_hash: str | None = None,
) -> dict[str, Any]:
    """Apply an administrative change to one User inside one transaction.

    Demotion and deactivation share the same invariant: the change must not
    remove the last active administrator. The guard is repeated as a
    predicate on the UPDATE itself, so two concurrent requests that each look
    safe in isolation cannot both commit.
    """
    if is_admin is False and user_id == actor_user_id:
        raise AdministrationError("self_demotion")
    if active is False and user_id == actor_user_id:
        raise AdministrationError("self_deactivation")

    updates: list[str] = []
    params: list[Any] = []
    if active is not None:
        updates.append("active=%s")
        params.append(active)
        updates.append("session_version=session_version+1")
    if is_admin is not None:
        updates.append("is_admin=%s")
        params.append(is_admin)
    if display_name is not None:
        updates.append("display_name=%s")
        params.append(display_name)
    if password_hash is not None:
        updates.append("password_hash=%s")
        params.append(password_hash)
        updates.append("session_version=session_version+1")
    if not updates:
        raise AdministrationError("no_changes")

    guards_last_admin = active is False or is_admin is False
    params.append(user_id)
    predicate = ""
    if guards_last_admin:
        _lock_active_administrators(connection)
        predicate = (
            " AND (is_admin=FALSE OR active=FALSE OR EXISTS ("
            "SELECT 1 FROM users AS other "
            "WHERE other.is_admin=TRUE AND other.active=TRUE AND other.id<>%s))"
        )
        params.append(user_id)

    row = connection.execute(
        f"UPDATE users SET {', '.join(updates)} WHERE id=%s{predicate} "
        "RETURNING id, username, display_name, is_admin, active",
        params,
    ).fetchone()
    if row:
        if is_admin is not None:
            record_audit_event(
                connection,
                event="admin_user_role_changed",
                actor_user_id=actor_user_id,
                outcome="success",
                visibility="admin",
                target_user_id=user_id,
                is_admin=bool(is_admin),
            )
        return _public_user(row)

    exists = connection.execute(
        "SELECT id FROM users WHERE id=%s", (user_id,)
    ).fetchone()
    if not exists:
        raise AdministrationError("not_found")
    raise AdministrationError("last_admin")


def _public_user(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a User row for the API: booleans, never the password hash."""
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "is_admin": bool(row["is_admin"]),
        "active": bool(row["active"]),
    }


def _require_user(connection: Any, user_id: int) -> dict[str, Any]:
    row = connection.execute(
        "SELECT id, password_hash FROM users WHERE id=%s", (user_id,)
    ).fetchone()
    if not row:
        raise AdministrationError("not_found")
    return row


def list_identities(connection: Any, *, user_id: int) -> list[dict[str, Any]]:
    """Return the external Identities linked to one User.

    Only the immutable ``(issuer, subject)`` pair and its binding time are
    exposed: no tokens, no claims, nothing an administrator could replay.
    """
    _require_user(connection, user_id)
    rows = connection.execute(
        """
        SELECT id, issuer, subject, created_at FROM user_identities
        WHERE user_id=%s ORDER BY id
        """,
        (user_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "issuer": row["issuer"],
            "subject": row["subject"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def unlink_identity(
    connection: Any,
    *,
    user_id: int,
    identity_id: int,
    actor_user_id: int,
    confirmed: bool,
) -> list[dict[str, Any]]:
    """Remove one external Identity from a User and return what remains.

    Identity records are immutable: the binding is deleted, never re-pointed
    at another issuer or subject (ADR-0003). The caller must confirm
    explicitly, and the removal is refused when it would leave the User with
    neither a password nor any remaining Identity, which would lock them out.
    """
    if not confirmed:
        raise AdministrationError("confirmation_required")
    user = _require_user(connection, user_id)
    identity = connection.execute(
        "SELECT id, issuer, subject FROM user_identities WHERE id=%s AND user_id=%s",
        (identity_id, user_id),
    ).fetchone()
    if not identity:
        raise AdministrationError("identity_not_found")
    if user["password_hash"] is None:
        others = connection.execute(
            "SELECT COUNT(*) AS total FROM user_identities "
            "WHERE user_id=%s AND id<>%s",
            (user_id, identity_id),
        ).fetchone()["total"]
        if not others:
            raise AdministrationError("would_lock_out")

    connection.execute(
        "DELETE FROM user_identities WHERE id=%s AND user_id=%s",
        (identity_id, user_id),
    )
    record_audit_event(
        connection,
        event="admin_identity_unlinked",
        actor_user_id=actor_user_id,
        outcome="success",
        visibility="admin",
        target_user_id=user_id,
        identity_id=identity_id,
        issuer=identity["issuer"],
        subject=identity["subject"],
    )
    return list_identities(connection, user_id=user_id)
