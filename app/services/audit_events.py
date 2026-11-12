"""Append-only log for sensitive or destructive actions (no browsing API).

Framework-agnostic persistence for security and destructive action records.
Every record is immutable: there is no update API and the database refuses
updates; only retention deletes old rows. Sensitive payload keys are redacted
before the detail is stored and before the structured log line is emitted.

The actor is never a user id: ``owner`` (an OIDC Session), ``emergency``
(the Emergency Password) or ``system`` (FrostVault itself).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from psycopg.types.json import Jsonb

REDACTED = "[REDACTED]"

ACTORS = frozenset({"owner", "emergency", "system"})

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
        "archive_master_key",
        "master_key",
        "crypt_password",
        "crypt_password2",
        "client_secret",
        "api_key",
        "x-api-key",
    }
)

_logger = logging.getLogger("app.audit")


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
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "event": row["event"],
        "actor": row["actor"],
        "outcome": row.get("outcome"),
        "vault_id": row.get("vault_id"),
        "job_id": row.get("job_id"),
        "correlation_id": row.get("correlation_id"),
        "detail": dict(row.get("detail") or {}),
    }


def record_audit_event(
    connection: Any,
    *,
    event: str,
    actor: str,
    vault_id: int | None = None,
    job_id: int | None = None,
    outcome: str | None = None,
    correlation_id: str | None = None,
    emit_log: bool = True,
    **fields: Any,
) -> dict[str, Any]:
    """Persist one append-only audit event and optionally emit a log line.

    ``actor`` is ``owner``, ``emergency`` or ``system``. Extra ``fields``
    become the redacted detail payload.
    """
    if actor not in ACTORS:
        raise ValueError(f"invalid audit actor: {actor}")
    detail = json.loads(json.dumps(redact_sensitive(fields), sort_keys=True, default=str))
    row = connection.execute(
        """
        INSERT INTO audit_events(
            event, actor, outcome, vault_id, job_id, correlation_id, detail
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (event, actor, outcome, vault_id, job_id, correlation_id, Jsonb(detail)),
    ).fetchone()
    recorded = _row_to_event(row)

    if emit_log:
        emit_audit_log_line(
            event,
            actor=actor,
            outcome=outcome,
            vault_id=vault_id,
            job_id=job_id,
            correlation_id=correlation_id,
            audit_event_id=recorded["id"],
            **detail,
        )
    return recorded
