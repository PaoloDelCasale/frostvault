"""Structured audit logging with optional durable persistence (issue #16)."""
from __future__ import annotations

from typing import Any

from .services.audit_events import emit_audit_log_line, record_audit_event, redact_sensitive


def audit_log(
    event: str,
    connection: Any | None = None,
    *,
    actor: str,
    **fields: Any,
) -> None:
    """Emit one structured audit record for ``event`` with arbitrary fields.

    ``actor`` is ``owner``, ``emergency`` or ``system`` (ADR-0035). Sensitive
    keys are redacted. When ``connection`` is provided the event is also
    appended to ``audit_events`` on that same connection so it commits or
    rolls back with the caller's transaction. This function never opens its
    own database connection: persistence belongs to the caller's transaction,
    including auth-backoff counters.
    """
    redacted = redact_sensitive(fields)
    vault_id = redacted.pop("vault_id", None)
    job_id = redacted.pop("job_id", None)
    outcome = redacted.pop("outcome", None)
    correlation_id = redacted.pop("correlation_id", None)
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
            emit_log=True,
            **redacted,
        )
        return

    emit_audit_log_line(
        event,
        actor=actor,
        outcome=outcome,
        vault_id=vault_id,
        job_id=job_id,
        correlation_id=correlation_id,
        **redacted,
    )
