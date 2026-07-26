"""Quota policy and admission for a Vault.

This module is the quota seam used by both HTTP queue admission and direct
callers.  It owns the accounting rules, stable decision codes, and the
transactional vault lock; callers only need to supply the operation's
projected growth.

A missing ``vault_quotas`` row, or a NULL limit, means unlimited.  Admission
must be called while the caller's transaction is open.  ``lock_vault`` takes a
row lock on PostgreSQL and a database write lock on SQLite before usage is
read, preventing two admissions from making the same decision from one stale
snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Any


LIMIT_COLUMNS = (
    "storage_soft_limit_bytes",
    "storage_hard_limit_bytes",
    "concurrency_soft_limit",
    "concurrency_hard_limit",
    "restore_30d_soft_limit_bytes",
    "restore_30d_hard_limit_bytes",
)
TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled")


@dataclass(frozen=True)
class QuotaLimits:
    storage_soft_limit_bytes: int | None = None
    storage_hard_limit_bytes: int | None = None
    concurrency_soft_limit: int | None = None
    concurrency_hard_limit: int | None = None
    restore_30d_soft_limit_bytes: int | None = None
    restore_30d_hard_limit_bytes: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    def has_storage_limit(self) -> bool:
        return (
            self.storage_soft_limit_bytes is not None
            or self.storage_hard_limit_bytes is not None
        )

    def has_restore_limit(self) -> bool:
        return (
            self.restore_30d_soft_limit_bytes is not None
            or self.restore_30d_hard_limit_bytes is not None
        )


@dataclass(frozen=True)
class QuotaDecision:
    """One stable, machine-readable quota result."""

    code: str
    severity: str
    projected: int | None = None
    limit: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "projected": self.projected,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class QuotaEvaluation:
    allowed: bool
    decisions: tuple[QuotaDecision, ...] = ()

    @property
    def warnings(self) -> tuple[QuotaDecision, ...]:
        return tuple(item for item in self.decisions if item.severity == "warning")

    @property
    def blocks(self) -> tuple[QuotaDecision, ...]:
        return tuple(item for item in self.decisions if item.severity == "block")

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "decisions": [item.as_dict() for item in self.decisions],
        }


class QuotaBlocked(RuntimeError):
    """Admission failed; the caller must insert no jobs."""

    def __init__(self, evaluation: QuotaEvaluation):
        super().__init__("Quota blocked admission")
        self.evaluation = evaluation


def _limit_value(row: dict[str, Any] | None, column: str) -> int | None:
    value = row.get(column) if row else None
    return int(value) if value is not None else None


def get_limits(connection: Any, vault_id: int) -> QuotaLimits:
    row = connection.execute(
        "SELECT * FROM vault_quotas WHERE vault_id=%s", (vault_id,)
    ).fetchone()
    return QuotaLimits(
        **{column: _limit_value(row, column) for column in LIMIT_COLUMNS}
    )


def validate_limits(limits: QuotaLimits) -> None:
    for column in LIMIT_COLUMNS:
        value = getattr(limits, column)
        if value is not None and value < 0:
            raise ValueError(f"{column} must be nonnegative")
    for soft_name, hard_name in (
        ("storage_soft_limit_bytes", "storage_hard_limit_bytes"),
        ("concurrency_soft_limit", "concurrency_hard_limit"),
        ("restore_30d_soft_limit_bytes", "restore_30d_hard_limit_bytes"),
    ):
        soft = getattr(limits, soft_name)
        hard = getattr(limits, hard_name)
        if soft is not None and hard is not None and soft > hard:
            raise ValueError(f"{soft_name} must be less than or equal to {hard_name}")


def set_limits(connection: Any, vault_id: int, limits: QuotaLimits) -> QuotaLimits:
    """Replace one Vault's limits and return the canonical values."""
    validate_limits(limits)
    exists = connection.execute(
        "SELECT id FROM vaults WHERE id=%s", (vault_id,)
    ).fetchone()
    if not exists:
        raise LookupError("vault_not_found")
    # Configuration changes must not race an admission that is using the
    # previous or next policy snapshot.
    lock_vault(connection, vault_id)
    values = [getattr(limits, column) for column in LIMIT_COLUMNS]
    connection.execute(
        """
        INSERT INTO vault_quotas(
            vault_id, storage_soft_limit_bytes, storage_hard_limit_bytes,
            concurrency_soft_limit, concurrency_hard_limit,
            restore_30d_soft_limit_bytes, restore_30d_hard_limit_bytes
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(vault_id) DO UPDATE SET
            storage_soft_limit_bytes=excluded.storage_soft_limit_bytes,
            storage_hard_limit_bytes=excluded.storage_hard_limit_bytes,
            concurrency_soft_limit=excluded.concurrency_soft_limit,
            concurrency_hard_limit=excluded.concurrency_hard_limit,
            restore_30d_soft_limit_bytes=excluded.restore_30d_soft_limit_bytes,
            restore_30d_hard_limit_bytes=excluded.restore_30d_hard_limit_bytes
        """,
        [vault_id, *values],
    )
    return limits


def lock_vault(connection: Any, vault_id: int) -> None:
    """Serialize quota accounting with all other admissions for a Vault."""
    row = connection.execute(
        "UPDATE vaults SET name=name WHERE id=%s RETURNING id", (vault_id,)
    ).fetchone()
    if not row:
        raise LookupError("vault_not_found")


def _as_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)


def usage_snapshot(
    connection: Any,
    vault_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, int | bool]:
    """Read current accounting values without changing them."""
    current = _as_utc(now)
    cutoff = (current - timedelta(days=30)).isoformat()
    storage = connection.execute(
        """
        SELECT COALESCE(SUM(size), 0) AS total,
               SUM(CASE WHEN size IS NULL THEN 1 ELSE 0 END) AS unknown
        FROM archive_versions
        WHERE vault_id=%s AND availability='available'
        """,
        (vault_id,),
    ).fetchone()
    pending_uploads = connection.execute(
        """
        SELECT COALESCE(SUM(j.total_bytes), 0) AS total
        FROM jobs j
        LEFT JOIN archive_versions av ON av.id=j.archive_version_id
        WHERE j.vault_id=%s
          AND j.action='upload'
          AND j.status NOT IN (%s, %s, %s)
          AND (av.id IS NULL OR av.availability <> 'available')
        """,
        (vault_id, *TERMINAL_JOB_STATUSES),
    ).fetchone()
    concurrency = connection.execute(
        """
        SELECT COUNT(*) AS total FROM jobs
        WHERE vault_id=%s AND status NOT IN (%s, %s, %s)
        """,
        (vault_id, *TERMINAL_JOB_STATUSES),
    ).fetchone()
    restore = connection.execute(
        """
        SELECT COALESCE(SUM(av.size), 0) AS total,
               SUM(CASE WHEN av.size IS NULL THEN 1 ELSE 0 END) AS unknown
        FROM jobs j
        JOIN archive_versions av ON av.id=j.archive_version_id
        WHERE j.vault_id=%s AND j.action='recover' AND j.requested_at >= %s
        """,
        (vault_id, cutoff),
    ).fetchone()
    return {
        "storage_bytes": int(storage["total"] or 0) + int(pending_uploads["total"] or 0),
        "storage_unknown": bool(storage["unknown"] or 0),
        "concurrency": int(concurrency["total"] or 0),
        "restore_30d_bytes": int(restore["total"] or 0),
        "restore_request_unknown": bool(restore["unknown"] or 0),
    }


def evaluate_quota(
    connection: Any,
    vault_id: int,
    *,
    action: str,
    candidate_count: int = 1,
    storage_growth_bytes: int = 0,
    storage_size_unknown: bool = False,
    restore_growth_bytes: int = 0,
    restore_request_unknown: bool = False,
    now: datetime | None = None,
    lock: bool = True,
) -> QuotaEvaluation:
    """Evaluate a projected admission using the stable quota contract."""
    if action not in {
        "upload",
        "recover",
        "free-space",
        "rename",
        "storage-class",
    }:
        raise ValueError(f"unsupported quota action: {action}")
    if candidate_count < 0:
        raise ValueError("candidate_count must be nonnegative")
    if storage_growth_bytes < 0 or restore_growth_bytes < 0:
        raise ValueError("quota growth must be nonnegative")
    if lock:
        lock_vault(connection, vault_id)
    limits = get_limits(connection, vault_id)
    usage = usage_snapshot(connection, vault_id, now=now)
    decisions: list[QuotaDecision] = []

    def add_limit_decision(
        prefix: str, projected: int, soft: int | None, hard: int | None
    ) -> None:
        if hard is not None and projected > hard:
            decisions.append(
                QuotaDecision(f"quota.{prefix}.hard_exceeded", "block", projected, hard)
            )
        elif soft is not None and projected > soft:
            decisions.append(
                QuotaDecision(f"quota.{prefix}.soft_exceeded", "warning", projected, soft)
            )

    if action == "upload" and (storage_growth_bytes or storage_size_unknown):
        if usage["storage_unknown"] or storage_size_unknown:
            if limits.has_storage_limit():
                decisions.append(QuotaDecision("storage.usage_unknown", "block"))
        else:
            add_limit_decision(
                "storage",
                int(usage["storage_bytes"]) + storage_growth_bytes,
                limits.storage_soft_limit_bytes,
                limits.storage_hard_limit_bytes,
            )

    projected_concurrency = int(usage["concurrency"]) + candidate_count
    add_limit_decision(
        "concurrency",
        projected_concurrency,
        limits.concurrency_soft_limit,
        limits.concurrency_hard_limit,
    )

    if action == "recover" and (restore_growth_bytes or restore_request_unknown):
        if usage["restore_request_unknown"] or restore_request_unknown:
            if limits.has_restore_limit():
                decisions.append(QuotaDecision("restore.request_unknown", "block"))
        else:
            add_limit_decision(
                "restore_30d",
                int(usage["restore_30d_bytes"]) + restore_growth_bytes,
                limits.restore_30d_soft_limit_bytes,
                limits.restore_30d_hard_limit_bytes,
            )

    return QuotaEvaluation(
        allowed=not any(item.severity == "block" for item in decisions),
        decisions=tuple(decisions),
    )


def evaluate_current_quota(
    connection: Any,
    vault_id: int,
    *,
    now: datetime | None = None,
) -> QuotaEvaluation:
    """Evaluate the current usage snapshot against the configured limits.

    This is intentionally separate from admission evaluation: the UI needs to
    report the state of usage already present, not a projection that includes
    another candidate operation.
    """
    limits = get_limits(connection, vault_id)
    usage = usage_snapshot(connection, vault_id, now=now)
    decisions: list[QuotaDecision] = []

    def add_limit_decision(
        prefix: str, current: int, soft: int | None, hard: int | None
    ) -> None:
        if hard is not None and current > hard:
            decisions.append(
                QuotaDecision(f"quota.{prefix}.hard_exceeded", "block", current, hard)
            )
        elif soft is not None and current > soft:
            decisions.append(
                QuotaDecision(f"quota.{prefix}.soft_exceeded", "warning", current, soft)
            )

    if usage["storage_unknown"]:
        if limits.has_storage_limit():
            decisions.append(QuotaDecision("storage.usage_unknown", "block"))
    else:
        add_limit_decision(
            "storage",
            int(usage["storage_bytes"]),
            limits.storage_soft_limit_bytes,
            limits.storage_hard_limit_bytes,
        )

    add_limit_decision(
        "concurrency",
        int(usage["concurrency"]),
        limits.concurrency_soft_limit,
        limits.concurrency_hard_limit,
    )

    if usage["restore_request_unknown"]:
        if limits.has_restore_limit():
            decisions.append(QuotaDecision("restore.request_unknown", "block"))
    else:
        add_limit_decision(
            "restore_30d",
            int(usage["restore_30d_bytes"]),
            limits.restore_30d_soft_limit_bytes,
            limits.restore_30d_hard_limit_bytes,
        )

    return QuotaEvaluation(
        allowed=not any(item.severity == "block" for item in decisions),
        decisions=tuple(decisions),
    )


def admit_quota(connection: Any, vault_id: int, **kwargs: Any) -> QuotaEvaluation:
    """Evaluate and raise before any job INSERT when a hard limit is hit."""
    evaluation = evaluate_quota(connection, vault_id, **kwargs)
    if not evaluation.allowed:
        raise QuotaBlocked(evaluation)
    return evaluation


# Short aliases keep the direct-import interface convenient without exposing
# SQL or requiring callers to understand the accounting queries.
evaluate = evaluate_quota
admit = admit_quota
