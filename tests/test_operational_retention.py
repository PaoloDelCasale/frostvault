"""DB-independent clocks, replay boundaries and bounded cursor orchestration."""
import asyncio
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import psycopg

from app.config import settings
from app.services import operational_retention as retention

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)


class _Connection:
    def __init__(self, *, last=0, through=0, maximum=9, ids=(1, 2), commit_error=None):
        self.last, self.through, self.maximum, self.ids = last, through, maximum, ids
        self.commit_error = commit_error
        self.calls = []
        self.closed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        self.rolled_back = exc_type is not None or self.commit_error is not None
        if self.commit_error:
            raise self.commit_error

    def close(self):
        self.closed = True

    def execute(self, statement, params=None):
        self.calls.append((statement, params))
        if statement.startswith("SELECT last_id"):
            row, rows = {"last_id": self.last, "through_id": self.through}, []
        elif statement.startswith("SELECT COALESCE(max"):
            row, rows = {"id": self.maximum}, []
        else:
            row, rows = None, [{"id": i} for i in self.ids]
        return SimpleNamespace(fetchone=lambda: row, fetchall=lambda: rows, rowcount=len(self.ids))


class OperationalRetentionTests(unittest.TestCase):
    def test_fake_clock_uses_utc_and_independent_30_90_day_cutoffs(self):
        local_now = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
        self.assertEqual(retention.cutoffs(local_now, 30, 90), {
            "operational_cutoff": NOW - timedelta(days=30),
            "audit_cutoff": NOW - timedelta(days=90),
        })
        self.assertEqual(retention.cutoffs(NOW, 7, 365)["audit_cutoff"], NOW - timedelta(days=365))

    def test_naive_clock_and_nonpositive_retention_fail_before_checkout(self):
        for clock, days in ((NOW.replace(tzinfo=None), 30), (NOW, 0), (NOW, -1)):
            with self.assertRaises(ValueError):
                retention.maintain_operational_history(lambda: self.fail("DB touched"), now=clock, operational_days=days)

    def test_defaults_are_operational_30_audit_90_and_bounded(self):
        self.assertEqual(settings.operational_history_retention_days, 30)
        self.assertEqual(settings.audit_retention_days, 90)
        self.assertLessEqual(settings.operational_retention_batch_size, retention.MAX_BATCH_SIZE)

    def test_one_page_per_fixed_plan_and_batch_cap(self):
        with patch.object(retention, "_prune_page", return_value={"examined": 0, "deleted": 0}) as page:
            result = retention.maintain_operational_history(lambda: None, now=NOW, batch_size=10**9)
        self.assertEqual(list(result), ["deliveries", "notifications", "audit", "jobs"])
        self.assertEqual(page.call_count, 4)
        self.assertTrue(all(call.args[-1] == 1000 for call in page.call_args_list))

    def test_highwater_does_not_follow_concurrent_creation(self):
        connection = _Connection(last=5, through=10, maximum=100, ids=(6, 9))
        retention._prune_page(lambda: connection, retention._PLANS[-1], retention.cutoffs(NOW, 30, 90), 2)
        self.assertFalse(any("max(id)" in sql for sql, _ in connection.calls))
        page_params = next(p for s, p in connection.calls if s.startswith("SELECT id"))
        self.assertEqual((page_params["after"], page_params["through"], page_params["limit"]), (5, 10, 2))
        self.assertEqual(connection.calls[-1][1], (9, 10, "jobs"))
        self.assertTrue(connection.closed)

    def test_empty_or_final_page_wraps_for_late_commits_and_newly_closed_rows(self):
        for ids in ((), (9,)):
            connection = _Connection(through=9, ids=ids)
            retention._prune_page(lambda: connection, retention._PLANS[2], retention.cutoffs(NOW, 30, 90), 2)
            self.assertEqual(connection.calls[-1][1], (0, 0, "audit"))

    def test_commit_deadlock_replays_whole_db_unit_with_identical_cutoffs(self):
        first = _Connection(commit_error=psycopg.errors.DeadlockDetected("synthetic"))
        second = _Connection()
        handles = iter((first, second))
        with patch("app.services.database_retry.time.sleep"):
            result = retention._prune_page(lambda: next(handles), retention._PLANS[2], retention.cutoffs(NOW, 30, 90), 2)
        self.assertTrue(first.closed and first.rolled_back and second.closed)
        self.assertEqual(first.calls, second.calls)
        self.assertEqual(result, {"examined": 2, "deleted": 2})

    def test_nonretryable_timeout_leaves_cursor_to_next_tick(self):
        connection = _Connection(commit_error=psycopg.errors.QueryCanceled("synthetic"))
        with self.assertRaises(psycopg.errors.QueryCanceled):
            retention._prune_page(lambda: connection, retention._PLANS[2], retention.cutoffs(NOW, 30, 90), 2)
        self.assertTrue(connection.closed and connection.rolled_back)

    def test_delivery_final_state_serializes_with_notification_reopen(self):
        connection = _Connection()
        retention._prune_page(lambda: connection, retention._PLANS[0], retention.cutoffs(NOW, 30, 90), 2)
        parent_lock = next(i for i, (s, _) in enumerate(connection.calls) if s.startswith("SELECT n.id"))
        deletion = next(i for i, (s, _) in enumerate(connection.calls) if s.startswith("DELETE"))
        self.assertLess(parent_lock, deletion)
        self.assertIn("FOR UPDATE", connection.calls[parent_lock][0])

    def test_scheduler_uses_periodic_maintenance_without_blocking_claim_path(self):
        from app import storage
        async def exercise(interval):
            clock = SimpleNamespace(current=1000.0)
            clock.time = lambda: clock.current
            runtime = replace(settings, operational_retention_interval_seconds=interval,
                scan_interval=10**9, audit_interval=10**9,
                metadata_backup_interval_seconds=0, metadata_backup_verify_interval_seconds=0)
            cycles = 0
            async def refill(*_):
                nonlocal cycles
                cycles += 1
                if cycles == 3:
                    raise asyncio.CancelledError
                clock.current += 59 if cycles == 1 else 1
            with ExitStack() as stack:
                for owner, name in (
                    (storage.source_layout,"verify_mounts_once"),
                    (storage,"process_jobs_once"), (storage,"_deliver_notifications_once"),
                    (storage,"process_directory_aggregate_maintenance"),
                    (storage.protection_stop_service,"reconcile_all"),
                    (storage,"observe_queue_stall"),
                    (storage.health_service,"mark_worker_heartbeat"),
                ):
                    stack.enter_context(patch.object(owner,name))
                stack.enter_context(patch.object(storage,"persisted_maintenance_ages",return_value={"scan":0,"audit":0}))
                stack.enter_context(patch.object(storage,"_runtime_settings",return_value=runtime))
                stack.enter_context(patch.object(storage,"claimable_queue_depth",return_value=0))
                stack.enter_context(patch.object(storage,"observe_stuck_lease_jobs",return_value=(0,0)))
                stack.enter_context(patch.object(storage,"observe_failed_job_backlog",return_value=(0,0)))
                stack.enter_context(patch.object(storage,"_refill_until_deadline",side_effect=refill))
                submit = stack.enter_context(patch.object(storage,"_submit_maintenance"))
                errors = stack.enter_context(patch.object(storage,"_record_background_loop_error"))
                with self.assertRaises(asyncio.CancelledError):
                    await storage._background_loop(clock,asyncio.Event())
                errors.assert_not_called()
                return [c.args for c in submit.call_args_list]
        calls = asyncio.run(exercise(60))
        self.assertEqual([name for name, _ in calls], ["operational_retention"]*2)
        self.assertTrue(all(func is storage._run_operational_retention_once for _,func in calls))
        self.assertEqual(asyncio.run(exercise(0)), [])

    def test_scheduler_hook_passes_only_config_and_database_seam(self):
        from app import storage
        runtime = replace(settings, operational_history_retention_days=45,
            audit_retention_days=180, operational_retention_batch_size=7)
        with patch.object(storage,"_runtime_settings",return_value=runtime), \
             patch.object(retention,"maintain_operational_history") as maintain:
            storage._run_operational_retention_once()
        maintain.assert_called_once_with(storage.db,operational_days=45,audit_days=180,batch_size=7)

    def test_sql_surface_never_deletes_evidence_or_introspects_schema(self):
        sql = "\n".join(p.delete for p in retention._PLANS)
        self.assertEqual([p.delete.split()[2] for p in retention._PLANS],
                         ["notification_deliveries", "notifications", "audit_events", "jobs"])
        self.assertNotIn("CASCADE", sql)
        self.assertNotIn("information_schema", sql)
        self.assertIn("j.upload_snapshot_json IS NULL", sql)
        self.assertIn("upload_intents i WHERE i.original_job_id=j.id", sql)
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("GREATEST", sql)
        self.assertIn("n.resolved_at IS NOT NULL", sql)


if __name__ == "__main__":
    unittest.main()
