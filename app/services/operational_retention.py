"""Bounded DB-only operational retention (ADR-0035).

This is deliberately NOT an evidence garbage collector. Settled upload intents,
receipts and completed inventories still participate in snapshot_cleanup's
_has_lineage/_keeper checks. No closed-at timestamp or independent replacement
for that authority exists yet. Keep them, proofs, multiparts, Archive Versions,
remediation history and all potentially evidentiary Jobs, even when old.

Each fixed plan visits at most one keyset page per tick. Persistent high-water
marks bound a sweep despite concurrent inserts; wraparound revisits locked,
newly eligible and late-committing rows. Guards run AFTER row locks in a fresh
READ COMMITTED statement. FK protection remains enabled. No cascading evidence
deletes, provider calls, filesystem access or schema introspection.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .database_retry import owned_connection, retry_database_unit

MAX_BATCH_SIZE = 1000
# Exact exclusions, not a claim that full operational-history pruning is done.
CONSERVATIVE_EXCLUSIONS = (
    "all upload intents, receipts and multipart journals (including settled intents)",
    "all cleanup inventories and proofs (including completed cleanup)",
    "all Archive Versions (including purged), Local Copies and path history",
    "all remediation episodes, links and discovery records",
    "all Jobs except cancelled uploads without any preparation/evidence/reference",
    "unread notifications, open problems and pending notification deliveries",
)

# Reading is not resolution. A problem must be resolved AND read; its age starts
# at the latest final-state timestamp, never merely at original creation.
_NOTIFICATION_CLOSED = """
    n.read_at IS NOT NULL
    AND (n.problem_status IS NULL OR
         (n.problem_status='resolved' AND n.resolved_at IS NOT NULL))
    AND GREATEST(n.created_at::timestamptz, n.read_at::timestamptz,
                 n.resolved_at::timestamptz) < %(operational_cutoff)s
"""


@dataclass(frozen=True)
class _Plan:
    name: str
    maximum: str
    page: str
    delete: str


# Only fixed, reviewed SQL. Candidate filters match partial indexes; safety
# exclusions belong to DELETE so a page of referenced rows cannot starve the
# next page. Primary-key paging also works while timestamps migrate to native
# types, and avoids textual timestamp order/offset assumptions.
_PLANS = (
    _Plan("deliveries",
        "SELECT COALESCE(max(id),0) AS id FROM notification_deliveries WHERE status IN ('delivered','failed')",
        """SELECT id FROM notification_deliveries WHERE id > %(after)s AND id <= %(through)s
             AND status IN ('delivered','failed') ORDER BY id LIMIT %(limit)s FOR UPDATE SKIP LOCKED""",
        f"""DELETE FROM notification_deliveries d WHERE d.id=ANY(%(ids)s)
             AND d.status IN ('delivered','failed')
             AND d.notification_id=ANY(%(notification_ids)s)
             AND d.updated_at::timestamptz < %(operational_cutoff)s
             AND EXISTS (SELECT 1 FROM notifications n WHERE n.id=d.notification_id
                         AND {_NOTIFICATION_CLOSED})"""),
    _Plan("notifications",
        "SELECT COALESCE(max(id),0) AS id FROM notifications WHERE read_at IS NOT NULL AND (problem_status IS NULL OR problem_status='resolved')",
        """SELECT id FROM notifications WHERE id > %(after)s AND id <= %(through)s
             AND read_at IS NOT NULL AND (problem_status IS NULL OR problem_status='resolved')
             ORDER BY id LIMIT %(limit)s FOR UPDATE SKIP LOCKED""",
        f"""DELETE FROM notifications n WHERE n.id=ANY(%(ids)s) AND {_NOTIFICATION_CLOSED}
             AND NOT EXISTS (SELECT 1 FROM notification_deliveries d WHERE d.notification_id=n.id)"""),
    _Plan("audit",
        "SELECT COALESCE(max(id),0) AS id FROM audit_events",
        """SELECT id FROM audit_events WHERE id > %(after)s AND id <= %(through)s
             ORDER BY id LIMIT %(limit)s FOR UPDATE SKIP LOCKED""",
        """DELETE FROM audit_events WHERE id=ANY(%(ids)s) AND created_at::timestamptz < %(audit_cutoff)s"""),
    _Plan("jobs",
        "SELECT COALESCE(max(id),0) AS id FROM jobs WHERE status='cancelled' AND action='upload'",
        """SELECT id FROM jobs WHERE id > %(after)s AND id <= %(through)s
             AND status='cancelled' AND action='upload'
             ORDER BY id LIMIT %(limit)s FOR UPDATE SKIP LOCKED""",
        """DELETE FROM jobs j WHERE j.id=ANY(%(ids)s)
             AND j.status='cancelled' AND j.action='upload'
             AND j.updated_at::timestamptz < %(operational_cutoff)s
             AND j.requested_at::timestamptz < %(operational_cutoff)s
             AND j.archive_version_id IS NULL AND NOT j.upload_reconcile_only
             AND j.upload_snapshot_json IS NULL AND j.upload_plaintext_sha256 IS NULL
             AND j.claim_token IS NULL AND j.claim_expires_at IS NULL
             AND j.terminal_notification_status IS DISTINCT FROM 'pending'
             AND NOT EXISTS (SELECT 1 FROM upload_intents i WHERE i.original_job_id=j.id)
             AND NOT EXISTS (SELECT 1 FROM upload_intent_jobs b WHERE b.job_id=j.id)
             AND NOT EXISTS (SELECT 1 FROM snapshot_cleanup_inventories c WHERE c.job_id=j.id)
             AND NOT EXISTS (SELECT 1 FROM upload_remediations r WHERE r.current_job_id=j.id OR r.resolved_by_job_id=j.id)
             AND NOT EXISTS (SELECT 1 FROM upload_remediations r WHERE r.vault_file_id=j.vault_file_id AND r.state <> 'resolved')
             AND NOT EXISTS (SELECT 1 FROM upload_remediation_originals r WHERE r.original_job_id=j.id)
             AND NOT EXISTS (SELECT 1 FROM upload_remediation_jobs r WHERE r.job_id=j.id)
             AND NOT EXISTS (SELECT 1 FROM upload_remediation_discovery r WHERE r.job_id=j.id)
             AND NOT EXISTS (SELECT 1 FROM snapshot_reconciliation_blocks b WHERE b.vault_file_id=j.vault_file_id)
             AND NOT EXISTS (SELECT 1 FROM catalog_incidents c WHERE c.vault_file_id=j.vault_file_id AND c.status='open')
             AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.job_id=j.id)
             AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.vault_id=j.vault_id AND n.problem_status='open')
             AND NOT EXISTS (SELECT 1 FROM audit_events a WHERE a.job_id=j.id)
             AND NOT EXISTS (SELECT 1 FROM cloud_deletion_items c WHERE c.job_id=j.id)
             AND NOT EXISTS (SELECT 1 FROM vault_class_moves m WHERE m.job_id=j.id)"""),
)


def cutoffs(now: datetime, operational_days: int, audit_days: int) -> dict[str, datetime]:
    """Pure fake-clock seam. Zero/negative retention never means 'delete all'."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("retention requires a timezone-aware clock")
    if operational_days < 1 or audit_days < 1:
        raise ValueError("retention days must be positive")
    current = now.astimezone(timezone.utc)
    return {"operational_cutoff": current - timedelta(days=operational_days),
            "audit_cutoff": current - timedelta(days=audit_days)}


@retry_database_unit
def _prune_page(db: Callable[..., Any], plan: _Plan, bounds: dict[str, datetime], limit: int) -> dict[str, int]:
    # Retry owns the WHOLE transaction, including cursor and commit. Results
    # escape only after commit; a retry cannot advance a cursor past lost work.
    with owned_connection(db()) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        connection.execute("SET LOCAL statement_timeout='2s'")
        connection.execute("SET LOCAL lock_timeout='100ms'")
        cursor = connection.execute("""SELECT last_id,through_id FROM operational_retention_cursors
            WHERE name=%s FOR UPDATE SKIP LOCKED""", (plan.name,)).fetchone()
        if cursor is None:
            return {"examined": 0, "deleted": 0}
        through = cursor["through_id"] or connection.execute(plan.maximum).fetchone()["id"]
        params = {**bounds, "after": cursor["last_id"], "through": through, "limit": limit}
        rows = connection.execute(plan.page, params).fetchall()
        ids = [row["id"] for row in rows]
        if plan.name == "deliveries" and ids:
            # Reopening an episode resets deliveries. Serialize against that
            # transition before deleting its old terminal delivery; otherwise a
            # concurrent reopen could lose its newly pending email. Skip locked
            # parents to avoid waiting in inverse notification/delivery order.
            # Unlocked parents MUST NOT authorize deletion this tick.
            parents = connection.execute("""SELECT n.id FROM notifications n WHERE n.id IN (
                SELECT notification_id FROM notification_deliveries WHERE id=ANY(%s))
                ORDER BY n.id FOR UPDATE SKIP LOCKED""", (ids,)).fetchall()
            params["notification_ids"] = [row["id"] for row in parents]
        # Second statement sees references committed while the page was locking.
        deleted = connection.execute(plan.delete, {**params, "ids": ids}).rowcount if ids else 0
        last = ids[-1] if ids else 0
        if not ids or last >= through:
            last, through = 0, 0
        connection.execute("""UPDATE operational_retention_cursors SET last_id=%s,through_id=%s
            WHERE name=%s""", (last, through, plan.name))
    return {"examined": len(ids), "deleted": deleted}


def maintain_operational_history(db: Callable[..., Any], *, operational_days: int = 30,
        audit_days: int = 90, batch_size: int = 100,
        now: datetime | None = None) -> dict[str, dict[str, int]]:
    """At most four short transactions, each locking/deleting <= batch_size rows.

    Caller schedules periodically off the worker's claim path. SQL/lock timeout
    leaves that page intact for the next tick; deadlock/serialization retries
    replay only this DB work. No vacuum, files or backup-artifact pruning.
    """
    bounds = cutoffs(now if now is not None else datetime.now(timezone.utc), operational_days, audit_days)
    limit = max(1, min(MAX_BATCH_SIZE, int(batch_size)))
    return {plan.name: _prune_page(db, plan, bounds, limit) for plan in _PLANS}
