"""Vault membership/ownership governance.

Provides the transactional ownership-transfer primitive that preserves the
one-primary-owner invariant, plus the admin-exception plumbing (reason,
audit, owner-notification seam) for sensitive global-admin overrides of a
vault's sharing, following the reauth-then-audit precedent set by
ADR-0005 (``docs/adr/0005-host-csrf-reauth-hardening.md``).

Framework-agnostic on purpose: no FastAPI imports, so the rules here are
directly unit-testable and reusable from both the owner self-service
routes and the global-admin override routes in :mod:`app.main`.
"""
from __future__ import annotations

from typing import Any

from ..audit import audit_log
from .vault_roles import OPERATOR, OWNER

# Roles handed out through ordinary membership assignment. The primary
# owner role is never assigned this way: it can only move through
# ``transfer_primary_ownership``, which preserves the one-owner invariant
# transactionally instead of racing the database's partial unique index
# (``vault_members_one_owner_uq``).
ASSIGNABLE_ROLES = (OPERATOR, "viewer")


class GovernanceError(Exception):
    """A recoverable failure while governing vault membership/ownership.

    ``reason`` is a short machine-readable code callers may surface safely.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _find_vault(connection: Any, vault_id: int) -> dict[str, Any] | None:
    return connection.execute(
        "SELECT id FROM vaults WHERE id=%s", (vault_id,)
    ).fetchone()


def _find_active_user(connection: Any, user_id: int) -> dict[str, Any] | None:
    return connection.execute(
        "SELECT 1 FROM users WHERE id=%s AND active=TRUE", (user_id,)
    ).fetchone()


def primary_owner(connection: Any, vault_id: int) -> dict[str, Any] | None:
    """Return the ``{"user_id": ...}`` row of the vault's primary owner."""
    return connection.execute(
        "SELECT user_id FROM vault_members WHERE vault_id=%s AND role=%s",
        (vault_id, OWNER),
    ).fetchone()


def assign_member_role(
    connection: Any,
    *,
    vault_id: int,
    user_id: int,
    role: str,
    expected_owner_user_id: int | None = None,
) -> None:
    """Add or change a member's role to ``operator`` or ``viewer``.

    Never assigns ``owner``: promoting a member to primary owner must go
    through :func:`transfer_primary_ownership` so a vault never ends up
    with more than one. Likewise refuses to reassign the *current* primary
    owner to a non-owner role -- that would silently demote them and leave
    the vault with zero owners, since the partial unique index only
    enforces "at most one", not "at least one". The owner must be replaced
    with :func:`transfer_primary_ownership` first, mirroring
    :func:`remove_member`. Owner self-service passes the owner ID used for
    authorization; the assignment statement rejects it if ownership changed.
    Global-admin overrides explicitly pass ``None``.
    """
    if role not in ASSIGNABLE_ROLES:
        raise GovernanceError("invalid_role")
    if not _find_vault(connection, vault_id):
        raise GovernanceError("vault_not_found")
    if not _find_active_user(connection, user_id):
        raise GovernanceError("user_not_found")
    target = connection.execute(
        "SELECT role FROM vault_members WHERE vault_id=%s AND user_id=%s",
        (vault_id, user_id),
    ).fetchone()
    if target and target["role"] == OWNER:
        raise GovernanceError("owner_required")
    assigned = connection.execute(
        """
        INSERT INTO vault_members(vault_id, user_id, role)
        SELECT %s, %s, %s
        WHERE %s IS NULL OR EXISTS (
            SELECT 1 FROM vault_members
            WHERE vault_id=%s AND user_id=%s AND role=%s
        )
        ON CONFLICT(vault_id, user_id) DO UPDATE SET role=excluded.role
        WHERE vault_members.role <> %s
          AND (
            %s IS NULL OR EXISTS (
                SELECT 1 FROM vault_members AS owner_membership
                WHERE owner_membership.vault_id=%s
                  AND owner_membership.user_id=%s
                  AND owner_membership.role=%s
            )
          )
        RETURNING role
        """,
        (
            vault_id,
            user_id,
            role,
            expected_owner_user_id,
            vault_id,
            expected_owner_user_id,
            OWNER,
            OWNER,
            expected_owner_user_id,
            vault_id,
            expected_owner_user_id,
            OWNER,
        ),
    ).fetchone()
    if not assigned:
        owner = primary_owner(connection, vault_id)
        if (
            expected_owner_user_id is not None
            and owner
            and owner["user_id"] != expected_owner_user_id
        ):
            raise GovernanceError("ownership_changed")
        raise GovernanceError("owner_required")


def remove_member(
    connection: Any,
    *,
    vault_id: int,
    user_id: int,
    expected_owner_user_id: int | None = None,
) -> None:
    """Remove a member's access. Refuses to remove the primary owner.

    The owner must be replaced with :func:`transfer_primary_ownership`
    before their own membership can be removed, so a vault is never left
    without a primary owner. Owner self-service passes the owner ID used for
    authorization; the delete statement rejects it if ownership changed.
    Global-admin overrides explicitly pass ``None``.
    """
    target = connection.execute(
        "SELECT role FROM vault_members WHERE vault_id=%s AND user_id=%s",
        (vault_id, user_id),
    ).fetchone()
    if target and target["role"] == OWNER:
        raise GovernanceError("owner_required")
    deleted = connection.execute(
        """
        DELETE FROM vault_members
        WHERE vault_id=%s AND user_id=%s AND role <> %s
          AND (
            %s IS NULL OR EXISTS (
                SELECT 1 FROM vault_members AS owner_membership
                WHERE owner_membership.vault_id=%s
                  AND owner_membership.user_id=%s
                  AND owner_membership.role=%s
            )
          )
        RETURNING role
        """,
        (
            vault_id,
            user_id,
            OWNER,
            expected_owner_user_id,
            vault_id,
            expected_owner_user_id,
            OWNER,
        ),
    ).fetchone()
    if not deleted:
        owner = primary_owner(connection, vault_id)
        if (
            expected_owner_user_id is not None
            and owner
            and owner["user_id"] != expected_owner_user_id
        ):
            raise GovernanceError("ownership_changed")
        target = connection.execute(
            "SELECT role FROM vault_members WHERE vault_id=%s AND user_id=%s",
            (vault_id, user_id),
        ).fetchone()
        raise GovernanceError("owner_required" if target else "member_not_found")


def transfer_primary_ownership(
    connection: Any,
    *,
    vault_id: int,
    new_owner_user_id: int,
    expected_current_owner_user_id: int | None,
) -> dict[str, int]:
    """Atomically hand primary ownership to ``new_owner_user_id``.

    Owner self-service must pass the owner ID used for authorization so the
    mutation fails if ownership changed in the meantime. A global-admin
    override explicitly passes ``None`` because it is authorized independently
    of the current owner.

    Demotes the current owner to ``operator`` (a narrowing: they keep
    upload/recover but lose membership/policy control, never a promotion
    beyond what they already held) and promotes the target in the same
    transaction, so a vault always has exactly one primary owner. This
    only ever updates ``vault_members`` rows; it never moves the vault's
    generated namespace, local directory, or S3 objects.
    """
    if not _find_vault(connection, vault_id):
        raise GovernanceError("vault_not_found")
    if not _find_active_user(connection, new_owner_user_id):
        raise GovernanceError("user_not_found")
    actual = primary_owner(connection, vault_id)
    if not actual:
        raise GovernanceError("no_current_owner")
    if actual["user_id"] == new_owner_user_id:
        raise GovernanceError("already_owner")
    if (
        expected_current_owner_user_id is not None
        and actual["user_id"] != expected_current_owner_user_id
    ):
        raise GovernanceError("ownership_changed")
    target = connection.execute(
        "SELECT 1 FROM vault_members WHERE vault_id=%s AND user_id=%s",
        (vault_id, new_owner_user_id),
    ).fetchone()
    if not target:
        raise GovernanceError("member_not_found")

    # The owner predicate is repeated in the write, so a concurrent transfer
    # cannot demote the wrong owner after the authorization read above.
    current = connection.execute(
        """
        UPDATE vault_members SET role=%s
        WHERE vault_id=%s AND role=%s AND user_id <> %s
          AND (%s IS NULL OR user_id=%s)
        RETURNING user_id
        """,
        (
            OPERATOR,
            vault_id,
            OWNER,
            new_owner_user_id,
            expected_current_owner_user_id,
            expected_current_owner_user_id,
        ),
    ).fetchone()
    if not current:
        raise GovernanceError("ownership_changed")
    connection.execute(
        """
        INSERT INTO vault_members(vault_id, user_id, role) VALUES (%s, %s, %s)
        ON CONFLICT(vault_id, user_id) DO UPDATE SET role=excluded.role
        """,
        (vault_id, new_owner_user_id, OWNER),
    )
    return {
        "previous_owner_id": int(current["user_id"]),
        "new_owner_id": int(new_owner_user_id),
    }


def notify_owner_of_admin_action(
    event: str,
    *,
    vault_id: int,
    owner_user_id: int,
    actor_id: int,
    reason: str,
    **fields: Any,
) -> None:
    """Owner-notification seam for a global-admin override.

    Persists an append-only audit event, enqueues an in-app notification for
    the vault owner, and emits the structured audit log line. Outbound
    webhook/SMTP delivery is handled by the notification worker (issue #16).
    """
    from ..database import db
    from .audit_events import record_audit_event
    from .notifications import enqueue_notification

    try:
        with db() as connection:
            record_audit_event(
                connection,
                event=event,
                actor_user_id=actor_id,
                vault_id=vault_id,
                outcome="success",
                visibility="owner",
                reason=reason,
                admin_override=True,
                notify_user_id=owner_user_id,
                **fields,
            )
            if owner_user_id:
                enqueue_notification(
                    connection,
                    user_id=owner_user_id,
                    vault_id=vault_id,
                    event=event,
                    title=f"Administrator action: {event}",
                    body=reason,
                    channels=("in_app",),
                )
    except Exception:
        # Persistence must never block the admin action itself; fall back to
        # the structured log line so operators still have a trail.
        audit_log(
            event,
            vault_id=vault_id,
            notify_user_id=owner_user_id,
            actor_id=actor_id,
            reason=reason,
            admin_override=True,
            **fields,
        )
