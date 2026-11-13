"""#565: the existing periodic scan is independent of native notifications.

Use a simulated clock, temporary source trees and in-memory catalog writes.
The real scheduler, scan_all_vaults dispatcher and pinned local walk run, but
Jobs, S3 and PostgreSQL are not required. This is not a FUSE reproduction.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import storage
from app.services import watch_plan


class PeriodicLostEventRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_events_in_every_vault_are_recovered_at_configured_interval(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            roots = [Path(directory) / str(index) for index in range(2)]
            vaults = []
            for index, root in enumerate(roots):
                root.mkdir()
                vault_id = 9565 + index
                self.addCleanup(watch_plan.forget, vault_id)
                vaults.append({"id": vault_id, "source_root": str(root)})
            connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
            connection.execute.return_value.fetchall.return_value = vaults
            connection.execute.return_value.fetchone.return_value = None
            observed = set()
            catalog = stack.enter_context(patch("app.storage.ArchiveCatalog")).return_value
            catalog.observe_local_copies_batch.side_effect = lambda **kw: observed.update(
                (kw["vault_id"], entry[0]) for entry in kw["entries"]
            )
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(
                local_operations_allowed=True, volume_alias=None, volume_health="healthy",
            )))
            scanned = []

            def scan(vault):
                # Only cloud integration, health publication and persistence
                # are outside this seam; discovery uses the real pinned walk.
                scanned.append(vault["id"])
                storage._scan_tree(
                    connection, vault, "periodic", root=Path(vault["source_root"]),
                    allow_chunk_commits=True, finalize_missing=False,
                )

            stack.enter_context(patch("app.storage.scan_vault", side_effect=scan))
            runtime = SimpleNamespace(
                operation_concurrency=1, queue_poll_interval=2, scan_interval=37,
                audit_interval=10**9, metadata_backup_interval_seconds=0,
                metadata_backup_verify_interval_seconds=0,
            )
            now = 1000.0
            clock = SimpleNamespace(time=lambda: now)
            cycles = []
            expected = {(vault["id"], "new/b.bin") for vault in vaults}

            async def refill(wakeup, delay):
                nonlocal now
                cycles.append((list(scanned), set(observed)))
                if len(cycles) == 1:
                    for root in roots:
                        (root / "new").mkdir()
                        (root / "new" / "b.bin").write_bytes(b"no native event delivered")
                    # No watcher is running and no event is submitted anywhere.
                    now += runtime.scan_interval - 0.01
                elif len(cycles) == 2:
                    now += 0.02
                else:
                    # Always stop, even on regression: assertions inside the
                    # worker's exception handlers would otherwise be swallowed.
                    raise asyncio.CancelledError

            for owner, name in (
                (storage.source_layout, "verify_mounts_once"),
                (storage, "process_jobs_once"), (storage, "_deliver_notifications_once"),
                (storage, "process_directory_aggregate_maintenance"),
                (storage.protection_stop_service, "reconcile_all"),
                (storage, "observe_queue_stall"),
                (storage.health_service, "mark_worker_heartbeat"),
            ):
                stack.enter_context(patch.object(owner, name))
            stack.enter_context(patch.object(storage, "persisted_maintenance_ages", return_value={"audit": 0}))
            stack.enter_context(patch.object(storage, "_runtime_settings", return_value=runtime))
            stack.enter_context(patch.object(storage, "claimable_queue_depth", return_value=0))
            stack.enter_context(patch.object(storage, "observe_stuck_lease_jobs", return_value=(0, 0.0)))
            stack.enter_context(patch.object(storage, "observe_failed_job_backlog", return_value=(0, 0.0)))
            stack.enter_context(patch.object(storage, "_refill_until_deadline", side_effect=refill))
            # Run the submitted scan synchronously to keep the simulated-clock
            # verdict deterministic. Pool serialization has separate coverage.
            submit = stack.enter_context(patch.object(storage, "_submit_maintenance", side_effect=lambda name, func: func()))
            errors = stack.enter_context(patch.object(storage, "_record_background_loop_error"))
            with self.assertRaises(asyncio.CancelledError):
                await storage._background_loop(clock, asyncio.Event())
            errors.assert_not_called()
            self.assertEqual(cycles[0][0], [v["id"] for v in vaults])
            self.assertEqual(cycles[1][0], cycles[0][0], "must not rescan before the interval")
            self.assertTrue(expected.isdisjoint(cycles[1][1]))
            self.assertTrue(expected <= cycles[2][1], "missed files must not need a restart")
            self.assertEqual(cycles[2][0], [v["id"] for v in vaults] * 2)
            self.assertEqual([call.args[0] for call in submit.call_args_list], ["scan", "scan"])
            queries = [call.args[0] for call in connection.execute.call_args_list]
            self.assertTrue(any("v.lifecycle='active'" in sql for sql in queries))


if __name__ == "__main__":
    unittest.main()
