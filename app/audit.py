"""Structured audit logging with optional durable persistence (issue #16)."""
from __future__ import annotations

from typing import Any

from .services.audit_events import emit_audit_log_line, record_audit_event, redact_sensitive


def audit_log(event: str, connection: Any | None = None, **fields: Any) -> None:
    """Emit one structured audit record for ``event`` with arbitrary fields.

    Sensitive keys are redacted. When ``connection`` is provided the event is
    also appended to ``audit_events`` on that same connection so it commits or
    rolls back with the caller's transaction. This function never opens its own
    database connection: persistence belongs to the caller's transaction,
    including auth-backoff counters.
    """
    redacted = redact_sensitive(fields)
    actor_id = redacted.pop("actor_user_id", redacted.pop("actor_id", None))
    actor = redacted.pop("actor", None)
    if actor is None:
        if event.startswith("break_glass_") or event in {"reauth_failed", "reauth_succeeded"} or (
            event == "auth_backoff_blocked" and redacted.get("flow") in {"break_glass", "reauth"}
        ):
            actor = "emergency"
        else:
            actor = "owner" if actor_id is not None else "system"
    vault_id = redacted.pop("vault_id", None)
    job_id = redacted.pop("job_id", None)
    outcome = redacted.pop("outcome", None)
    correlation_id = redacted.pop("correlation_id", None)
    visibility = redacted.pop("visibility", None)
    redacted.pop("audit_event_id", None)

    if connection is not None:
        record_audit_event(
            connection,
            event=event,
            actor=actor,
            vault_id=vault_id,
            job_id=job_id,
            outcome=outcome,
            correlation_id=correlation_id,
            visibility=visibility,
            emit_log=True,
            **redacted,
        )
        return

    emit_audit_log_line(
        event,
        outcome=outcome,
        actor=actor,
        vault_id=vault_id,
        job_id=job_id,
        correlation_id=correlation_id,
        visibility=visibility,
        **redacted,
    )
