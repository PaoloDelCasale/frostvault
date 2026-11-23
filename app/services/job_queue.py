"""Job queue: who owns a Job, and when it stops owning it (#794).

A Job is *open* until it reaches a terminal status; an open Job that runs holds
a live lease (``job_leases``), every other open Job waits for a claim. This
Module owns that life, and nothing else implements it (ADR-0026, ADR-0031):

- the one vocabulary of Job states and the SQL predicate "open Job";
- the order of turns: tiers inside a Vault and across Vaults, aging, fairness;
- :class:`JobQueue`, the queue on one connection: claimability, claim, lease
  renewal, compare-and-swap transitions, cancellation, the settling of a Job
  whose lease expired;
- the claim a worker thread holds, the in-process cancellation signal, and
  :class:`QueueRunner`, the queue as one process runs it: "take this Job",
  "record its outcome", "reconcile the Jobs a dead worker left behind".

What an operation does with its Job (Upload, Recovery, cleanup…) stays with the
worker; routes read Jobs from ``job_queries``.

The catalog and most services read the vocabulary from here, so this Module
loads only leaves of the application; what it asks of a Module that reads it
(the catalog, directory aggregates, Upload Remediation) is imported where it is
used.
"""
from __future__ import annotations

import contextvars
import json
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from ..i18n import DEFAULT_LOCALE, format_message_params, translate
from . import metrics as metrics_service
from . import notifications as notification_service
from .database_retry import owned_connection, retry_database_unit
from .shutdown import OperationCancelled, shutdown_state
from .upload_retry import upload_retry_delay_seconds

# A terminal Job never moves again and holds no lease (the schema drops it).
TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled")
# A Job in one of these states is running and holds a live lease
# (``job_leases``); every other non-terminal state waits for a claim.
JOB_LEASE_HELD_STATUSES = frozenset(
    {"downloading", "uploading", "verifying", "cleaning"}
)

# The terminal set as a SQL list: ``('completed', 'failed', 'cancelled')``.
TERMINAL_JOB_STATUSES_SQL = (
    "(" + ", ".join(f"'{status}'" for status in TERMINAL_JOB_STATUSES) + ")"
)
# "Open Job" on an unqualified ``jobs`` row.
OPEN_JOB_SQL = f"status NOT IN {TERMINAL_JOB_STATUSES_SQL}"
TERMINAL_JOB_SQL = f"status IN {TERMINAL_JOB_STATUSES_SQL}"


def open_job_sql(alias: str) -> str:
    """"Open Job" on the ``jobs`` row named ``alias`` in the caller's query."""
    return f"{alias}.{OPEN_JOB_SQL}"


def terminal_job_sql(alias: str) -> str:
    """"Terminal Job" on the ``jobs`` row named ``alias`` in the caller's query."""
    return f"{alias}.{TERMINAL_JOB_SQL}"


# -- The order of turns (ADR-0026, ADR-0037) -----------------------------------
#
# Deterministic, fair selection of runnable Vault turns.


USER_OPERATION_PRIORITY = 0
MANUAL_UPLOAD_PRIORITY = 1
AUTOMATIC_PRIORITY = 2
AGING_SECONDS = 900.0


def job_priority(job: Mapping[str, Any]) -> int:
    """Base tier *inside a Vault*: Owner operations, Owner uploads, then automatic work.

    Bulk manual uploads remain manual. Aging never changes this order inside
    a Vault, nor the durable origin of a Job. Across Vaults see
    :func:`cross_vault_tier` (ADR-0037).
    """
    if job.get("origin") == "automatic":
        return AUTOMATIC_PRIORITY
    if job.get("action") != "upload":
        return USER_OPERATION_PRIORITY
    return MANUAL_UPLOAD_PRIORITY


def effective_priority(job: Mapping[str, Any], now: datetime | None = None) -> int:
    """Promote one turn using *accounted runnable/unserved* time, not Job age.

    ``now`` remains a compatible seam; the catalog owns the durable clock.
    Missing accounting means no credit, never a fallback to requested_at.
    """
    waited = max(0.0, float(job.get("scheduler_wait_seconds") or 0))
    return max(USER_OPERATION_PRIORITY, job_priority(job) - int(waited // AGING_SECONDS))


def cross_vault_tier(job: Mapping[str, Any]) -> int:
    """Tier at which a Vault's turn competes with other Vaults (ADR-0037).

    Owner operations still come first; uploads the Owner asked for and work
    admitted automatically share one tier, so a manual backlog in one Vault
    never holds another Vault's automatic protection back.
    """
    return min(job_priority(job), MANUAL_UPLOAD_PRIORITY)


def effective_cross_vault_tier(job: Mapping[str, Any]) -> int:
    """Cross-Vault tier after aging: only the aged-turn exception reaches tier 0.

    A manual turn reaches the Owner-operation tier after one aging interval,
    an automatic one after two, exactly as before ADR-0037.
    """
    return min(effective_priority(job), MANUAL_UPLOAD_PRIORITY)


def turn_is_aged(job: Mapping[str, Any]) -> bool:
    """True when banked credit lets this turn outrank its cross-Vault tier."""
    return effective_cross_vault_tier(job) < cross_vault_tier(job)


def select_fair_jobs(
    jobs: Sequence[Mapping[str, Any]],
    *,
    limit: int,
    active_by_vault: Mapping[Any, int] | None = None,
    now: datetime | None = None,
) -> list[Mapping[str, Any]]:
    """Select turns, preserving base-tier/oldest-first order inside each Vault.

    Across Vaults turns compete at their cross-Vault tier (ADR-0037): Owner
    operations, then every upload and automatic work together. Equal-tier turns prefer fewer live leases, then the longest-unserved turn
    (including concurrency 1, where every active count can be zero). A turn's
    credit is spent by its first nomination in this pass; later backlog entries
    compete at their base tier. Only the catalog's successful conditional claim
    actually consumes durable credit. A generation fences concurrent nominees.
    """
    if limit <= 0 or not jobs:
        return []
    active = dict(active_by_vault or {})
    by_vault: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
    for job in sorted(jobs, key=_job_order):
        by_vault[job.get("vault_id")].append(job)
    position = {vault_id: 0 for vault_id in by_vault}

    def vault_key(vault_id: Any) -> tuple:
        head = by_vault[vault_id][position[vault_id]]
        waited = float(head.get("scheduler_wait_seconds") or 0) if position[vault_id] == 0 else 0.0
        tier = (effective_cross_vault_tier(head) if position[vault_id] == 0
                else cross_vault_tier(head))
        return (
            tier,
            int(active.get(vault_id, 0)),
            -waited,
            str(head.get("scheduler_last_served_at") or ""),
            str(head.get("requested_at") or ""),
            int(head.get("id") or 0),
            str(vault_id),
        )

    selected: list[Mapping[str, Any]] = []
    while len(selected) < limit and position:
        vault_id = min(position, key=vault_key)
        selected.append(by_vault[vault_id][position[vault_id]])
        position[vault_id] += 1
        active[vault_id] = int(active.get(vault_id, 0)) + 1
        if position[vault_id] >= len(by_vault[vault_id]):
            del position[vault_id]
    return selected


def _job_order(job: Mapping[str, Any]) -> tuple[int, str, int]:
    return (job_priority(job), str(job.get("requested_at") or ""), int(job.get("id") or 0))

# -- The queue on one connection -----------------------------------------------

# The action/state matrix of work that can be persisted mid-I/O (alias ``j``):
# with no live lease, such a Job was interrupted and must be reconciled before
# it runs again. Waiting states are absent: they stay with the scheduler.
INTERRUPTED_JOB_SQL = """
(
        (j.action='recover' AND j.status IN ('downloading', 'verifying'))
     OR (j.action='upload' AND j.status IN ('uploading', 'verifying'))
     OR (j.action='rename' AND j.status IN ('uploading', 'verifying', 'cleaning'))
     OR (j.action='free-space' AND j.status='cleaning')
     OR (j.action='storage-class' AND j.status='uploading')
     OR (j.action='cloud-purge' AND j.status='cleaning')
)
"""


class JobQueue:
    """The Job queue on one connection: claims, leases and transitions (#589).

    A claim is one row in ``job_leases`` (token, expiry), never a column of
    the Job: renewal is a HOT update of that row and nothing else. The lease
    token changes only while its Job row is locked (claim, takeover,
    release, end), so a worker that locks the Job row and then reads its
    lease sees a fresh answer; cancellation, takeover and expiry are always
    checked on that live state (ADR-0035), never on a cached generation.

    The caller owns the transaction: nothing here commits.
    """

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        # The row the last :meth:`transition_job` moved (#597).
        self.transitioned_job: Mapping[str, Any] | None = None

    @staticmethod
    def _claimable_job_status_sql(
        *,
        prefix: str,
        now: str,
        restore_due_before: str,
        include_restoring: bool = True,
    ) -> tuple[str, list[Any]]:
        """Return the durable scheduler-state predicate shared by claim/count.

        A claim only starts work from an explicitly runnable state.  Intermediate
        states (``uploading``, ``cleaning``, …) are deliberately absent: a stale
        lease in one of those states must first pass restart reconciliation rather
        than be run as though it were a fresh queued Job.
        """
        status = f"{prefix}status"
        action = f"{prefix}action"
        run_after = f"{prefix}run_after"
        updated_at = f"{prefix}updated_at"
        clauses = [
            f"{status}='queued'",
            f"({status}='retrying' AND ({run_after} IS NULL OR {run_after} <= %s::timestamptz))",
            (
                f"({action}='cloud-purge' AND {status}='pending_delay' "
                f"AND {run_after} <= %s::timestamptz)"
            ),
        ]
        params: list[Any] = [now, now]
        if include_restoring:
            clauses.append(
                f"({action} IN ('recover', 'storage-class') "
                f"AND {status}='restoring' AND {updated_at} <= %s::timestamptz)"
            )
            params.append(restore_due_before)
        return "(" + " OR ".join(clauses) + ")", params

    @staticmethod
    def _stop_protection_purge_sql(prefix: str) -> str:
        return f"""(
            {prefix}action = 'cloud-purge' AND EXISTS (
                SELECT 1 FROM cloud_purge_requests stop_request
                WHERE stop_request.group_id = {prefix}group_id
                  AND stop_request.stop_protection
            )
        )"""

    @classmethod
    def _claimable_job_predicate_sql(
        cls,
        *,
        prefix: str,
        now: str,
        restore_due_before: str,
    ) -> tuple[str, list[Any]]:
        """Build the one durable claimability predicate for scheduler/metrics.

        Keeping candidate selection, conditional acquisition, and queue depth on
        this predicate prevents monitoring from reporting rows a worker cannot
        acquire. An active Vault runs its ordinary work; a stopping Vault runs
        only the purge consented by Stop Protection; a paused Vault holds every
        new cloud Job except recoveries and a storage-class change already
        restoring (ADR-0033). An automatic upload not yet materialized waits
        while the Vault's automatic upload is off (#423). A live lease,
        anywhere in a purge group, keeps the Job unclaimable.
        """
        runnable_sql, params = cls._claimable_job_status_sql(
            prefix=prefix,
            now=now,
            restore_due_before=restore_due_before,
            include_restoring=True,
        )
        stop_purge = cls._stop_protection_purge_sql(prefix)
        predicate = f"""
            {runnable_sql}
            AND EXISTS (
                SELECT 1 FROM vaults scope_vault
                WHERE scope_vault.id = {prefix}vault_id
                  AND (
                        (scope_vault.lifecycle = 'active' AND NOT {stop_purge})
                     OR (scope_vault.lifecycle = 'stopping' AND {stop_purge})
                  )
                  AND (
                        NOT scope_vault.paused
                     OR {prefix}action = 'recover'
                     OR ({prefix}action = 'storage-class' AND {prefix}status = 'restoring')
                  )
            )
            AND (
                    {prefix}action <> 'upload'
                 OR {prefix}origin <> 'automatic'
                 OR {prefix}archive_version_id IS NOT NULL
                 OR COALESCE((
                        SELECT upload_policy.auto_upload FROM vault_operation_policies upload_policy
                        WHERE upload_policy.vault_id = {prefix}vault_id
                    ), TRUE)
            )
            AND NOT EXISTS (
                SELECT 1 FROM job_leases live_lease
                WHERE live_lease.job_id = {prefix}id
                  AND live_lease.expires_at > %s::timestamptz
            )
            AND (
                    {prefix}action <> 'cloud-purge'
                 OR NOT EXISTS (
                        SELECT 1 FROM jobs purge_peer
                        WHERE purge_peer.vault_id = {prefix}vault_id
                          AND purge_peer.group_id = {prefix}group_id
                          AND purge_peer.action = 'cloud-purge'
                          AND {open_job_sql("purge_peer")}
                          AND (
                                (
                                    purge_peer.status <> 'queued'
                                AND NOT (
                                    purge_peer.status = 'pending_delay'
                                    AND purge_peer.run_after <= %s::timestamptz
                                )
                                )
                             OR EXISTS (
                                    SELECT 1 FROM job_leases peer_lease
                                    WHERE peer_lease.job_id = purge_peer.id
                                      AND peer_lease.expires_at > %s::timestamptz
                                )
                          )
                    )
            )
        """
        return predicate, [*params, now, now, now]

    # SQL twin of :func:`job_priority` (alias ``j``), the order inside
    # a Vault: 0 Owner operations, 1 uploads the Owner asked for, 2 anything
    # admitted automatically. Candidate windows are cut per Vault and per tier.
    _SCHEDULING_TIER_SQL = (
        "CASE WHEN j.origin='automatic' THEN 2 WHEN j.action<>'upload' THEN 0 ELSE 1 END"
    )
    # SQL twin of :func:`cross_vault_tier` (ADR-0037): across Vaults
    # Owner uploads and automatic work share tier 1.
    _CROSS_VAULT_TIER_SQL = (
        "CASE WHEN j.origin<>'automatic' AND j.action<>'upload' THEN 0 ELSE 1 END"
    )

    @staticmethod
    def _lease_token(claim_token: Any) -> str:
        """A lease token is a UUID; anything else is a programming error."""
        if not claim_token:
            raise ValueError("A Job claim token is required")
        return str(uuid.UUID(str(claim_token)))

    def list_claimable_jobs(
        self,
        *,
        now: str,
        restore_due_before: str,
        limit: int,
        per_vault_limit: int | None = None,
        exclude_job_ids: Sequence[int] = (),
        unserved_only: bool = False,
        include_file_ids: Sequence[Any] = (),
    ) -> list[dict[str, Any]]:
        """Load scheduler candidates using the shared claimability definition.

        The window is taken **per Vault and per scheduling tier**
        (``per_vault_limit``, default ``limit``), never over the whole
        installation: a Vault holding thousands of older Jobs, or Jobs the worker
        cannot run, must not push every other Vault's work (or its own uploads
        behind a flood of Owner operations) out of the candidate set (issues
        #493, #500). Windows follow :func:`job_priority`; rows are
        ordered by :func:`cross_vault_tier` (ADR-0037).
        ``exclude_job_ids`` removes candidates the worker already found not
        runnable *before* the window is cut. ``limit`` defaults the per-Vault
        window, not a global cut: every eligible Vault must be visible to the
        turn selector, including its automatic work.
        Each row carries the File's current path and its Vault's root and bucket.
        ``include_file_ids`` adds the claimable Jobs of those Files beyond the
        window: Local Copies the scan just read, whose upload goes first (#713).
        """
        if limit <= 0:
            return []
        window = int(per_vault_limit) if per_vault_limit else int(limit)
        predicate, params = self._claimable_job_predicate_sql(
            prefix="j.",
            now=now,
            restore_due_before=restore_due_before,
        )
        excluded = [int(job_id) for job_id in exclude_job_ids]
        tier = self._SCHEDULING_TIER_SQL
        # When no slot is free we only need starvation observations, not the
        # already-served bulk queue's windows or local readiness checks.
        idle_filter = """
            AND NOT EXISTS (
                SELECT 1 FROM jobs serving
                JOIN job_leases serving_lease ON serving_lease.job_id = serving.id
                WHERE serving.vault_id = v.id
                  AND serving_lease.expires_at > %s::timestamptz
            )
        """ if unserved_only else ""
        return self.connection.execute(
            f"""
            SELECT j.*, f.path, v.source_root, v.uuid AS vault_uuid,
                   v.name AS vault_name, v.lifecycle AS vault_lifecycle,
                   t.bucket AS s3_bucket, t.key_layout AS s3_key_layout
            FROM vaults v
            JOIN vault_targets t ON t.id = v.target_id
            CROSS JOIN LATERAL (
                SELECT ranked.* FROM (
                    SELECT j.*,
                           row_number() OVER (
                               PARTITION BY {tier}
                               ORDER BY j.requested_at ASC, j.id ASC
                           ) AS tier_rank
                    FROM jobs j
                    WHERE j.vault_id = v.id
                      AND ({predicate})
                      AND j.id <> ALL(%s::bigint[])
                ) ranked
                WHERE ranked.tier_rank <= {window} OR ranked.file_id = ANY(%s::uuid[])
            ) j
            JOIN files f ON f.id = j.file_id
            WHERE v.lifecycle IN ('active', 'stopping') {idle_filter}
            ORDER BY {self._CROSS_VAULT_TIER_SQL} ASC, {tier} ASC,
                     j.requested_at ASC, j.id ASC
            """,
            [*params, excluded, [str(file_id) for file_id in include_file_ids],
             *([now] if unserved_only else [])],
        ).fetchall()

    def observe_scheduler_turns(
        self, *, now: str, runnable_vault_ids: Sequence[int],
    ) -> dict[int, dict[str, Any]]:
        """Bank only observed runnable/unserved time, durably across restarts.

        The worker validates one runnable head per Vault (including the local
        automatic-upload fence) before observing. A live lease pauses aging.
        SQL state changes invalidate open intervals even between observations;
        missing/non-runnable Vaults retain banked credit but earn nothing new.
        SKIP LOCKED avoids blocking claims; a skipped turn gets no promotion.
        """
        runnable = sorted({int(vault_id) for vault_id in runnable_vault_ids})
        rows = self.connection.execute(
            "SELECT * FROM scheduler_vault_turns ORDER BY vault_id FOR UPDATE SKIP LOCKED"
        ).fetchall()
        active = self.active_job_counts(now=now)
        result = {}
        for row in rows:
            vault_id = int(row["vault_id"])
            eligible = vault_id in runnable and not active.get(vault_id, 0)
            updated = self.connection.execute(
                """UPDATE scheduler_vault_turns
                   SET wait_seconds=wait_seconds + CASE
                         WHEN %s AND eligible_since IS NOT NULL
                         THEN GREATEST(0, EXTRACT(EPOCH FROM (%s::timestamptz-eligible_since)))
                         ELSE 0 END,
                       eligible_since=CASE WHEN %s THEN
                         GREATEST(eligible_since, %s::timestamptz) ELSE NULL END
                   WHERE vault_id=%s
                   RETURNING wait_seconds AS scheduler_wait_seconds,
                             last_served_at::text AS scheduler_last_served_at,
                             generation AS scheduler_generation""",
                (eligible, now, eligible, now, vault_id),
            ).fetchone()
            if vault_id in runnable:
                result[vault_id] = dict(updated)
        return result

    def active_job_counts(self, *, now: str) -> dict[int, int]:
        """Jobs currently holding a live lease, per Vault (scheduler fairness).

        A terminal Job holds no lease (the schema releases it), so a live lease
        is running work by construction.
        """
        rows = self.connection.execute(
            """
            SELECT j.vault_id, count(*) AS total
            FROM job_leases l JOIN jobs j ON j.id = l.job_id
            WHERE l.expires_at > %s::timestamptz
            GROUP BY j.vault_id
            """,
            (now,),
        ).fetchall()
        return {int(row["vault_id"]): int(row["total"]) for row in rows}

    def _lock_claim_vault(self, job_id: int) -> bool:
        """Vault-first shared fence against pause/lifecycle changes.

        Skip a contended Vault instead of blocking the scheduler. The following
        statement sees pause's committed flag, even after stale candidate reads.
        Shared claims and held progress steps remain concurrent (ADR-0031).
        """
        return self.connection.execute(
            """SELECT v.id FROM vaults v
               WHERE v.id=(SELECT vault_id FROM jobs WHERE id=%s)
               FOR KEY SHARE SKIP LOCKED""", (job_id,),
        ).fetchone() is not None

    def _lock_scheduler_turn(self, job_id: int, generation: int | None) -> bool:
        """Lock accounting before acquiring any Job locks.

        Base claims can briefly queue here; aged nominations skip contention.
        Once holding the turn row, callers MUST use SKIP LOCKED on Jobs: a bulk
        status writer can already hold Jobs and need this row to invalidate an
        interval. Never wait for its Jobs, nor hold a Job while waiting here.
        Ordinary progress retains ADR-0031 shared Vault fences.
        """
        job = self.connection.execute(
            "SELECT vault_id FROM jobs WHERE id=%s", (job_id,),
        ).fetchone()
        if job is None:
            return False
        lock = "FOR UPDATE SKIP LOCKED" if generation is not None else "FOR UPDATE"
        turn = self.connection.execute(
            "SELECT generation FROM scheduler_vault_turns WHERE vault_id=%s " + lock,
            (job["vault_id"],),
        ).fetchone()
        return turn is not None and (generation is None or turn["generation"] == generation)

    def _consume_scheduler_turn(self, job_id: int, *, now: str) -> None:
        self.connection.execute(
            """UPDATE scheduler_vault_turns
               SET wait_seconds=0, eligible_since=NULL,
                   last_served_at=GREATEST(last_served_at, %s::timestamptz),
                   generation=generation+1
               WHERE vault_id=(SELECT vault_id FROM jobs WHERE id=%s)""", (now, job_id),
        )

    def claim_job(
        self,
        *,
        job_id: int,
        claim_token: str,
        claimed_at: str,
        claim_expires_at: str,
        now: str,
        restore_due_before: str,
        scheduler_generation: int | None = None,
    ) -> dict[str, Any] | None:
        """Atomically lease one runnable Job.

        Vault (shared, skip if contended), then the scheduler turn, then the
        Job row (skip if locked); the predicate is rechecked in a fresh
        statement under those locks. An expired lease is taken over only if it
        is still expired on its latest row version: a renewal that won keeps it.
        A loser observes no returned row instead of executing a stale
        in-memory selection.
        """
        token = self._lease_token(claim_token)
        if not self._lock_claim_vault(job_id):
            return None
        if not self._lock_scheduler_turn(job_id, scheduler_generation):
            return None
        if self.connection.execute(
            'SELECT id FROM jobs WHERE id=%s FOR UPDATE SKIP LOCKED', (job_id,),
        ).fetchone() is None:
            return None
        claimable_sql, claimable_params = self._claimable_job_predicate_sql(
            prefix="j.",
            now=now,
            restore_due_before=restore_due_before,
        )
        claimed = self.connection.execute(
            f"""
            WITH target AS (
                SELECT j.id, j.status, j.updated_at FROM jobs j
                WHERE j.id=%s AND j.action<>'cloud-purge' AND ({claimable_sql})
            ), lease AS (
                INSERT INTO job_leases(job_id, token, claimed_at, expires_at)
                SELECT id, %s, %s, %s FROM target
                ON CONFLICT (job_id) DO UPDATE
                SET token=EXCLUDED.token, claimed_at=EXCLUDED.claimed_at,
                    expires_at=EXCLUDED.expires_at
                WHERE job_leases.expires_at <= %s::timestamptz
                RETURNING job_id, token, claimed_at, expires_at
            )
            SELECT target.id, target.status, target.updated_at,
                   lease.token::text AS claim_token, lease.claimed_at,
                   lease.expires_at AS claim_expires_at
            FROM target JOIN lease ON lease.job_id = target.id
            """,
            (job_id, *claimable_params, token, claimed_at, claim_expires_at, now),
        ).fetchone()
        if claimed is not None:
            self._consume_scheduler_turn(job_id, now=claimed_at)
        return claimed

    def claim_purge_group(
        self,
        *,
        lead_job_id: int,
        claim_token: str,
        claimed_at: str,
        claim_expires_at: str,
        now: str,
        message: str,
        message_key: str,
        scheduler_generation: int | None = None,
    ) -> list[dict[str, Any]]:
        """Lease every active Job in one cloud-purge group as one operation.

        A permanent purge may span many Vault Files.  Its group is only
        executable when every non-terminal member is due and unclaimed (or its
        prior lease has expired). PostgreSQL locks the group rows in deterministic
        ``requested_at, id`` order.  This prevents two workers from splitting a
        destructive group between themselves: the group claims all its members
        or none, including under partial row contention.
        """
        token = self._lease_token(claim_token)
        if not self._lock_claim_vault(lead_job_id):
            return []
        # Resolve the group before taking Job locks. The advisory fence below
        # serializes different lead nominations for the same destructive group.
        lead = self.connection.execute(
            f"""
            SELECT j.vault_id, j.group_id, v.paused, v.lifecycle,
                   {self._stop_protection_purge_sql('j.')} AS stop_protection
            FROM jobs j JOIN vaults v ON v.id = j.vault_id
            WHERE j.id=%s AND j.action='cloud-purge'
            """,
            (lead_job_id,),
        ).fetchone()
        if lead is None or not lead.get("group_id"):
            return []
        # A contended destructive group must not block the scheduler thread.
        # Row locks below still protect cancellation/non-cooperating writers.
        advisory = self.connection.execute(
            """
            SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS acquired
            """,
            (f"frostvault:cloud-purge:{lead['vault_id']}:{lead['group_id']}",),
        ).fetchone()
        if not advisory or not advisory["acquired"]:
            return []
        lifecycle_admits = (
            (lead["lifecycle"] == "active" and not lead["stop_protection"])
            or (lead["lifecycle"] == "stopping" and lead["stop_protection"])
        )
        if lead["paused"] or not lifecycle_admits:
            return []
        if not self._lock_scheduler_turn(lead_job_id, scheduler_generation):
            return []
        expected = self.connection.execute(
            "SELECT id FROM jobs WHERE vault_id=%s AND group_id=%s AND action='cloud-purge'",
            (lead['vault_id'], lead['group_id']),
        ).fetchall()
        rows = self.connection.execute(
            """
            SELECT j.id, j.status,
                   (j.status = 'queued' OR (
                        j.status = 'pending_delay' AND j.run_after <= %s::timestamptz
                   )) AS runnable,
                   NOT EXISTS (
                       SELECT 1 FROM job_leases live_lease
                       WHERE live_lease.job_id = j.id
                         AND live_lease.expires_at > %s::timestamptz
                   ) AS available
            FROM jobs j
            WHERE j.vault_id=%s AND j.group_id=%s AND j.action='cloud-purge'
            ORDER BY j.requested_at ASC, j.id ASC FOR UPDATE SKIP LOCKED
            """,
            (now, now, lead["vault_id"], lead["group_id"]),
        ).fetchall()
        # Never split a purge group when a status writer holds one member.
        if {row['id'] for row in rows} != {row['id'] for row in expected}:
            return []
        active = [
            row
            for row in rows
            if row["status"] not in TERMINAL_JOB_STATUSES
        ]
        if not active or any(
            not row["available"] or not row["runnable"] for row in active
        ):
            return []

        ids = [int(row["id"]) for row in active]
        self.connection.execute("SAVEPOINT purge_group_claim")
        leased = self.connection.execute(
            """
            INSERT INTO job_leases(job_id, token, claimed_at, expires_at)
            SELECT member, %s, %s, %s FROM unnest(%s::bigint[]) AS member
            ON CONFLICT (job_id) DO UPDATE
            SET token=EXCLUDED.token, claimed_at=EXCLUDED.claimed_at,
                expires_at=EXCLUDED.expires_at
            WHERE job_leases.expires_at <= %s::timestamptz
            RETURNING job_id
            """,
            (token, claimed_at, claim_expires_at, ids, now),
        ).fetchall()
        if len(leased) != len(ids):
            # A previous holder renewed one member meanwhile: all or nothing.
            self.connection.execute("ROLLBACK TO SAVEPOINT purge_group_claim")
            return []
        self.connection.execute("RELEASE SAVEPOINT purge_group_claim")
        self.connection.execute(
            """
            UPDATE jobs
            SET status='cleaning', message=%s, message_key=%s,
                message_params=NULL, updated_at=%s
            WHERE id = ANY(%s::bigint[])
            """,
            (message, message_key, claimed_at, ids),
        )
        self._consume_scheduler_turn(lead_job_id, now=claimed_at)
        # Keep the scheduler's deterministic ordering for the selected lead.
        return self.connection.execute(
            """
            SELECT j.id, j.file_id, j.status, j.requested_at,
                   l.token::text AS claim_token, l.claimed_at,
                   l.expires_at AS claim_expires_at
            FROM jobs j JOIN job_leases l ON l.job_id = j.id
            WHERE j.id = ANY(%s::bigint[])
            ORDER BY j.requested_at ASC, j.id ASC
            """,
            (ids,),
        ).fetchall()

    def renew_job_claim(
        self,
        *,
        job_id: int,
        claim_token: str,
        now: str,
        claim_expires_at: str,
        lock_job: bool = False,
    ) -> dict[str, Any] | None:
        """Extend a live claim, returning no row after cancellation, takeover or expiry.

        Only the lease row is rewritten (a HOT update). With ``lock_job`` the
        Job row is locked first, in the claim's lock order (Job, then lease):
        until the surrounding transaction commits no cancellation or takeover
        can win, and one that already won is seen here.
        """
        token = self._lease_token(claim_token)
        if lock_job:
            return self.connection.execute(
                f"""
                WITH job AS (
                    SELECT id, status, updated_at FROM jobs
                    WHERE id=%s AND {OPEN_JOB_SQL}
                    FOR NO KEY UPDATE
                )
                UPDATE job_leases l SET expires_at=%s
                FROM job
                WHERE l.job_id = job.id AND l.token=%s
                  AND l.expires_at > %s::timestamptz
                RETURNING job.id, job.status, l.token::text AS claim_token,
                          l.claimed_at, l.expires_at AS claim_expires_at,
                          job.updated_at
                """,
                (job_id, claim_expires_at, token, now),
            ).fetchone()
        return self.connection.execute(
            """
            UPDATE job_leases l SET expires_at=%s
            FROM jobs j
            WHERE l.job_id=%s AND j.id = l.job_id AND l.token=%s
              AND l.expires_at > %s::timestamptz
            RETURNING j.id, j.status, l.token::text AS claim_token,
                      l.claimed_at, l.expires_at AS claim_expires_at, j.updated_at
            """,
            (claim_expires_at, job_id, token, now),
        ).fetchone()

    def job_claim_is_live(self, *, job_id: int, claim_token: str, now: str) -> bool:
        """Read the exact predicate of :meth:`renew_job_claim` without writing.

        The answer is the same point-in-time fact a renewal returns. It extends
        nothing, so the caller must have renewed the lease recently.
        """
        return self.connection.execute(
            """
            SELECT 1 FROM job_leases
            WHERE job_id=%s AND token=%s AND expires_at > %s::timestamptz
            """,
            (job_id, self._lease_token(claim_token), now),
        ).fetchone() is not None

    def transition_job(
        self,
        *,
        job_id: int,
        status: str,
        message: str,
        message_key: str | None,
        message_params: Any,
        now: str,
        claim_token: str | None = None,
        claim_expires_at: str | None = None,
        transferred_bytes: int | None = None,
        run_after: str | None = None,
        retry_count: int | None = None,
        lease_renewed: bool = False,
    ) -> bool:
        """Move a Job to ``status``, compare-and-swap on its live lease.

        ``lease_renewed``: this transaction already renewed the lease under the
        Job row lock (a claim-fenced unit), so a held status is not renewed twice.

        With a claim the Job row is locked first and the lease checked fresh:
        a held status extends the lease, a waiting status gives it back, a
        terminal status ends the Job (the schema drops the lease). A
        cancellation, takeover or expiry that won makes this a no-op. Without
        a claim (administrative callers) only a non-terminal Job moves.
        A terminal Job never moves again.
        """
        self.transitioned_job = None
        if claim_token:
            token = self._lease_token(claim_token)
            if status in JOB_LEASE_HELD_STATUSES:
                if not claim_expires_at:
                    raise ValueError("A held Job needs its lease expiry")
                if lease_renewed:
                    pass
                elif self.renew_job_claim(
                    job_id=job_id, claim_token=token, now=now,
                    claim_expires_at=claim_expires_at, lock_job=True,
                ) is None:
                    return False
            elif self.connection.execute(
                f"""
                WITH job AS (
                    SELECT id FROM jobs
                    WHERE id=%s AND {OPEN_JOB_SQL}
                    FOR NO KEY UPDATE
                )
                DELETE FROM job_leases l USING job
                WHERE l.job_id = job.id AND l.token=%s
                  AND l.expires_at > %s::timestamptz
                RETURNING l.job_id
                """,
                (job_id, token, now),
            ).fetchone() is None:
                return False
        params = message_params
        if isinstance(params, Mapping):
            params = json.dumps(dict(params))
        result = self.connection.execute(
            f"""
            UPDATE jobs
            SET status=%s, message=%s, message_key=%s, message_params=%s::jsonb,
                updated_at=%s,
                transferred_bytes=COALESCE(%s, transferred_bytes),
                run_after=COALESCE(%s::timestamptz, run_after),
                retry_count=COALESCE(%s, retry_count)
            WHERE id=%s AND {OPEN_JOB_SQL}
            RETURNING id, file_id, vault_id, action, status, retry_count, run_after,
                      archive_version_id
            """,
            (status, message, message_key, params, now,
             None if transferred_bytes is None else max(0, int(transferred_bytes)),
             run_after, retry_count, job_id),
        )
        # The moved row, for the caller's same-transaction follow-ups (#597).
        self.transitioned_job = result.fetchone()
        return self.transitioned_job is not None

    def cancel_open_jobs(
        self,
        *,
        vault_id: int,
        job_ids: Sequence[int],
        message: str,
        message_key: str | None,
        now: str,
    ) -> list[int]:
        """Cancel non-terminal Jobs of one Vault; the caller holds the Vault lock.

        Cancellation wins on live state: it locks each Job row and ends it,
        which drops any lease (schema trigger), so the next renewal, progress
        or status write of a worker holding that lease finds nothing to update.
        """
        # A Recovery stopped while AWS restores says so: the cloud restore goes
        # on and a new Recovery resumes it from its first request (#677).
        from .recovery_progress import STOPPED_WAITING_KEY
        waiting_key = STOPPED_WAITING_KEY if message_key == "job.recovery_stopped" else message_key
        waiting_message = translate(waiting_key, locale=DEFAULT_LOCALE) if waiting_key != message_key else message
        rows = self.connection.execute(
            f"""
            UPDATE jobs
            SET status='cancelled',
                message=CASE WHEN action='recover' AND status='restoring' THEN %s ELSE %s END,
                message_key=CASE WHEN action='recover' AND status='restoring' THEN %s ELSE %s END,
                message_params='{{}}'::jsonb, updated_at=%s
            WHERE vault_id=%s AND id = ANY(%s::bigint[])
              AND {OPEN_JOB_SQL}
            RETURNING id
            """,
            (waiting_message, message, waiting_key, message_key, now, vault_id,
             [int(job_id) for job_id in job_ids]),
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def record_job_progress(
        self,
        *,
        job_id: int,
        transferred_bytes: int,
        now: str,
        claim_token: str | None = None,
        claim_expires_at: str | None = None,
    ) -> bool:
        """Persist progress; with a claim, only while the lease is live (and extend it)."""
        if claim_token:
            if self.renew_job_claim(
                job_id=job_id, claim_token=claim_token, now=now,
                claim_expires_at=claim_expires_at or now, lock_job=True,
            ) is None:
                return False
        result = self.connection.execute(
            f"""
            UPDATE jobs SET transferred_bytes=%s, updated_at=%s
            WHERE id=%s AND {OPEN_JOB_SQL}
            """,
            (max(0, int(transferred_bytes)), now, job_id),
        )
        return result.rowcount == 1

    def renew_purge_group_claim(
        self,
        *,
        vault_id: int,
        group_id: str,
        claim_token: str,
        expected_job_ids: Sequence[int],
        now: str,
        claim_expires_at: str,
    ) -> bool:
        """Hold every member of a claimed purge group through one DB step.

        The members' Job rows are locked first, in group order, then their
        leases: a cancellation of the group either wins before this point or
        waits until the surrounding transaction commits.
        """
        token = self._lease_token(claim_token)
        self.connection.execute(
            """
            SELECT id FROM jobs
            WHERE vault_id=%s AND group_id=%s AND action='cloud-purge'
              AND status='cleaning'
            ORDER BY requested_at ASC, id ASC
            FOR NO KEY UPDATE
            """,
            (vault_id, group_id),
        ).fetchall()
        rows = self.connection.execute(
            """
            UPDATE job_leases l SET expires_at=%s
            FROM jobs j
            WHERE j.id = l.job_id
              AND j.vault_id=%s AND j.group_id=%s AND j.action='cloud-purge'
              AND j.status='cleaning'
              AND l.token=%s
              AND l.expires_at > %s::timestamptz
            RETURNING l.job_id
            """,
            (claim_expires_at, vault_id, group_id, token, now),
        ).fetchall()
        return {int(row["job_id"]) for row in rows} == {
            int(job_id) for job_id in expected_job_ids
        }

    def release_purge_group_claim(
        self, *, vault_id: int, group_id: str, claim_token: str, now: str,
    ) -> list[int]:
        """Give a claimed purge group back before any provider deletion.

        Only members still leased by ``claim_token`` return to ``queued``; the
        group's consent (``cloud_purge_requests``) and deadline stay intact.
        """
        token = self._lease_token(claim_token)
        self.connection.execute(
            """
            SELECT id FROM jobs
            WHERE vault_id=%s AND group_id=%s AND action='cloud-purge'
              AND status='cleaning'
            ORDER BY requested_at ASC, id ASC
            FOR NO KEY UPDATE
            """,
            (vault_id, group_id),
        ).fetchall()
        released = self.connection.execute(
            """
            DELETE FROM job_leases l USING jobs j
            WHERE j.id = l.job_id
              AND j.vault_id=%s AND j.group_id=%s AND j.action='cloud-purge'
              AND j.status='cleaning'
              AND l.token=%s AND l.expires_at > %s::timestamptz
            RETURNING l.job_id
            """,
            (vault_id, group_id, token, now),
        ).fetchall()
        ids = [int(row["job_id"]) for row in released]
        if ids:
            self.connection.execute(
                """
                UPDATE jobs SET status='queued', updated_at=%s
                WHERE id = ANY(%s::bigint[])
                """,
                (now, ids),
            )
        return ids

    def settle_stale_job(
        self,
        *,
        job_id: int,
        status: str,
        message: str,
        now: str,
        progress: str | None = None,
    ) -> bool:
        """Publish one restart decision only if no worker holds a live lease.

        The Job row is locked first; an expired lease is then deleted under
        its own row lock, so a renewal that won keeps the Job. Any lease still
        present afterwards is live and the decision is dropped.
        """
        if progress not in {None, "reset", "complete"}:
            raise ValueError("Unknown progress update")
        if self.connection.execute(
            f"""
            SELECT id FROM jobs
            WHERE id=%s AND {OPEN_JOB_SQL}
            FOR NO KEY UPDATE
            """,
            (job_id,),
        ).fetchone() is None:
            return False
        self.connection.execute(
            "DELETE FROM job_leases WHERE job_id=%s AND expires_at <= %s::timestamptz",
            (job_id, now),
        )
        if self.connection.execute(
            "SELECT 1 FROM job_leases WHERE job_id=%s", (job_id,),
        ).fetchone() is not None:
            return False
        progress_sql = {
            None: "",
            "reset": ", transferred_bytes=0",
            "complete": ", transferred_bytes=total_bytes",
        }[progress]
        result = self.connection.execute(
            f"""
            UPDATE jobs SET status=%s, message=%s, message_key=NULL,
                message_params=NULL, updated_at=%s{progress_sql}
            WHERE id=%s AND {OPEN_JOB_SQL}
            """,
            (status, message, now, job_id),
        )
        return result.rowcount == 1

    def load_claimed_purge_group(
        self,
        *,
        lead_job_id: int,
        claim_token: str,
        now: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Load a complete, still-live cloud-purge claim and its consented targets.

        One item per member Job: its file, the exact Current it consented to
        (``archive_version_id``) and that version's key and VersionId.
        """
        lead = self.connection.execute(
            """
            SELECT vault_id, group_id
            FROM jobs
            WHERE id=%s AND action='cloud-purge'
            """,
            (lead_job_id,),
        ).fetchone()
        if lead is None or not lead.get("group_id"):
            return [], []
        all_active = self.connection.execute(
            f"""
            SELECT j.id, j.status, j.file_id,
                   l.token::text AS claim_token, l.expires_at AS claim_expires_at,
                   COALESCE(
                       j.status = 'cleaning' AND l.token = %s
                       AND l.expires_at > %s::timestamptz, FALSE
                   ) AS owned
            FROM jobs j
            LEFT JOIN job_leases l ON l.job_id = j.id
            WHERE j.vault_id=%s AND j.group_id=%s AND j.action='cloud-purge'
              AND {open_job_sql("j")}
            ORDER BY j.requested_at ASC, j.id ASC
            """,
            (self._lease_token(claim_token), now, lead["vault_id"], lead["group_id"]),
        ).fetchall()
        if not all_active or not all(row["owned"] for row in all_active):
            return [], []
        items = self.connection.execute(
            """
            SELECT j.id, j.vault_id, j.file_id, j.group_id, j.archive_version_id,
                   j.origin, f.path, av.object_key, av.provider_version_id, av.size,
                   av.created_at AS version_created_at
            FROM jobs j
            JOIN files f ON f.id = j.file_id
            LEFT JOIN archive_versions av ON av.id = j.archive_version_id
            WHERE j.id = ANY(%s::bigint[])
            ORDER BY j.requested_at ASC, j.id ASC
            """,
            ([int(row["id"]) for row in all_active],),
        ).fetchall()
        return all_active, items

    def claimable_queue_depth(
        self,
        *,
        now: str,
        restore_due_before: str,
        exclude_job_ids: Sequence[int] = (),
    ) -> int:
        """Count runnable, currently unleased Jobs.

        ``exclude_job_ids`` leaves out Jobs the worker already found not
        runnable right now, so they are not reported as work that should move.

        This is intentionally a backlog metric, not a concurrency-limited
        scheduler batch.  Leased work and stale intermediate states are absent;
        expired queued and due restoring leases are reclaimable and included.
        """
        predicate, params = self._claimable_job_predicate_sql(
            prefix="j.",
            now=now,
            restore_due_before=restore_due_before,
        )
        row = self.connection.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM jobs j
            WHERE ({predicate})
              AND j.id <> ALL(%s::bigint[])
            """,
            [*params, [int(job_id) for job_id in exclude_job_ids]],
        ).fetchone()
        return int(row["total"] or 0) if row is not None else 0

    # -- Jobs a dead worker left mid-I/O ---------------------------------------

    def list_interrupted_jobs(self, *, now: str, limit: int) -> list[dict[str, Any]]:
        """Jobs persisted mid-I/O that no live lease covers, oldest first.

        Each row carries the File's current path and its Vault's root and bucket.
        """
        return self.connection.execute(
            f"""
            SELECT j.*, f.path, v.source_root, v.uuid AS vault_uuid,
                   t.bucket AS s3_bucket, t.key_layout AS s3_key_layout
            FROM jobs j
            JOIN files f ON f.id = j.file_id
            JOIN vaults v ON v.id=j.vault_id
            JOIN vault_targets t ON t.id = v.target_id
            WHERE {INTERRUPTED_JOB_SQL}
              AND NOT EXISTS (
                  SELECT 1 FROM job_leases l
                  WHERE l.job_id = j.id AND l.expires_at > %s::timestamptz
              )
            ORDER BY j.requested_at ASC, j.id ASC
            LIMIT %s
            """,
            (now, limit),
        ).fetchall()

    def interrupted_purge_group_members(
        self, *, vault_id: int, group_id: Any, now: str,
    ) -> list[dict[str, Any]]:
        """The members of a cloud-purge group left ``cleaning`` with no live lease."""
        return self.connection.execute(
            """
            SELECT j.id FROM jobs j
            WHERE j.vault_id=%s AND j.group_id=%s AND j.action='cloud-purge'
              AND j.status='cleaning'
              AND NOT EXISTS (
                  SELECT 1 FROM job_leases l
                  WHERE l.job_id = j.id AND l.expires_at > %s::timestamptz
              )
            """,
            (vault_id, group_id, now),
        ).fetchall()

    def interrupted_job_backlog(self, *, now: str) -> dict[str, Any] | None:
        """How many interrupted Jobs wait for reconciliation, and since when.

        ``total``, the oldest expired lease (``oldest_expiry``) and the oldest
        update (``oldest_update``) among Jobs persisted mid-I/O whose lease is
        expired or missing.
        """
        return self.connection.execute(
            f"""
            SELECT COUNT(*) AS total,
                   MIN(l.expires_at) AS oldest_expiry,
                   MIN(j.updated_at) AS oldest_update
            FROM jobs j
            LEFT JOIN job_leases l ON l.job_id = j.id
            WHERE {INTERRUPTED_JOB_SQL}
              AND (l.job_id IS NULL OR l.expires_at <= %s::timestamptz)
            """,
            (now,),
        ).fetchone()


# -- The claim a worker thread holds -------------------------------------------

# A claim is intentionally durable rather than process-local.  Five minutes is
# long enough for ordinary provider calls while status/progress checkpoints renew
# it during transfers; a dead process becomes recoverable once the lease expires,
# without waiting for another restart.
JOB_CLAIM_LEASE_SECONDS = 5 * 60
# A foreground checkpoint extends the lease at most this often and otherwise
# only reads it. Far below the keeper's lease/3 cadence, so a claim that passes
# checkpoints is never closer to expiry than one that only has its keeper.
CLAIM_CHECKPOINT_RENEW_SECONDS = 15.0
EXPIRED_LEASE_RECONCILE_BATCH = 100

_worker_claim = threading.local()


class JobLeaseLost(OperationCancelled):
    """The worker no longer owns the durable claim required to publish work."""


def claim_token_for(job_id: int) -> str | None:
    active = getattr(_worker_claim, "active", None)
    if not active:
        return None
    active_id, token = active
    return str(token) if int(active_id) == int(job_id) else None


def claim_lost_event_for(job_id: int) -> threading.Event | None:
    active = getattr(_worker_claim, "active", None)
    lost = getattr(_worker_claim, "lost_event", None)
    if not active or not isinstance(lost, threading.Event):
        return None
    return lost if int(active[0]) == int(job_id) else None


def claim_is_lost(job_id: int) -> bool:
    event = claim_lost_event_for(job_id)
    return bool(event and event.is_set())


def mark_claim_lost(job_id: int) -> None:
    event = claim_lost_event_for(job_id)
    if event is not None:
        event.set()


@contextmanager
def job_claim_context(
    job: dict[str, Any],
    *,
    lost_event: threading.Event | None = None,
):
    """Make a scheduler-acquired claim and its loss signal available to helpers."""
    token = job.get("claim_token")
    if not token:
        yield
        return
    previous_active = getattr(_worker_claim, "active", None)
    previous_lost = getattr(_worker_claim, "lost_event", None)
    _worker_claim.active = (int(job["id"]), str(token))
    _worker_claim.lost_event = lost_event or threading.Event()
    try:
        yield
    finally:
        if previous_active is None:
            for attribute in ("active", "lost_event"):
                try:
                    delattr(_worker_claim, attribute)
                except AttributeError:
                    pass
        else:
            _worker_claim.active = previous_active
            _worker_claim.lost_event = previous_lost


# -- Cancellation, as this process hears it ------------------------------------
#
# Two different things stop a Job. :meth:`JobQueue.cancel_open_jobs` is the
# durable one: it ends the row and drops its lease, for every process.
# :func:`signal_job_cancellation` only tells the threads of *this* process to
# stop at their next checkpoint, without waiting for a lease check.

cancelled_jobs: set[int] = set()
_cancelled_jobs_lock = threading.Lock()


def signal_job_cancellation(job_ids: list[int]) -> None:
    """Signal SDK transfers and verification streams to stop at checkpoints."""
    with _cancelled_jobs_lock:
        cancelled_jobs.update(job_ids)


def job_cancelled(job_id: int) -> bool:
    with _cancelled_jobs_lock:
        return job_id in cancelled_jobs


def forget_job_cancellation(job_id: int) -> None:
    """Drop the in-process signal of a Job id that starts a new run."""
    with _cancelled_jobs_lock:
        cancelled_jobs.discard(job_id)


# -- Job outcome counters ------------------------------------------------------

_JOB_COUNTER_ACTIONS = frozenset(
    {
        "upload",
        "recover",
        "free-space",
        "rename",
        "cloud-purge",
        "storage-class",
    }
)
_pending_job_counters: contextvars.ContextVar[list[tuple[str, str]] | None] = (
    contextvars.ContextVar("pending_job_counters", default=None)
)


def _job_counter_action(action: str | None) -> str:
    value = str(action or "other")
    return value if value in _JOB_COUNTER_ACTIONS else "other"


def _inc_job_counter(kind: str, action: str | None) -> None:
    names = {
        "completed": "jobs_completed_total",
        "failed": "jobs_failed_total",
        "retries": "jobs_retries_total",
    }
    try:
        metrics_service.inc(names[kind], action=_job_counter_action(action))
    except Exception:
        pass


def record_committed_job_counter(kind: str, action: str | None) -> None:
    """Count a committed Job outcome, or defer until the outer transaction."""
    pending = _pending_job_counters.get()
    if pending is not None:
        pending.append((kind, _job_counter_action(action)))
        return
    _inc_job_counter(kind, action)


@contextmanager
def defer_job_counters() -> Iterator[None]:
    """Hold Job counters until the surrounding DB transaction commits."""
    items: list[tuple[str, str]] = []
    token = _pending_job_counters.set(items)
    try:
        yield
    except BaseException:
        raise
    else:
        for kind, action in items:
            _inc_job_counter(kind, action)
    finally:
        _pending_job_counters.reset(token)


# -- Transitions inside the caller's transaction -------------------------------

def transition_subject(connection: Any, job_id: int) -> dict[str, Any] | None:
    """The Job a transition moves, and whether it is unbound, in one read.

    ``unbound``: a held-status step of ``job_id`` writes nothing admission
    reads. An upload bound to an Upload Remediation episode also moves that
    episode (``upload_remediation.transition``); it keeps the exclusive Vault
    lock. Bindings are written only with the Job's creation or completion,
    never while a claimant holds it, so this unlocked read cannot go stale.
    """
    return connection.execute(
        """
        SELECT j.status, j.action, j.vault_id, f.path,
               NOT (
                   j.action='upload'
                   AND EXISTS (SELECT 1 FROM upload_remediation_jobs b WHERE b.job_id=j.id)
               ) AS unbound
        FROM jobs j JOIN files f ON f.id = j.file_id WHERE j.id=%s
        """,
        (job_id,),
    ).fetchone()


def dirty_job_protection(connection: Any, job_id: int) -> None:
    from .directory_aggregates import mark_path_dirty
    row = connection.execute(
        """SELECT j.vault_id, f.path FROM jobs j JOIN files f ON f.id = j.file_id
           WHERE j.id=%s AND j.action='upload'""", (job_id,),
    ).fetchone()
    if row:
        mark_path_dirty(connection, int(row["vault_id"]), row["path"])


def settle_interrupted_job(
    connection: Any,
    *,
    job_id: int,
    status: str,
    message: str,
    timestamp: str,
    reset_progress: bool = False,
    complete_progress: bool = False,
) -> bool:
    """Publish one restart decision only if no worker renewed the old lease."""
    updated = JobQueue(connection).settle_stale_job(
        job_id=job_id,
        status=status,
        message=message,
        now=timestamp,
        progress="reset" if reset_progress else "complete" if complete_progress else None,
    )
    if updated:
        dirty_job_protection(connection, job_id)
    if updated and status in {"completed", "failed"}:
        row = connection.execute(
            "SELECT action FROM jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
        record_committed_job_counter(
            status, row["action"] if row is not None else None
        )
    return updated


# -- The queue as one process runs it ------------------------------------------

class ClaimLeaseHeartbeat:
    """Renew a claimed Job while CPU/provider work has no progress callback."""

    def __init__(self, runner: "QueueRunner", job: dict[str, Any]) -> None:
        self.runner = runner
        self.job_id = int(job["id"])
        self.claim_token = str(job["claim_token"]) if job.get("claim_token") else None
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "ClaimLeaseHeartbeat":
        if self.claim_token:
            self._thread = threading.Thread(
                target=self._run,
                name=f"job-lease-{self.job_id}",
                daemon=True,
            )
            self._thread.start()
        return self

    def _run(self) -> None:
        # Use a fraction of the lease rather than a fixed setting so tests and
        # future policy changes cannot silently make a live transfer reclaimable.
        interval = max(0.1, JOB_CLAIM_LEASE_SECONDS / 3)
        while not self._stop.wait(interval):
            if shutdown_state.requested:
                return
            try:
                if self.runner.renew_claim_if_owned(self.job_id, str(self.claim_token)) is None:
                    self.lost.set()
                    return
            except Exception:
                # The foreground worker observes this shared signal at its next
                # checkpoint and fences all post-provider catalog/audit writes.
                self.lost.set()
                return

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)


class QueueRunner:
    """The Job queue as one process runs it: its pool, its clock, its claims.

    ``db`` opens a catalog connection of the process (a context manager that
    commits on a clean exit); ``now_iso`` is its clock, in the representation
    of Job rows. Every unit below is one transaction of its own unless its
    name says ``in_transaction``.

    A worker asks three things: :meth:`claim` (take this Job),
    :meth:`set_job` and its siblings (record the outcome, compare-and-swap on
    the claim), :meth:`reconcile_interrupted_jobs` (settle what a dead worker
    left mid-I/O).
    """

    def __init__(self, *, db: Callable[[], Any], now_iso: Callable[[], str]) -> None:
        self._db = db
        self._now_iso = now_iso

    # -- Claim and lease -------------------------------------------------------

    def claim_expiry_at(self, timestamp: str | None = None) -> str:
        """Build an ISO deadline using the same UTC representation as Job rows."""
        base = datetime.fromisoformat(timestamp or self._now_iso())
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        return (base + timedelta(seconds=JOB_CLAIM_LEASE_SECONDS)).isoformat()

    def claim(self, job: dict[str, Any], *, restore_due_before: Callable[[], str]) -> bool:
        """Take the nominated ``job``: True when this worker now holds its lease.

        Nomination is not acquisition: the row is rechecked by the conditional
        claim of :class:`JobQueue`, so two processes may nominate the same Job
        but only one receives a claim token. A cloud-purge Job claims its whole
        group. On success ``job`` carries its claim (token, expiry, status).
        """
        claimed_at = self._now_iso()
        claim_token = str(uuid.uuid4())
        claim_expires_at = self.claim_expiry_at(claimed_at)
        generation = job.get('scheduler_generation') if turn_is_aged(job) else None
        with self._db() as connection:
            queue = JobQueue(connection)
            if job["action"] == "cloud-purge":
                group = queue.claim_purge_group(
                    lead_job_id=int(job["id"]),
                    claim_token=claim_token,
                    claimed_at=claimed_at,
                    claim_expires_at=claim_expires_at,
                    now=claimed_at,
                    message=translate(
                        "job.cloud_purge_deleting", locale=DEFAULT_LOCALE
                    ),
                    message_key="job.cloud_purge_deleting",
                    scheduler_generation=generation,
                )
                lead = next(
                    (row for row in group if int(row["id"]) == int(job["id"])),
                    None,
                )
                if lead is None:
                    return False
                job.update(lead)
            else:
                claimed = queue.claim_job(
                    job_id=int(job["id"]),
                    claim_token=claim_token,
                    claimed_at=claimed_at,
                    claim_expires_at=claim_expires_at,
                    now=claimed_at,
                    restore_due_before=restore_due_before(),
                    scheduler_generation=generation,
                )
                if claimed is None:
                    return False
                job.update(claimed)
        return True

    @contextmanager
    def hold_claim(self, job: dict[str, Any]) -> Iterator[None]:
        """Run a Job with its durable claim kept alive and bound to this thread."""
        with ClaimLeaseHeartbeat(self, job) as heartbeat:
            with job_claim_context(job, lost_event=heartbeat.lost):
                yield

    @retry_database_unit
    def renew_claim_if_owned(self, job_id: int, claim_token: str) -> dict[str, Any] | None:
        timestamp = self._now_iso()
        with owned_connection(self._db()) as connection:
            return JobQueue(connection).renew_job_claim(
                job_id=int(job_id),
                claim_token=claim_token,
                now=timestamp,
                claim_expires_at=self.claim_expiry_at(timestamp),
            )

    @retry_database_unit
    def claim_is_live(self, job_id: int, claim_token: str) -> bool:
        with owned_connection(self._db()) as connection:
            return JobQueue(connection).job_claim_is_live(
                job_id=int(job_id), claim_token=claim_token, now=self._now_iso(),
            )

    def checkpoint_claim(self, job_id: int, claim_token: str) -> bool:
        """Durable ownership checkpoint of the foreground worker's own claim.

        Every checkpoint still asks PostgreSQL whether the claim is live, so
        cancellation, takeover and expiry are observed exactly as before. Only the
        lease *extension* is amortized: a small upload passes ~20 checkpoints in a
        few seconds, and extending a five-minute lease each time cost one WAL-logged
        non-HOT ``jobs`` update and a synchronous commit per checkpoint.
        """
        renewed = getattr(_worker_claim, "renewed", None)
        current = time.monotonic()
        if (
            renewed is not None and renewed[0] == (int(job_id), claim_token)
            and current - renewed[1] < CLAIM_CHECKPOINT_RENEW_SECONDS
        ):
            return self.claim_is_live(job_id, claim_token)
        if self.renew_claim_if_owned(job_id, claim_token) is None:
            return False
        _worker_claim.renewed = ((int(job_id), claim_token), current)
        return True

    def renew_started_claim(self, job: dict[str, Any]) -> dict[str, Any] | None:
        """The first extension of a run's claim: the Job's status, None when lost.

        One DB unit; the caller replays it on a serialization failure.
        """
        claim_token = job["claim_token"]
        with owned_connection(self._db()) as connection:
            renewed = JobQueue(connection).renew_job_claim(
                job_id=int(job["id"]), claim_token=str(claim_token),
                now=self._now_iso(), claim_expires_at=self.claim_expiry_at(),
            )
        if renewed is not None and claim_token_for(int(job["id"])) == str(claim_token):
            # This is the run's first extension: the next checkpoints read the
            # lease and extend it again only after the usual interval.
            _worker_claim.renewed = ((int(job["id"]), str(claim_token)), time.monotonic())
        return renewed

    def ensure_job_active(self, job_id: int, message: str) -> None:
        """Stop work when local cancellation, DB cancellation, or lease loss wins.

        ``signal_job_cancellation`` is a fast same-process signal.  The durable
        claim check is also required because another application process may
        cancel or reclaim the row while this worker is inside a long-running
        operation.
        """
        shutdown_state.checkpoint()
        if job_cancelled(job_id):
            raise OperationCancelled(message)
        if claim_is_lost(job_id):
            raise JobLeaseLost(message)
        claim_token = claim_token_for(job_id)
        if claim_token and not self.checkpoint_claim(job_id, claim_token):
            mark_claim_lost(job_id)
            raise JobLeaseLost(message)

    def ensure_job_claim_owned_in_transaction(
        self,
        connection: Any,
        job: dict[str, Any],
        message: str,
    ) -> None:
        """Fence a catalog/audit transaction behind the currently owned lease.

        The conditional renewal locks the Job row, then its lease, until the
        surrounding transaction commits (the claim's lock order).  A cancellation/takeover that won first therefore
        prevents every post-provider catalog or audit write in that transaction.
        """
        shutdown_state.checkpoint()
        job_id = int(job["id"])
        if job_cancelled(job_id):
            raise OperationCancelled(message)
        if claim_is_lost(job_id):
            raise JobLeaseLost(message)
        claim_token = job.get("claim_token")
        if not claim_token:
            return
        timestamp = self._now_iso()
        current = JobQueue(connection).renew_job_claim(
            job_id=job_id,
            claim_token=str(claim_token),
            now=timestamp,
            claim_expires_at=self.claim_expiry_at(timestamp),
            lock_job=True,
        )
        if current is None:
            mark_claim_lost(job_id)
            raise JobLeaseLost(message)

    # -- Outcome ---------------------------------------------------------------

    @retry_database_unit
    def set_job(
        self,
        job_id: int,
        status: str,
        message: str = "",
        *,
        message_key: str | None = None,
        message_params: dict[str, Any] | None = None,
        remediation_suspension: str | None = None,
        remediation_reason: str | None = None,
    ) -> bool:
        """Persist a state transition without letting a cancelled/lost claim win.

        Worker calls inherit their token from ``job_claim_context``.  A status
        change then becomes a compare-and-swap: a concurrent cancellation or a new
        claimant makes this a harmless no-op instead of resurrecting the Job.
        Direct administrative/test callers without a claim retain the historic
        update behavior.
        """
        if claim_is_lost(job_id):
            return False
        with defer_job_counters():
            with owned_connection(self._db()) as connection:
                return self.set_job_in_transaction(
                    connection, job_id, status, message,
                    message_key=message_key, message_params=message_params,
                    remediation_suspension=remediation_suspension,
                    remediation_reason=remediation_reason,
                )

    def set_job_in_transaction(
        self,
        connection: Any, job_id: int, status: str, message: str = "", *,
        message_key: str | None = None,
        message_params: dict[str, Any] | None = None,
        remediation_suspension: str | None = None,
        remediation_reason: str | None = None,
        transferred_bytes: int | None = None,
        fenced_unit: bool = False,
    ) -> bool:
        """DB-only transition; caller owns commit/retry and deferred counters.

        This is the same transition used by set_job, not a second completion path.
        Terminal/retry transitions still lock the Vault exclusively; never invoke
        from an audited SHARE callback. Final upload progress can share its CAS.

        ``fenced_unit``: the caller is an exclusive claim-fenced upload unit
        (``_upload_catalog_unit``), which already holds the Vault row NO KEY UPDATE
        and renewed this lease under the Job row lock in the same transaction. The
        Vault lock and a held status's second renewal are then not asked again; a
        terminal status still ends the lease by its own CAS.
        """
        params = message_params or {}
        if message_key and not message:
            message = translate(message_key, locale=DEFAULT_LOCALE, **params)
        timestamp = self._now_iso()
        if claim_is_lost(job_id):
            return False
        claim_token = claim_token_for(job_id)
        held = status in JOB_LEASE_HELD_STATUSES
        previous_status = None
        action = None
        vault_lock = "NO KEY UPDATE"
        current = None
        if (held and claim_token
                and remediation_suspension is None and remediation_reason is None):
            # A held step reads its subject before the Vault lock, whose mode it
            # decides; its previous status feeds no counter.
            current = transition_subject(connection, job_id)
        # An unbound held step moves no Upload Remediation episode (see below).
        unbound_held = bool(current and current["unbound"])
        if unbound_held:
            # ADR-0031: a held step changes no admission predicate. Keep Vault-first
            # order before locking Jobs or append-only aggregate dirty FK marks.
            vault_lock = "KEY SHARE"
        if not fenced_unit:
            connection.execute(
                f"SELECT id FROM vaults WHERE id=(SELECT vault_id FROM jobs WHERE id=%s) FOR {vault_lock}",
                (job_id,),
            )
        if current is None:
            current = transition_subject(connection, job_id)
        if current is not None:
            previous_status, action = current["status"], current["action"]
        queue = JobQueue(connection)
        updated = queue.transition_job(
            job_id=job_id,
            status=status,
            message=message,
            message_key=message_key,
            message_params=format_message_params(params) if message_key else None,
            now=timestamp,
            claim_token=claim_token,
            claim_expires_at=self.claim_expiry_at(timestamp) if held else None,
            transferred_bytes=transferred_bytes,
            lease_renewed=fenced_unit,
        )
        if updated and action == "upload":
            from . import upload_remediation
            from .directory_aggregates import mark_path_dirty
            if not unbound_held:
                # Bindings are written only with the Job's creation or completion:
                # an unbound held step has no episode to move (ADR-0035, #592).
                upload_remediation.transition(
                    connection, job_id, status, timestamp,
                    suspension=remediation_suspension, closing_reason=remediation_reason,
                    bound=not current["unbound"], job=queue.transitioned_job,
                )
            # Same row as dirty_job_protection would read again.
            mark_path_dirty(connection, int(current["vault_id"]), current["path"])
            if remediation_reason == "file_removed" and current is not None:
                from ..catalog import ArchiveCatalog
                ArchiveCatalog(connection).mark_local_path_missing(
                    vault_id=int(current["vault_id"]), path=current["path"], observed_at=timestamp,
                )
        if updated and status in {"completed", "failed"}:
            if action == "upload" and status == "completed":
                # A completed upload notifies no one (see
                # enqueue_job_terminal_notification): only leave the outbox.
                notification_service.mark_terminal_notification_done(connection, job_id=job_id)
            else:
                notification_service.enqueue_job_terminal_notification_best_effort(
                    connection, job_id=job_id)
            if previous_status not in TERMINAL_JOB_STATUSES:
                record_committed_job_counter("completed" if status == "completed" else "failed", action)
        return updated

    def set_job_message_in_transaction(self, connection: Any, job_id: int, message_key: str) -> None:
        """Change only the visible message of a claimed, running Job.

        No status, lease, protection or remediation change: the caller's
        claim-fenced unit already holds the Job row. A Job that moved on keeps its
        own message (the CAS matches only the status the run is in).
        """
        connection.execute(
            """UPDATE jobs SET message=%s, message_key=%s, message_params=NULL, updated_at=%s
               WHERE id=%s AND status IN ('uploading', 'verifying')""",
            (translate(message_key, locale=DEFAULT_LOCALE), message_key, self._now_iso(), job_id),
        )

    @retry_database_unit
    def schedule_upload_retry(
        self,
        job_id: int,
        *,
        message: str = "",
        message_key: str | None = None,
        message_params: dict[str, Any] | None = None,
        retry_count: int,
        delay_seconds: int | None = None,
        remediation_suspension: str | None = None,
    ) -> bool:
        delay = (
            int(delay_seconds)
            if delay_seconds is not None
            else upload_retry_delay_seconds(retry_count)
        )
        # Bounded jitter keeps thundering herds apart without changing the base delay.
        jitter = delay * 0.2 * (uuid.uuid4().int % 1000) / 1000.0
        retry_after = (
            datetime.now(timezone.utc) + timedelta(seconds=delay + jitter)
        ).isoformat()
        params = message_params or {}
        if message_key and not message:
            message = translate(message_key, locale=DEFAULT_LOCALE, **params)
        timestamp = self._now_iso()
        if claim_is_lost(job_id):
            return False
        claim_token = claim_token_for(job_id)
        action = None
        with owned_connection(self._db()) as connection:
            # Stays exclusive (ADR-0031, #535): releasing the lease returns the
            # Job to the unstarted set ('retrying') that cancel-pending and the
            # scheduler select, and moves a bound remediation episode; sharing
            # was not proven safe, and retries are rare.
            connection.execute("SELECT id FROM vaults WHERE id=(SELECT vault_id FROM jobs WHERE id=%s) FOR NO KEY UPDATE", (job_id,))
            current = connection.execute(
                "SELECT action FROM jobs WHERE id=%s",
                (job_id,),
            ).fetchone()
            if current is not None:
                action = current["action"]
            updated = JobQueue(connection).transition_job(
                job_id=job_id,
                status="retrying",
                message=message,
                message_key=message_key,
                message_params=format_message_params(params) if message_key else None,
                now=timestamp,
                claim_token=claim_token,
                run_after=retry_after,
                retry_count=retry_count,
            )
            if updated and action == "upload":
                from . import upload_remediation
                upload_remediation.transition(connection, job_id, "retrying", timestamp, suspension=remediation_suspension)
                dirty_job_protection(connection, job_id)
        if updated and not remediation_suspension:
            # A wait for the Source Volume is not a retry of the transfer.
            record_committed_job_counter("retries", action)
        return updated

    @retry_database_unit
    def set_job_progress(
        self,
        job_id: int,
        transferred_bytes: int,
        *,
        claim_token: str | None = None,
    ) -> bool:
        timestamp = self._now_iso()
        if claim_is_lost(job_id):
            return False
        claim_token = claim_token or claim_token_for(job_id)
        with owned_connection(self._db()) as connection:
            return JobQueue(connection).record_job_progress(
                job_id=job_id,
                transferred_bytes=transferred_bytes,
                now=timestamp,
                claim_token=claim_token,
                claim_expires_at=self.claim_expiry_at(timestamp),
            )

    # -- Jobs a dead worker left mid-I/O ---------------------------------------

    def reconcile_interrupted_jobs(
        self,
        *,
        reachable: Callable[[dict[str, Any]], bool],
        reconcile_job: Callable[..., str],
        reconcile_purge_group: Callable[..., dict[str, int]],
    ) -> dict[str, int]:
        """Reconcile expired or legacy durable worker states.

        A live lease is authoritative even when another process is sweeping.  The
        explicit action/state matrix covers every operation state which can be
        persisted mid-I/O; waiting states (retrying, pending approval/delay, and
        Glacier restoring) remain scheduler-owned and are not reset.  Each call
        is bounded so a large backlog of expired leases cannot monopolize a poll.

        The queue knows which Jobs were interrupted and publishes each decision
        only while no worker holds a live lease (:func:`settle_interrupted_job`);
        what the Local Copy or the bucket says about one Job is the worker's:
        ``reachable(job)`` is False while the Job's tree must not be touched
        (the Job stays as it is), ``reconcile_job(connection, job,
        timestamp=...)`` settles one Job and returns its outcome,
        ``reconcile_purge_group(connection, lead, timestamp=...)`` settles a
        whole cloud-purge group and returns its counts.
        """
        with defer_job_counters():
            return self._reconcile_interrupted_jobs(
                reachable, reconcile_job, reconcile_purge_group)

    def _reconcile_interrupted_jobs(
        self,
        reachable: Callable[[dict[str, Any]], bool],
        reconcile_job: Callable[..., str],
        reconcile_purge_group: Callable[..., dict[str, int]],
    ) -> dict[str, int]:
        summary = {"completed": 0, "requeued": 0, "failed": 0}
        seen_purge_groups: set[tuple[int, str]] = set()
        with self._db() as connection:
            timestamp = self._now_iso()
            queue = JobQueue(connection)
            jobs = queue.list_interrupted_jobs(
                now=timestamp, limit=EXPIRED_LEASE_RECONCILE_BATCH)

            for job in jobs:
                # A cloud-purge group must be reconciled as one operation, not once
                # per member, otherwise one restart could split its exact-version
                # postconditions across competing retries.
                if job["action"] == "cloud-purge":
                    group_key = (int(job["vault_id"]), str(job.get("group_id") or ""))
                    if group_key in seen_purge_groups:
                        continue
                    seen_purge_groups.add(group_key)
                    try:
                        result = reconcile_purge_group(
                            connection, job, timestamp=timestamp
                        )
                        for key, value in result.items():
                            summary[key] += value
                    except Exception as exc:
                        # A provider postcondition we cannot prove is never retried
                        # blindly.  Mark every stale group member terminal instead.
                        group_rows = queue.interrupted_purge_group_members(
                            vault_id=job["vault_id"], group_id=job.get("group_id"),
                            now=timestamp)
                        for row in group_rows:
                            if settle_interrupted_job(
                                connection,
                                job_id=int(row["id"]),
                                status="failed",
                                message=f"Post-restart reconciliation failed: {exc}",
                                timestamp=timestamp,
                            ):
                                notification_service.enqueue_job_terminal_notification_best_effort(
                                    connection, job_id=int(row["id"])
                                )
                                summary["failed"] += 1
                    continue

                if not reachable(job):
                    # Keep interrupted local work suspended without touching the
                    # absent, inaccessible, or replaced tree.
                    continue
                try:
                    outcome = reconcile_job(connection, job, timestamp=timestamp)
                    if outcome in summary:
                        summary[outcome] += 1
                except Exception as exc:
                    if settle_interrupted_job(
                        connection,
                        job_id=int(job["id"]),
                        status="failed",
                        message=f"Post-restart reconciliation failed: {exc}",
                        timestamp=timestamp,
                    ):
                        notification_service.enqueue_job_terminal_notification_best_effort(
                            connection, job_id=int(job["id"])
                        )
                        summary["failed"] += 1
        return summary
