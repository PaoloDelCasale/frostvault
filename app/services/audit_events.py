"""Append-only audit event store (issue #16).

Framework-agnostic persistence for security and operational audit events.
Every record is immutable: there is no update or delete API. Sensitive
payload keys are redacted before the detail JSON is stored and before the
structured log line is emitted.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

REDACTED = "[REDACTED]"

# Field names (case-insensitive) that must never be persisted or logged in
# cleartext. Matches issue #16 acceptance: secrets, passwords, OIDC tokens,
# recovery material, and sensitive headers.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "password2",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "authorization",
        "cookie",
        "set-cookie",
        "set_cookie",
        "recovery_secret",
        "recovery_material",
        "crypt_password",
        "crypt_password2",
        "client_secret",
        "api_key",
        "x-api-key",
    }
)

_logger = logging.getLogger("app.audit")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def redact_sensitive(value: Any) -> Any:
    """Recursively redact sensitive keys and leave other values intact."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                redacted[key] = REDACTED
            else:
                redacted[key] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    return value


def emit_audit_log_line(event: str, **fields: Any) -> None:
    """Emit one structured JSON audit log line (no persistence)."""
    payload = {"event": event, **redact_sensitive(fields)}
    _logger.warning(json.dumps(payload, sort_keys=True, default=str))


def _row_to_event(row: dict[str, Any]) -> dict[str, Any]:
    detail_raw = row.get("detail_json") or "{}"
    try:
        detail = json.loads(detail_raw)
    except json.JSONDecodeError:
        detail = {}
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "event": row["event"],
        "outcome": row.get("outcome"),
        "actor_user_id": row.get("actor_user_id"),
        "vault_id": row.get("vault_id"),
        "job_id": row.get("job_id"),
        "correlation_id": row.get("correlation_id"),
        "visibility": row.get("visibility") or "vault",
        "detail": detail,
    }


def record_audit_event(
    connection: Any,
    *,
    event: str,
    actor_user_id: int | None = None,
    vault_id: int | None = None,
    job_id: int | None = None,
    outcome: str | None = None,
    correlation_id: str | None = None,
    visibility: str | None = None,
    emit_log: bool = True,
    **fields: Any,
) -> dict[str, Any]:
    """Persist one append-only audit event and optionally emit a log line.

    Extra ``fields`` become the redacted detail payload. When ``visibility``
    is omitted, vault-scoped events default to ``vault`` and events without a
    vault default to ``admin``.
    """
    if visibility is None:
        visibility = "vault" if vault_id is not None else "admin"
    if visibility not in {"vault", "admin", "owner"}:
        raise ValueError(f"invalid audit visibility: {visibility}")

    detail = redact_sensitive(fields)
    created_at = now_iso()
    detail_json = json.dumps(detail, sort_keys=True, default=str)

    row = connection.execute(
        """
        INSERT INTO audit_events(
            created_at, event, outcome, actor_user_id, vault_id, job_id,
            correlation_id, visibility, detail_json
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            created_at,
            event,
            outcome,
            actor_user_id,
            vault_id,
            job_id,
            correlation_id,
            visibility,
            detail_json,
        ),
    ).fetchone()
    recorded = _row_to_event(row)

    if emit_log:
        emit_audit_log_line(
            event,
            outcome=outcome,
            actor_user_id=actor_user_id,
            vault_id=vault_id,
            job_id=job_id,
            correlation_id=correlation_id,
            visibility=visibility,
            audit_event_id=recorded["id"],
            **detail,
        )
    return recorded


def list_vault_audit_events(
    connection: Any,
    vault_id: int,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return vault-visible audit events newest first."""
    rows = connection.execute(
        """
        SELECT * FROM audit_events
        WHERE vault_id=%s AND visibility IN ('vault', 'owner')
        ORDER BY id DESC
        LIMIT %s
        """,
        (vault_id, max(1, min(limit, 500))),
    ).fetchall()
    return [_row_to_event(row) for row in rows]


def list_admin_audit_events(
    connection: Any,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return all audit events for global administrators, newest first."""
    rows = connection.execute(
        """
        SELECT * FROM audit_events
        ORDER BY id DESC
        LIMIT %s
        """,
        (max(1, min(limit, 500)),),
    ).fetchall()
    return [_row_to_event(row) for row in rows]
