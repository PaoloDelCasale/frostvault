"""The background scheduler: one loop that keeps the worker's slots filled and
submits every piece of periodic maintenance when it is due (#795).

A cycle dispatches Jobs, delivers notifications, submits the declared
periodic workers whose interval elapsed, observes the backlog, submits the
scan, the audit and the metadata backups from their persisted ages, then
waits for a completion or its poll deadline. The work itself belongs to the
host: what the scheduler asks of it is ``SchedulerHost``, and it imports
nothing of storage. ``app.storage`` gives the production Adapter, whose
methods resolve the storage functions when called.
"""
from __future__ import annotations

import asyncio
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from functools import partial
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..config import settings
from . import health as health_service
from . import metadata_backups as metadata_backup_service
from . import metrics as metrics_service
from . import protection_stop as protection_stop_service
from . import source_layout, vault_relocation
from .periodic_workers import PeriodicWorker

MAINTENANCE_TASKS = ("scan", "audit", "backup", "backup_verify")

# Clean cycles in a row that close the loop's internal-error problem (#711):
# more than one, so an error that recurs every few cycles stays one episode
# instead of reopening (and emailing) again each time.
BACKGROUND_LOOP_OK_CYCLES = 3


class SchedulerHost(Protocol):
    """What the scheduler asks of the worker it schedules."""

    # Seconds between two folds of the directory aggregates.
    directory_aggregate_seconds: float
    # A deep backlog is counted this often instead of at every cycle.
    backlog_observe_seconds: float
    # The longest a cycle waits, whatever the queue poll interval.
    notification_max_latency_seconds: float
    # The periodic workers submitted to the maintenance pool, in this order.
    workers: Sequence[PeriodicWorker]

    def maintenance_ages(self) -> Mapping[str, float | None]:
        """Seconds since each of ``MAINTENANCE_TASKS`` last ran; None when never."""

    def forget_audit_retry(self) -> None:
        """A new loop starts from the persisted ages, not from an earlier retry."""

    def completion_event(self, loop: asyncio.AbstractEventLoop) -> AbstractContextManager[asyncio.Event]:
        """The event a finished operation sets, for the life of the loop."""

    def dispatch_jobs(self) -> None:
        """Fill the free operation slots, without waiting for the Jobs."""

    def deliver_notifications(self) -> None: ...

    def submit_directory_aggregates(self) -> None:
        """Fold the directory aggregates on their own thread, one fold at a time."""

    def submit(self, name: str, func: Callable[[], Any]) -> None:
        """Run ``func`` on the maintenance pool unless ``name`` is still running."""

    def run_worker(self, worker: PeriodicWorker) -> None:
        """One pass of a periodic worker, recording its health."""

    def claimable_queue_depth(self, **filters: Any) -> int: ...

    def observe_queue_stall(self, runnable: int) -> None: ...

    def observe_vault_stalls(self) -> None: ...

    def observe_stuck_lease_jobs(self) -> tuple[int, float]: ...

    def observe_failed_job_backlog(self) -> tuple[int, float]: ...

    def scan_all_vaults(self, *, startup: bool = False) -> None:
        """Scan every active Vault; the ``startup`` scan leaves to the watchers
        the Vaults they scan anyway (#862)."""

    def audit_all_vaults(self) -> None: ...

    def audit_retry_due(self) -> bool: ...

    def audit_vaults_with_error(self) -> None: ...

    def run_scheduled_metadata_backup(self) -> None: ...

    def verify_latest_metadata_backup(self) -> None: ...

    async def refill_until_deadline(self, wakeup: asyncio.Event, delay: float) -> None:
        """Wait ``delay``, refilling the slots at every completion meanwhile."""

    def record_error(self, exc: Exception) -> None:
        """Record a failed cycle as the loop's own failure; never raises."""

    def closes_problem(self, clean: bool, streak: int) -> tuple[int, bool]:
        """The new clean-cycle streak, and whether the loop's problem closes now."""

    def note_ok(self) -> None:
        """Close the loop's own failure."""


def _seconds_since(
    parse: Callable[[Any], datetime | None], value: Any, *, now: datetime | None = None,
) -> float | None:
    parsed = parse(value)
    if parsed is None:
        return None
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - parsed).total_seconds())


def persisted_maintenance_ages(
    db: Callable[[], Any], parse: Callable[[Any], datetime | None], *, now: datetime | None = None,
) -> dict[str, float | None]:
    """Return seconds since each maintenance task last ran, or None when never.

    The full scan is always due at startup: it is the reconciliation net for
    events missed while the process was down (for the Vaults a watcher will
    scan once registered, that watcher's paired scan is, #862).  Audit,
    scheduled backup, and
    restore verification are read back from their durable records so a
    restart neither skips an overdue run nor repeats a recent one.
    """
    ages: dict[str, float | None] = {name: None for name in MAINTENANCE_TASKS}
    with db() as connection:
        vaults = connection.execute(
            """
            SELECT id FROM vaults WHERE lifecycle='active'
            """
        ).fetchall()
        audit_ages: list[float] = []
        for vault in vaults:
            # From the last success: a failed audit has not run, and a Vault
            # it left unconfirmed is audited again at startup (#620).
            row = connection.execute(
                """
                SELECT last_success_at, last_error FROM vault_component_health
                WHERE vault_id=%s AND component='audit'
                """,
                (int(vault["id"]),),
            ).fetchone()
            age = (
                _seconds_since(parse, row["last_success_at"], now=now)
                if row and not row["last_error"] else None
            )
            if age is None:
                audit_ages = []
                break
            audit_ages.append(age)
        if vaults and audit_ages:
            # The Vault audited longest ago decides when the next pass is due.
            ages["audit"] = max(audit_ages)
        backup = connection.execute(
            """
            SELECT MAX(created_at) AS at FROM metadata_backup_runs
            WHERE reason='scheduled'
            """
        ).fetchone()
        ages["backup"] = _seconds_since(parse, backup["at"], now=now) if backup else None
        verify = connection.execute(
            "SELECT MAX(verified_at) AS at FROM metadata_backup_runs"
        ).fetchone()
        ages["backup_verify"] = (
            _seconds_since(parse, verify["at"], now=now) if verify else None
        )
    return ages


def initial_maintenance_marks(
    current: float, ages: Mapping[str, float | None]
) -> dict[str, float]:
    """Translate persisted ages into loop-clock marks for the scheduler.

    ``loop.time()`` is a monotonic clock whose zero is arbitrary (seconds
    since boot on Linux), so a literal ``0.0`` mark would tie every schedule
    to host uptime.  A task that never ran gets ``-inf`` and is due at once.
    """
    marks: dict[str, float] = {}
    for name in MAINTENANCE_TASKS:
        age = ages.get(name)
        marks[name] = float("-inf") if age is None else float(current) - float(age)
    return marks


async def run(host: SchedulerHost) -> None:
    await asyncio.sleep(5)
    loop = asyncio.get_running_loop()
    with host.completion_event(loop) as wakeup:
        await run_cycles(host, loop, wakeup)


async def run_cycles(host: SchedulerHost, loop: asyncio.AbstractEventLoop, wakeup: asyncio.Event) -> None:
    try:
        ages = await asyncio.to_thread(host.maintenance_ages)
    except Exception:
        ages = {}
    marks = initial_maintenance_marks(loop.time(), ages)
    host.forget_audit_retry()
    last_scan = marks["scan"]
    # A scan age nobody recorded makes the first scan the startup scan.
    startup_scan = ages.get("scan") is None
    last_audit = marks["audit"]
    last_backup = marks["backup"]
    last_backup_verify = marks["backup_verify"]
    backlog_observed_at: float | None = None
    backlog_runnable = 0
    last_aggregates: float | None = None
    # When each periodic worker was last submitted, by its place in the list.
    workers = host.workers
    last_submitted: list[float | None] = [None] * len(workers)
    clean_cycles = 0
    try:
        while True:
            # Clear before dispatch, not before waiting: a completion arriving
            # during the normal cycle must still trigger a prompt refill.
            wakeup.clear()
            runnable_count = 0
            clean = True
            runtime = settings
            health_service.mark_worker_heartbeat()
            metrics_service.set_gauge("worker_up", 1)
            try:
                # Reconcile Source Volume identity even when filesystem watchers are
                # disabled, before workers or scheduled scans can touch local data.
                await asyncio.to_thread(source_layout.verify_mounts_once)
                # A v1 root identity left while its Source Volume was down (#784).
                await asyncio.to_thread(vault_relocation.upgrade_pending_legacy_root_identities)
                await asyncio.to_thread(host.dispatch_jobs)
                await asyncio.to_thread(host.deliver_notifications)
                # Converge durable directory aggregate dirty/rebuild work off the
                # /api/files request path (issue #229), one batch per interval
                # rather than one per cycle (#622), on its own thread: a large
                # Vault's fold takes seconds, and this loop must keep refilling
                # the operation slots meanwhile (#696).
                if (last_aggregates is None
                        or loop.time() - last_aggregates >= host.directory_aggregate_seconds):
                    last_aggregates = loop.time()
                    host.submit_directory_aggregates()
                await asyncio.to_thread(protection_stop_service.reconcile_all)
                # Each worker runs off the loop, one run of it at a time, at
                # its own interval; the first cycle submits them all.
                for index, worker in enumerate(workers):
                    last = last_submitted[index]
                    if last is None or loop.time() - last >= worker.interval():
                        last_submitted[index] = loop.time()
                        host.submit(worker.name, partial(host.run_worker, worker))
                # Backlog is measured independently from the selected batch: an
                # active lease is no longer claimable, while every remaining due
                # queued row is visible even when concurrency is small. Stuck
                # intermediate Jobs with expired leases are a separate series.
                # Each of these counts reads the whole backlog. While the queue
                # is deep the loop spins every 100 ms, so counting on every
                # cycle costs more the more work is waiting. A deep queue is
                # sampled; a short one is still counted exactly every cycle.
                current = loop.time()
                if (
                    backlog_observed_at is None
                    or backlog_runnable < max(10, runtime.operation_concurrency * 10)
                    or current - backlog_observed_at >= host.backlog_observe_seconds
                ):
                    queued_count = await asyncio.to_thread(host.claimable_queue_depth)
                    metrics_service.set_gauge("queue_depth", float(queued_count))
                    runnable_count = await asyncio.to_thread(
                        host.claimable_queue_depth, exclude_deferred=True
                    )
                    await asyncio.to_thread(host.observe_queue_stall, runnable_count)
                    # Here, not in the thread doing the work: a Vault held by
                    # stuck background work is still noticed (#759).
                    await asyncio.to_thread(host.observe_vault_stalls)
                    stuck_count, stuck_age = await asyncio.to_thread(
                        host.observe_stuck_lease_jobs
                    )
                    metrics_service.set_gauge("jobs_stuck", float(stuck_count))
                    metrics_service.set_gauge(
                        "jobs_stuck_oldest_age_seconds", stuck_age
                    )
                    failed_count, failed_age = await asyncio.to_thread(
                        host.observe_failed_job_backlog
                    )
                    metrics_service.set_gauge(
                        "jobs_failed_backlog", float(failed_count)
                    )
                    metrics_service.set_gauge(
                        "jobs_failed_oldest_age_seconds", failed_age
                    )
                    backlog_observed_at, backlog_runnable = current, runnable_count
                else:
                    runnable_count = backlog_runnable
                current = loop.time()
                if current - last_scan >= runtime.scan_interval:
                    host.submit("scan", partial(host.scan_all_vaults, startup=startup_scan))
                    startup_scan = False
                    last_scan = current
                if current - last_audit >= runtime.audit_interval:
                    host.submit("audit", host.audit_all_vaults)
                    last_audit = current
                elif host.audit_retry_due():
                    host.submit("audit_retry", host.audit_vaults_with_error)
                if (
                    runtime.metadata_backup_interval_seconds > 0
                    and current - last_backup
                    >= runtime.metadata_backup_interval_seconds
                ):
                    host.submit("backup", host.run_scheduled_metadata_backup)
                    last_backup = current
                if (
                    runtime.metadata_backup_verify_interval_seconds > 0
                    and current - last_backup_verify
                    >= runtime.metadata_backup_verify_interval_seconds
                ):
                    host.submit("backup_verify", host.verify_latest_metadata_backup)
                    last_backup_verify = current
                    metadata_backup_service.mark_verification_attempt()
                await asyncio.to_thread(host.deliver_notifications)
            except Exception as exc:
                clean = False
                await asyncio.to_thread(host.record_error, exc)
            batch_size = max(10, runtime.operation_concurrency * 10)
            # Poll fast only for work the worker can start now: Jobs set aside as
            # not runnable must not keep the loop spinning every 100 ms (#493).
            delay = (
                0.1
                if runnable_count >= batch_size
                else min(
                    host.notification_max_latency_seconds,
                    max(0.1, float(runtime.queue_poll_interval or 2)),
                )
            )
            try:
                await host.refill_until_deadline(wakeup, delay)
            except Exception as exc:
                clean = False
                await asyncio.to_thread(host.record_error, exc)
            clean_cycles, close = host.closes_problem(clean, clean_cycles)
            if close:
                await asyncio.to_thread(host.note_ok)
    except asyncio.CancelledError:
        # Lifespan owns pool draining after all producers have been cancelled.
        # Detaching here would lose the running futures before they can drain.
        raise
