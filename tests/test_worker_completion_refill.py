"""A finished operation wakes fair dispatch without waiting for the poll timer."""
from __future__ import annotations

import asyncio
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from app import storage
from tests import test_worker_loop_decoupling as worker_tests
from tests.postgres import PostgresConnection


class WorkerCompletionRefillTests(unittest.TestCase):
    def setUp(self):
        self.worker = worker_tests.WorkerLoopDecouplingTests(methodName="runTest")
        self.addCleanup(self.worker.doCleanups)
        self.worker.setUp()

    def test_completion_refills_before_poll_deadline(self):
        asyncio.run(self._exercise(pause=False))

    def test_completion_wakeup_cannot_claim_through_a_pause(self):
        asyncio.run(self._exercise(pause=True))

    async def _exercise(self, *, pause):
        first = self.worker._insert_job("slow.txt", "2026-07-01T00:00:00+00:00")
        second = self.worker._insert_job("fast.txt", "2026-07-01T00:00:01+00:00")
        started, release, completed, second_started = (threading.Event() for _ in range(4))
        runtime = SimpleNamespace(
            operation_concurrency=1, bandwidth_limit_kibps=None,
            restore_poll_interval=0, queue_poll_interval=60,
            scan_interval=3600, audit_interval=3600,
            metadata_backup_interval_seconds=0,
            metadata_backup_verify_interval_seconds=0,
        )

        def handler(job):
            # Replace only the transfer handler, not scheduler/admission SQL.
            if job["id"] == first:
                started.set()
                if not release.wait(10):
                    raise AssertionError("test did not release its transfer")
                if pause:
                    with PostgresConnection(self.worker.path) as connection:
                        connection.execute("UPDATE vaults SET paused=TRUE WHERE id=10")
            else:
                second_started.set()
            with storage._job_claim_context(job):
                storage.set_job(job["id"], "completed", "test transfer finished")
            if job["id"] == first:
                completed.set()
            return True

        _, database_patch, handler_patch = self.worker._worker_patches(handler)
        real_sleep = asyncio.sleep

        async def startup_sleep(_seconds):
            # Only background_loop's startup uses sleep; its poll deadline must
            # stay real. Do not fast-forward the five-second refill interval.
            await real_sleep(0)

        with ExitStack() as stack:
            stack.enter_context(database_patch)
            stack.enter_context(handler_patch)
            stack.enter_context(patch.object(storage, "_runtime_settings", return_value=runtime))
            stack.enter_context(patch.object(storage, "persisted_maintenance_ages", return_value={
                name: 0.0 for name in storage.MAINTENANCE_TASKS
            }))
            for owner, name in (
                (storage.source_layout, "verify_mounts_once"),
                (storage, "_deliver_notifications_once"),
                (storage, "process_directory_aggregate_maintenance"),
                (storage.protection_stop_service, "reconcile_all"),
                (storage, "observe_queue_stall"),
            ):
                stack.enter_context(patch.object(owner, name))
            stack.enter_context(patch.object(storage, "observe_stuck_lease_jobs", return_value=(0, 0)))
            stack.enter_context(patch.object(storage.asyncio, "sleep", side_effect=startup_sleep))
            loop = asyncio.create_task(storage.background_loop())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5), "first Job never dispatched")
                release.set()
                self.assertTrue(await asyncio.to_thread(completed.wait, 5))
                if pause:
                    await real_sleep(0.3)
                    self.assertFalse(second_started.is_set())
                    with PostgresConnection(self.worker.path) as connection:
                        row = connection.execute("SELECT status,claim_token FROM jobs WHERE id=%s", (second,)).fetchone()
                    self.assertEqual(row, {"status": "queued", "claim_token": None})
                else:
                    self.assertTrue(await asyncio.to_thread(second_started.wait, 2),
                                    "idle slot waited for poll instead of completion wakeup")
            finally:
                release.set()
                loop.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await loop
                # The handler is bounded; finish it before fixture DB cleanup.
                with storage._operation_lock:
                    pending = list(storage._operation_futures.values())
                for future in pending:
                    await asyncio.to_thread(future.result, 5)
