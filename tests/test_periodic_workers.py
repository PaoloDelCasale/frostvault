"""The periodic workers and the background scheduler, as seen from outside (#795).

What each worker runs in a pass and on which thread, what it reports when the
pass fails and when it succeeds, how long it waits, and what stopping it does;
then the scheduler: the order of one cycle, the cadence of what it submits and
how it records its own failures. Nothing here needs PostgreSQL or S3: the work
itself is replaced, the workers and the scheduler are the real ones, entered
through the names ``app.main`` starts.
"""
from __future__ import annotations

import asyncio
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import storage
from app.services import bulk_upload_admission, history_retention, periodic_workers, upload_remediation
from tests.fixtures import override_settings


def thread_kind() -> str:
    """``loop`` on the event loop's thread, else the pool the thread belongs to."""
    name = threading.current_thread().name
    return "loop" if name == "MainThread" else name.rsplit("_", 1)[0]


class WorkerLoopCase(unittest.IsolatedAsyncioTestCase):
    """Two passes of one worker: the first fails at its last step, the second succeeds."""

    def setUp(self) -> None:
        self.events: list[tuple] = []
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch.object(
            storage, "_report_worker_error",
            side_effect=lambda *args: self.events.append(("error", args[0], str(args[1]), *args[2:], thread_kind()))))
        self.stack.enter_context(patch.object(
            storage, "_note_worker_ok",
            side_effect=lambda *args: self.events.append(("ok", *args, thread_kind()))))

    def step(self, owner, name: str, *, fail_first: bool = False):
        calls = 0

        def run(*args, **kwargs):
            nonlocal calls
            calls += 1
            self.events.append((name, args, kwargs, thread_kind()))
            if fail_first and calls == 1:
                raise RuntimeError(f"{name} failed")

        self.stack.enter_context(patch.object(owner, name, side_effect=run))

    async def two_passes(self, worker) -> list[tuple]:
        async def sleep(seconds):
            self.events.append(("sleep", seconds))
            if sum(event[0] == "sleep" for event in self.events) == 2:
                raise asyncio.CancelledError

        with patch.object(asyncio, "sleep", new=AsyncMock(side_effect=sleep)):
            with self.assertRaises(asyncio.CancelledError):
                await worker()
        return self.events


class WorkerLoopTests(WorkerLoopCase):
    async def test_cloud_preflight(self) -> None:
        self.step(storage, "reprobe_cloud_readiness_once", fail_first=True)
        self.assertEqual(await self.two_passes(storage.cloud_preflight_loop), [
            ("reprobe_cloud_readiness_once", (), {}, "asyncio"),
            # The only worker that reports on the event loop's own thread.
            ("error", "cloud_preflight", "reprobe_cloud_readiness_once failed", "loop"),
            ("sleep", 1),
            ("reprobe_cloud_readiness_once", (), {}, "asyncio"),
            ("ok", "cloud_preflight", "asyncio"),
            ("sleep", 1),
        ])

    async def test_upload_remediation(self) -> None:
        self.step(upload_remediation, "sweep")
        self.step(upload_remediation, "observe", fail_first=True)
        self.assertEqual(await self.two_passes(storage.upload_remediation_loop), [
            ("sweep", (storage.db,), {}, "upload-remediation"),
            ("observe", (storage.db,), {}, "upload-remediation"),
            ("error", "upload_remediation", "observe failed", "asyncio"),
            ("sleep", upload_remediation.POLL_SECONDS),
            ("sweep", (storage.db,), {}, "upload-remediation"),
            ("observe", (storage.db,), {}, "upload-remediation"),
            # A clean pass closes nothing: this worker never notes itself healthy.
            ("sleep", upload_remediation.POLL_SECONDS),
        ])

    async def test_a_failed_step_skips_the_rest_of_the_pass(self) -> None:
        self.step(upload_remediation, "sweep", fail_first=True)
        self.step(upload_remediation, "observe")
        self.assertEqual((await self.two_passes(storage.upload_remediation_loop))[:3], [
            ("sweep", (storage.db,), {}, "upload-remediation"),
            ("error", "upload_remediation", "sweep failed", "asyncio"),
            ("sleep", upload_remediation.POLL_SECONDS),
        ])

    async def test_history_retention(self) -> None:
        self.step(history_retention, "collect", fail_first=True)
        self.assertEqual(await self.two_passes(storage.history_retention_loop), [
            ("collect", (storage.db,), {}, "history-retention"),
            ("error", "history_retention", "collect failed", "asyncio"),
            ("sleep", history_retention.POLL_SECONDS),
            ("collect", (storage.db,), {}, "history-retention"),
            ("ok", "history_retention", "history-retention"),
            ("sleep", history_retention.POLL_SECONDS),
        ])

    async def test_bulk_upload_admission(self) -> None:
        self.step(bulk_upload_admission, "tick", fail_first=True)
        self.assertEqual(await self.two_passes(storage.bulk_upload_admission_loop), [
            ("tick", (storage.db,), {}, "bulk-upload-admission"),
            ("error", "bulk_upload_admission", "tick failed", "asyncio"),
            ("sleep", bulk_upload_admission.POLL_SECONDS),
            ("tick", (storage.db,), {}, "bulk-upload-admission"),
            ("ok", "bulk_upload_admission", "bulk-upload-admission"),
            ("sleep", bulk_upload_admission.POLL_SECONDS),
        ])

    async def test_auto_upload(self) -> None:
        self.step(storage, "queue_all_auto_uploads_once", fail_first=True)
        self.assertEqual(await self.two_passes(storage.auto_upload_loop), [
            ("queue_all_auto_uploads_once", (), {}, "asyncio"),
            ("error", "auto_upload_queue", "queue_all_auto_uploads_once failed", "asyncio"),
            ("sleep", storage.AUTO_UPLOAD_POLL_SECONDS),
            ("queue_all_auto_uploads_once", (), {}, "asyncio"),
            # Each Vault notes itself healthy inside the pass; the loop does not.
            ("sleep", storage.AUTO_UPLOAD_POLL_SECONDS),
        ])

    async def test_the_interval_is_read_at_every_pass(self) -> None:
        self.step(history_retention, "collect")
        with patch.object(history_retention, "POLL_SECONDS", 7):
            events = await self.two_passes(storage.history_retention_loop)
        self.assertEqual([event for event in events if event[0] == "sleep"], [("sleep", 7), ("sleep", 7)])


class WorkerStopTests(unittest.IsolatedAsyncioTestCase):
    """Cancelling a worker whose pass is running on its own thread."""

    async def stop_during_a_pass(self, worker, owner, name: str, prefix: str) -> tuple[bool, bool]:
        """Whether the worker returned before its pass ended, and whether it left a thread."""
        started, release = threading.Event(), threading.Event()

        def run(*_args):
            started.set()
            release.wait(10)

        with patch.object(owner, name, side_effect=run), \
                patch.object(storage, "_note_worker_ok"), \
                patch.object(storage, "_report_worker_error"):
            task = asyncio.create_task(worker())
            self.addCleanup(release.set)
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            self.assertTrue(any(thread.name.startswith(prefix) for thread in threading.enumerate()))
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=0.3)
            returned_before = bool(done)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        for thread in threading.enumerate():
            if thread.name.startswith(prefix):
                await asyncio.to_thread(thread.join, 5)
        return returned_before, any(thread.name.startswith(prefix) for thread in threading.enumerate())

    async def test_history_retention_does_not_wait_for_the_running_pass(self) -> None:
        self.assertEqual(
            await self.stop_during_a_pass(storage.history_retention_loop, history_retention, "collect",
                                          "history-retention"),
            (True, False))

    async def test_bulk_upload_admission_waits_for_the_running_pass(self) -> None:
        # A committing batch must not outlive shutdown and the pool's closure.
        self.assertEqual(
            await self.stop_during_a_pass(storage.bulk_upload_admission_loop, bulk_upload_admission, "tick",
                                          "bulk-upload-admission"),
            (False, False))


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    """``periodic_workers`` alone, with a recording health."""

    def setUp(self) -> None:
        self.calls: list[tuple] = []
        self.health = SimpleNamespace(
            report_error=lambda worker, exc: self.calls.append(("error", worker, str(exc))),
            note_ok=lambda worker: self.calls.append(("ok", worker)))

    def worker(self, *steps, **differences) -> periodic_workers.PeriodicWorker:
        return periodic_workers.PeriodicWorker("probe", steps, lambda: 0, **differences)

    def fail(self) -> None:
        raise RuntimeError("failed")

    def test_one_pass_records_its_outcome_once(self) -> None:
        periodic_workers.run_once(self.worker(lambda: None), self.health)
        periodic_workers.run_once(self.worker(self.fail, lambda: self.calls.append(("ran",))), self.health)
        periodic_workers.run_once(self.worker(lambda: None, notes_ok=False), self.health)
        self.assertEqual(self.calls, [("ok", "probe"), ("error", "probe", "failed")])

    def test_a_pass_that_did_not_run_leaves_the_health_as_it_was(self) -> None:
        skipped = self.worker(lambda: periodic_workers.NOT_RUN, lambda: self.calls.append(("ran",)))
        periodic_workers.run_once(skipped, self.health)
        self.assertEqual(self.calls, [])

    def test_a_worker_with_its_own_health_is_only_run(self) -> None:
        periodic_workers.run_once(self.worker(lambda: None, own_health=True), self.health)
        with self.assertRaisesRegex(RuntimeError, "failed"):
            periodic_workers.run_once(self.worker(self.fail, own_health=True), self.health)
        self.assertEqual(self.calls, [])

    async def test_a_paced_pass_that_did_not_run_notes_nothing(self) -> None:
        skipped = self.worker(lambda: periodic_workers.NOT_RUN, lambda: self.calls.append(("ran",)))
        with patch.object(asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await periodic_workers.run_forever(skipped, self.health)
        self.assertEqual(self.calls, [])


SCHEDULED = ("vault_deletion", "retained_versions", "duplicate_cleanup", "mirror_cleanup",
             "system_bucket", "vault_buckets", "bucket_metrics", "billed_cost")


class SchedulerCase(unittest.IsolatedAsyncioTestCase):
    """The real scheduler on a simulated clock; every piece of work is recorded, not run."""

    runtime = dict(
        operation_concurrency=4, queue_poll_interval=2, scan_interval=3600,
        audit_interval=7200, metadata_backup_interval_seconds=86400,
        metadata_backup_verify_interval_seconds=43200)

    def setUp(self) -> None:
        self.now = 1000.0
        self.clock = SimpleNamespace(time=lambda: self.now)
        self.events: list[tuple] = []
        self.cycles: list[list[tuple]] = []
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(override_settings(**self.runtime))
        self.depth = 0
        for owner, name, result in (
            (storage.health_service, "mark_worker_heartbeat", None),
            (storage.source_layout, "verify_mounts_once", None),
            (storage.vault_relocation, "upgrade_pending_legacy_root_identities", None),
            (storage, "process_jobs_once", None),
            (storage, "_deliver_notifications_once", None),
            (storage, "_submit_directory_aggregates", None),
            (storage.protection_stop_service, "reconcile_all", None),
            (storage, "observe_queue_stall", None),
            (storage, "observe_vault_stalls", None),
            (storage, "observe_stuck_lease_jobs", (3, 4.0)),
            (storage, "observe_failed_job_backlog", (5, 6.0)),
            (storage, "audit_retry_due", False),
            (storage.metadata_backup_service, "mark_verification_attempt", None),
            (storage, "_note_audit_outcome", None),
        ):
            self.record(owner, name, result)
        self.stack.enter_context(patch.object(
            storage, "claimable_queue_depth",
            side_effect=lambda **kwargs: self.events.append(("claimable_queue_depth", (), kwargs)) or self.depth))
        self.stack.enter_context(patch.object(
            storage.metrics_service, "set_gauge",
            side_effect=lambda name, value: self.events.append(("gauge", name, value))))
        self.submit = self.stack.enter_context(patch.object(
            storage, "_submit_maintenance",
            side_effect=lambda name, func: self.events.append(("submit", name))))
        self.stack.enter_context(patch.object(
            storage, "_record_background_loop_error",
            side_effect=lambda exc: self.events.append(("loop_error", str(exc), thread_kind()))))
        self.stack.enter_context(patch.object(
            storage, "_note_worker_ok",
            side_effect=lambda *args: self.events.append(("ok", *args, thread_kind()))))
        self.ages = self.stack.enter_context(patch.object(storage, "persisted_maintenance_ages", return_value={}))

    def record(self, owner, name: str, result) -> None:
        self.stack.enter_context(patch.object(
            owner, name,
            side_effect=lambda *args, **kwargs: self.events.append((name, args, kwargs)) or result))

    async def run_cycles(self, count: int, *, advance: float = 0.0) -> list[list[tuple]]:
        async def refill(wakeup, delay):
            self.events.append(("refill", delay))
            self.cycles.append(self.events[:])
            self.events.clear()
            self.now += advance
            if len(self.cycles) >= count:
                raise asyncio.CancelledError

        with patch.object(storage, "_refill_until_deadline", side_effect=refill):
            with self.assertRaises(asyncio.CancelledError):
                await storage._background_loop(self.clock, asyncio.Event())
        return self.cycles

    def submitted(self) -> list[list[str]]:
        return [[event[1] for event in cycle if event[0] == "submit"] for cycle in self.cycles]


class SchedulerCycleTests(SchedulerCase):
    async def test_the_first_cycle_runs_everything_in_this_order(self) -> None:
        first, second = await self.run_cycles(2)
        self.assertEqual(first, [
            ("_note_audit_outcome", (False,), {}),
            ("mark_worker_heartbeat", (), {}),
            ("gauge", "worker_up", 1),
            ("verify_mounts_once", (), {}),
            ("upgrade_pending_legacy_root_identities", (), {}),
            ("process_jobs_once", (), {"wait": False}),
            ("_deliver_notifications_once", (), {}),
            ("_submit_directory_aggregates", (), {}),
            ("reconcile_all", (), {}),
            *(("submit", name) for name in SCHEDULED),
            ("claimable_queue_depth", (), {}),
            ("gauge", "queue_depth", 0.0),
            ("claimable_queue_depth", (), {"exclude_deferred": True}),
            ("observe_queue_stall", (0,), {}),
            ("observe_vault_stalls", (), {}),
            ("observe_stuck_lease_jobs", (), {}),
            ("gauge", "jobs_stuck", 3.0),
            ("gauge", "jobs_stuck_oldest_age_seconds", 4.0),
            ("observe_failed_job_backlog", (), {}),
            ("gauge", "jobs_failed_backlog", 5.0),
            ("gauge", "jobs_failed_oldest_age_seconds", 6.0),
            ("submit", "scan"),
            ("submit", "audit"),
            ("submit", "backup"),
            ("submit", "backup_verify"),
            ("mark_verification_attempt", (), {}),
            ("_deliver_notifications_once", (), {}),
            ("refill", 2.0),
        ])
        # Nothing is due again on a clock that did not move.
        self.assertEqual(second, [
            ("mark_worker_heartbeat", (), {}),
            ("gauge", "worker_up", 1),
            ("verify_mounts_once", (), {}),
            ("upgrade_pending_legacy_root_identities", (), {}),
            ("process_jobs_once", (), {"wait": False}),
            ("_deliver_notifications_once", (), {}),
            ("reconcile_all", (), {}),
            ("claimable_queue_depth", (), {}),
            ("gauge", "queue_depth", 0.0),
            ("claimable_queue_depth", (), {"exclude_deferred": True}),
            ("observe_queue_stall", (0,), {}),
            ("observe_vault_stalls", (), {}),
            ("observe_stuck_lease_jobs", (), {}),
            ("gauge", "jobs_stuck", 3.0),
            ("gauge", "jobs_stuck_oldest_age_seconds", 4.0),
            ("observe_failed_job_backlog", (), {}),
            ("gauge", "jobs_failed_backlog", 5.0),
            ("gauge", "jobs_failed_oldest_age_seconds", 6.0),
            ("audit_retry_due", (), {}),
            ("_deliver_notifications_once", (), {}),
            ("refill", 2.0),
        ])

    async def test_each_worker_is_submitted_at_its_own_interval(self) -> None:
        # One cycle every 15 simulated seconds, for a little over two hours.
        await self.run_cycles(8 * 60 + 2, advance=15.0)
        due: dict[str, list[float]] = {}
        aggregates = []
        for index, cycle in enumerate(self.cycles):
            for event in cycle:
                if event[0] == "submit":
                    due.setdefault(event[1], []).append(index * 15.0)
                elif event[0] == "_submit_directory_aggregates":
                    aggregates.append(index * 15.0)
        horizon = (len(self.cycles) - 1) * 15.0

        def every(seconds: float) -> list[float]:
            return [float(at) for at in range(0, int(horizon) + 1, int(seconds))]

        self.assertEqual(due, {
            "vault_deletion": every(30),
            "retained_versions": every(3600),
            "duplicate_cleanup": every(300),
            "mirror_cleanup": every(300),
            "system_bucket": every(900),
            "vault_buckets": every(60),
            "bucket_metrics": every(300),
            "billed_cost": every(900),
            "scan": every(3600),
            "audit": every(7200),
            "backup": every(86400),
            "backup_verify": every(43200),
        })
        self.assertEqual(aggregates, every(15))

    async def test_persisted_ages_decide_the_first_run_of_scan_audit_and_backups(self) -> None:
        self.ages.return_value = {"scan": 3590.0, "audit": 0.0, "backup": 86400.0, "backup_verify": None}
        await self.run_cycles(3, advance=10.0)
        timed = [[name for name in cycle if name not in SCHEDULED] for cycle in self.submitted()]
        self.assertEqual(timed, [["backup", "backup_verify"], ["scan"], []])

    async def test_only_the_startup_scan_leaves_watched_vaults_to_their_watchers(self) -> None:
        # #862: the first scan of a loop whose scan age is unknown is the
        # startup scan; every later one is the periodic scan of every Vault.
        scans: list[dict] = []
        self.stack.enter_context(patch.object(
            storage, "scan_all_vaults", side_effect=lambda **options: scans.append(options)))
        self.submit.side_effect = lambda name, func: (
            self.events.append(("submit", name)) or (func() if name == "scan" else None))
        await self.run_cycles(3, advance=3600.0)
        self.assertEqual(scans, [{"startup": True}, {"startup": False}, {"startup": False}])
        # A scan age the host knows makes the first scan a periodic one.
        scans.clear()
        self.cycles.clear()
        self.ages.return_value = {"scan": 3600.0}
        await self.run_cycles(1)
        self.assertEqual(scans, [{"startup": False}])

    async def test_backups_switched_off_are_never_submitted_and_a_failed_audit_is_retried(self) -> None:
        self.stack.enter_context(override_settings(
            metadata_backup_interval_seconds=0, metadata_backup_verify_interval_seconds=0))
        self.stack.enter_context(patch.object(storage, "audit_retry_due", return_value=True))
        await self.run_cycles(2)
        timed = [[name for name in cycle if name not in SCHEDULED] for cycle in self.submitted()]
        self.assertEqual(timed, [["scan", "audit"], ["audit_retry"]])
        self.assertFalse(any(event[0] == "mark_verification_attempt" for cycle in self.cycles for event in cycle))

    async def test_a_deep_queue_is_polled_fast_and_a_slow_poll_is_capped(self) -> None:
        self.depth = 40
        await self.run_cycles(1)
        self.assertEqual(self.cycles[0][-1], ("refill", 0.1))
        self.cycles.clear()
        with override_settings(queue_poll_interval=60):
            self.depth = 0
            await self.run_cycles(1)
        self.assertEqual(self.cycles[0][-1], ("refill", storage.NOTIFICATION_MAX_LATENCY_SECONDS))

    async def test_a_failing_cycle_is_recorded_and_three_clean_ones_close_it(self) -> None:
        calls = 0

        def dispatch(**_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("dispatch failed")

        self.stack.enter_context(patch.object(storage, "process_jobs_once", side_effect=dispatch))
        await self.run_cycles(6)
        health = [[event for event in cycle if event[0] in {"loop_error", "ok"}] for cycle in self.cycles]
        # The streak of clean cycles restarts at the failure; the third clean
        # cycle closes the loop's own problem, after its wait (so it is seen
        # at the start of the next cycle here).
        self.assertEqual(health, [
            [], [("loop_error", "dispatch failed", "asyncio")], [], [], [],
            [("ok", "background_loop", "asyncio")],
        ])
        # The failed cycle stops where it failed, and still waits its turn.
        self.assertEqual(self.cycles[1], [
            ("mark_worker_heartbeat", (), {}),
            ("gauge", "worker_up", 1),
            ("verify_mounts_once", (), {}),
            ("upgrade_pending_legacy_root_identities", (), {}),
            ("loop_error", "dispatch failed", "asyncio"),
            ("refill", 2.0),
        ])

    async def test_a_failing_refill_is_recorded_too(self) -> None:
        async def refill(wakeup, delay):
            self.cycles.append(self.events[:])
            self.events.clear()
            if len(self.cycles) == 1:
                raise RuntimeError("refill failed")
            raise asyncio.CancelledError

        with patch.object(storage, "_refill_until_deadline", side_effect=refill):
            with self.assertRaises(asyncio.CancelledError):
                await storage._background_loop(self.clock, asyncio.Event())
        self.assertEqual(self.cycles[1][0], ("loop_error", "refill failed", "asyncio"))


class ScheduledWorkerHealthTests(SchedulerCase):
    """What a submitted worker reports, run where the scheduler submitted it."""

    async def run_submitted(self, failing: bool) -> list[tuple]:
        reports: list[tuple] = []
        boom = RuntimeError("worker failed")

        def work(*args, **kwargs):
            reports.append(("run", args, sorted(kwargs)))
            if failing:
                raise boom
            return {"deleted": 2}

        from app.services import system_bucket

        self.submit.side_effect = lambda name, func: reports.append(("submit", name)) or func()
        with ExitStack() as stack:
            for service in (storage.vault_deletion_service, storage.retention_lock_service,
                            storage.duplicate_cleanup_service, storage.mirror_cleanup_service,
                            storage.bucket_metrics_service):
                stack.enter_context(patch.object(service, "run_due", side_effect=work))
            stack.enter_context(patch.object(
                storage.metadata_backup_service, "off_host_backup_configured", return_value=True))
            stack.enter_context(patch.object(system_bucket, "configured_bucket", return_value=None))
            stack.enter_context(patch.object(system_bucket.DatabaseStore, "load", return_value=None))
            stack.enter_context(patch.object(system_bucket, "ensure", side_effect=work))
            for name in ("ensure_vault_buckets_once", "read_billed_cost_once", "scan_all_vaults",
                         "audit_all_vaults", "_run_scheduled_metadata_backup_once",
                         "_verify_latest_metadata_backup_once"):
                stack.enter_context(patch.object(storage, name))
            stack.enter_context(patch.object(
                storage, "_note_worker_ok", side_effect=lambda *args: reports.append(("ok", *args))))
            stack.enter_context(patch.object(
                storage, "_report_worker_error",
                side_effect=lambda *args: reports.append(("error", args[0], args[1] is boom, *args[2:]))))
            await self.run_cycles(1)
        return reports

    def expected(self, outcome) -> list[tuple]:
        return [
            ("submit", "vault_deletion"), ("run", (storage.db, storage.s3_client), []),
            outcome("vault_deletion"),
            ("submit", "retained_versions"), ("run", (storage.db, storage.s3_client), []),
            outcome("retained_versions"),
            ("submit", "duplicate_cleanup"), ("run", (storage.db,), []), outcome("duplicate_cleanup"),
            ("submit", "mirror_cleanup"), ("run", (storage.db,), []), outcome("mirror_cleanup"),
            ("submit", "system_bucket"), ("run", (), []), outcome("system_bucket"),
            ("submit", "vault_buckets"),
            ("submit", "bucket_metrics"), ("run", (storage.db,), ["should_stop"]), outcome("bucket_metrics"),
            ("submit", "billed_cost"),
            ("submit", "scan"), ("submit", "audit"), ("submit", "backup"), ("submit", "backup_verify"),
        ]

    async def test_a_successful_run_notes_the_worker_healthy(self) -> None:
        self.assertEqual(await self.run_submitted(False), self.expected(lambda name: ("ok", name)))

    async def test_a_failed_run_reports_the_worker_and_does_not_stop_the_cycle(self) -> None:
        self.assertEqual(await self.run_submitted(True), self.expected(lambda name: ("error", name, True)))
        self.assertFalse(any(event[0] == "loop_error" for event in self.cycles[0]))


if __name__ == "__main__":
    unittest.main()
