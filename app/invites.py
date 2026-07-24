from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import settings
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
    ttl = settings.invite_ttl_seconds if ttl_seconds is None else ttl_seconds
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
    if _now() >= _parse(invite["expires_at"]):
        raise InviteError("expired")
    _bind_identity(
        connection, user_id=invite["target_user_id"], issuer=issuer, subject=subject
    )
    updated = connection.execute(
        """
        UPDATE invites
        SET redeemed_at=%s, redeemed_issuer=%s, redeemed_subject=%s
        WHERE id=%s AND redeemed_at IS NULL
        """,
        (_now().isoformat(), issuer, subject, invite_id),
    )
    # Concurrent redeemers: second UPDATE matches 0 rows (ADR-0003 single-use).
    rowcount = getattr(updated, "rowcount", None)
    if rowcount == 0:
        raise InviteError("already_redeemed")
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
            raise InviteError("already_redeemed")
    return int(invite["target_user_id"])


def resolve_invite(connection: Any, raw_token: str) -> dict[str, Any]:
    invite = connection.execute(
        "SELECT * FROM invites WHERE token_hash=%s", (_hash_token(raw_token),)
    ).fetchone()
    if not invite:
        raise InviteError("unknown")
    if invite["redeemed_at"]:
        raise InviteError("already_redeemed")
    if _now() >= _parse(invite["expires_at"]):
        raise InviteError("expired")
    return invite


def link_identity(
    connection: Any, *, user_id: int, issuer: str, subject: str
) -> None:
    _bind_identity(connection, user_id=user_id, issuer=issuer, subject=subject)
