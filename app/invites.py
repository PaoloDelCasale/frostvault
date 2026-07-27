from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import settings
from .system_settings import effective_settings
from .database import INTEGRITY_ERRORS


TOKEN_BYTES = 32


class InviteError(Exception):
    """A recoverable failure while binding an external Identity.

    ``reason`` is a short machine-readable code (e.g. ``expired``,
    ``already_redeemed``, ``identity_taken``) callers may surface safely.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_invite(
    connection: Any,
    *,
    target_user_id: int,
    created_by: int,
    ttl_seconds: int | None = None,
) -> str:
    user = connection.execute(
        "SELECT id FROM users WHERE id=%s", (target_user_id,)
    ).fetchone()
    if not user:
        raise ValueError("Cannot invite an unknown user")
    raw_token = secrets.token_urlsafe(TOKEN_BYTES)
    now = _now()
    ttl = (
        effective_settings(connection, settings_obj=settings).invite_ttl_seconds
        if ttl_seconds is None
        else ttl_seconds
    )
    connection.execute(
        """
        INSERT INTO invites(
            token_hash, target_user_id, created_by, created_at, expires_at
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        (
            _hash_token(raw_token),
            target_user_id,
            created_by,
            now.isoformat(),
            (now + timedelta(seconds=ttl)).isoformat(),
        ),
    )
    return raw_token


def list_pending_invites(connection: Any) -> list[dict[str, Any]]:
    """Return Invites that can still be redeemed, newest first.

    Never exposes ``token_hash`` or any raw token: the token is shown once at
    creation and is unrecoverable afterwards (ADR-0003). Administrators only
    see who an Invite targets, who issued it and when it expires.
    """
    rows = connection.execute(
        """
        SELECT i.id, i.target_user_id, u.username AS target_username,
               i.created_by, i.created_at, i.expires_at
        FROM invites i JOIN users u ON u.id=i.target_user_id
        WHERE i.redeemed_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > %s
        ORDER BY i.id DESC
        """,
        (_now().isoformat(),),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "target_user_id": row["target_user_id"],
            "target_username": row["target_username"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }
        for row in rows
    ]


def revoke_invite(
    connection: Any, *, invite_id: int, actor_user_id: int
) -> dict[str, Any]:
    """Withdraw a pending Invite so it can never be redeemed.

    The Invite row is kept and marked instead of deleted, so the trail
    survives. The conditional UPDATE is the whole race strategy: a redemption
    committing first leaves no matching row here, and a revocation committing
    first makes the redeemer's own conditional UPDATE match nothing.
    """
    revoked = connection.execute(
        """
        UPDATE invites SET revoked_at=%s, revoked_by=%s
        WHERE id=%s AND redeemed_at IS NULL AND revoked_at IS NULL
        RETURNING id, target_user_id, created_by, created_at, expires_at,
                  revoked_at
        """,
        (_now().isoformat(), actor_user_id, invite_id),
    ).fetchone()
    if revoked:
        return {
            "id": revoked["id"],
            "target_user_id": revoked["target_user_id"],
            "created_by": revoked["created_by"],
            "created_at": revoked["created_at"],
            "expires_at": revoked["expires_at"],
            "revoked_at": revoked["revoked_at"],
        }
    invite = connection.execute(
        "SELECT redeemed_at, revoked_at FROM invites WHERE id=%s", (invite_id,)
    ).fetchone()
    if not invite:
        raise InviteError("unknown")
    if invite["redeemed_at"]:
        raise InviteError("already_redeemed")
    raise InviteError("already_revoked")


def _bind_identity(connection: Any, *, user_id: int, issuer: str, subject: str) -> None:
    try:
        connection.execute(
            """
            INSERT INTO user_identities(user_id, issuer, subject, created_at)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, issuer, subject, _now().isoformat()),
        )
    except INTEGRITY_ERRORS:
        raise InviteError("identity_taken")


def redeem_invite(
    connection: Any, *, invite_id: int, issuer: str, subject: str
) -> int:
    invite = connection.execute(
        "SELECT * FROM invites WHERE id=%s", (invite_id,)
    ).fetchone()
    if not invite:
        raise InviteError("unknown")
    if invite["redeemed_at"]:
        raise InviteError("already_redeemed")
    if invite["revoked_at"]:
        raise InviteError("revoked")
    if _now() >= _parse(invite["expires_at"]):
        raise InviteError("expired")
    _bind_identity(
        connection, user_id=invite["target_user_id"], issuer=issuer, subject=subject
    )
    updated = connection.execute(
        """
        UPDATE invites
        SET redeemed_at=%s, redeemed_issuer=%s, redeemed_subject=%s
        WHERE id=%s AND redeemed_at IS NULL AND revoked_at IS NULL
        """,
        (_now().isoformat(), issuer, subject, invite_id),
    )
    # Concurrent redeemers: second UPDATE matches 0 rows (ADR-0003 single-use).
    # A concurrent revocation removes the match the same way.
    rowcount = getattr(updated, "rowcount", None)
    if rowcount == 0:
        raise InviteError(_lost_redemption_reason(connection, invite_id))
    if rowcount is None:
        # Drivers that omit rowcount: re-read and confirm our redeem stuck.
        row = connection.execute(
            "SELECT redeemed_issuer, redeemed_subject FROM invites WHERE id=%s",
            (invite_id,),
        ).fetchone()
        if (
            not row
            or row["redeemed_issuer"] != issuer
            or row["redeemed_subject"] != subject
        ):
            raise InviteError(_lost_redemption_reason(connection, invite_id))
    return int(invite["target_user_id"])


def _lost_redemption_reason(connection: Any, invite_id: int) -> str:
    """Explain why a conditional redeem UPDATE matched nothing."""
    row = connection.execute(
        "SELECT revoked_at FROM invites WHERE id=%s", (invite_id,)
    ).fetchone()
    if row and row["revoked_at"]:
        return "revoked"
    return "already_redeemed"


def resolve_invite(connection: Any, raw_token: str) -> dict[str, Any]:
    invite = connection.execute(
        "SELECT * FROM invites WHERE token_hash=%s", (_hash_token(raw_token),)
    ).fetchone()
    if not invite:
        raise InviteError("unknown")
    if invite["redeemed_at"]:
        raise InviteError("already_redeemed")
    if invite["revoked_at"]:
        raise InviteError("revoked")
    if _now() >= _parse(invite["expires_at"]):
        raise InviteError("expired")
    return invite


def link_identity(
    connection: Any, *, user_id: int, issuer: str, subject: str
) -> None:
    _bind_identity(connection, user_id=user_id, issuer=issuer, subject=subject)
