"""Fair Job scheduling and cancellation (issue #12).

Seams under test:
- ``app.services.job_scheduler.select_fair_jobs`` — deterministic fair
  interleave across Vaults so one Vault cannot starve others.
- ``app.services.job_scheduler.job_is_within_operating_window`` — defer Jobs
  outside configured windows without cancelling them.
- Cancellation remains observable through Job status via the existing
  ``cancel_jobs`` seam (covered in an integration slice below).
- Expired intermediate leases are swept by ``process_jobs_once`` without a
  second restart; stuck gauges stay distinct from ``queue_depth``.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Windows test hosts lack the Linux open flags imported by vault decommission.
for _flag, _value in (
    ("O_DIRECTORY", 0x10000),
    ("O_NOFOLLOW", 0x20000),
    ("O_CLOEXEC", 0x80000),
):
    if not hasattr(os, _flag):
        setattr(os, _flag, _value)

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.services.job_scheduler import (
    job_is_within_operating_window,
    select_fair_jobs,
)
from app import storage as storage_module
from app.storage import (
    cancel_jobs,
    claimable_queue_depth,
    observe_stuck_lease_jobs,
    process_job,
    process_jobs_once,
    reconcile_interrupted_jobs,
)
from app.services import metrics as metrics_service
from tests.test_database import run_alembic


class SelectFairJobsTests(unittest.TestCase):
    def test_interleaves_oldest_jobs_across_vaults(self) -> None:
        jobs = [
            {"id": 1, "vault_id": 10, "requested_at": "2026-07-01T00:00:00+00:00"},
            {"id": 2, "vault_id": 10, "requested_at": "2026-07-01T00:01:00+00:00"},
            {"id": 3, "vault_id": 10, "requested_at": "2026-07-01T00:02:00+00:00"},
            {"id": 4, "vault_id": 20, "requested_at": "2026-07-01T00:03:00+00:00"},
            {"id": 5, "vault_id": 20, "requested_at": "2026-07-01T00:04:00+00:00"},
            {"id": 6, "vault_id": 30, "requested_at": "2026-07-01T00:05:00+00:00"},
        ]
        selected = select_fair_jobs(jobs, limit=3)
        self.assertEqual([job["id"] for job in selected], [1, 4, 6])

    def test_continues_round_robin_after_first_pass(self) -> None:
        jobs = [
            {"id": 1, "vault_id": 10, "requested_at": "2026-07-01T00:00:00+00:00"},
            {"id": 2, "vault_id": 10, "requested_at": "2026-07-01T00:01:00+00:00"},
            {"id": 3, "vault_id": 20, "requested_at": "2026-07-01T00:02:00+00:00"},
        ]
        selected = select_fair_jobs(jobs, limit=3)
        self.assertEqual([job["id"] for job in selected], [1, 3, 2])

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(select_fair_jobs([], limit=5), [])


class OperatingWindowTests(unittest.TestCase):
    def test_empty_windows_always_allow(self) -> None:
        now = datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc)
        self.assertTrue(job_is_within_operating_window(now, ()))

    def test_job_outside_window_is_deferred(self) -> None:
        now = datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc)
        windows = (
            {"weekday": 2, "start": "09:00", "end": "17:00"},  # Wednesday
        )
        self.assertFalse(job_is_within_operating_window(now, windows))

    def test_job_inside_window_is_allowed(self) -> None:
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)  # Wednesday
        windows = (
            {"weekday": 2, "start": "09:00", "end": "17:00"},
        )
        self.assertTrue(job_is_within_operating_window(now, windows))


class _SchedulerDbMixin:
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "scheduler.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'owner', 'Owner', 'hash', 0)"
            )
            for vault_id, slug in ((10, "alpha"), (20, "beta")):
                connection.execute(
                    "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                    "VALUES (%s, %s, %s, %s, 'bucket', %s, 'remote')",
                    (vault_id, slug, slug, f"/source/{slug}", slug),
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) VALUES (%s, 1, 'owner')",
                    (vault_id,),
                )
                catalog = ArchiveCatalog(connection)
                for name in ("a.txt", "b.txt"):
                    catalog.observe_local_copy(
                        vault_id=vault_id,
                        path=name,
                        file_type="regular",
                        size=1,
                        mtime_ns=1,
                        observed_at="2026-07-01T00:00:00+00:00",
                    )

    def _insert_upload_job(
        self,
        *,
        vault_id: int,
        path: str,
        requested_at: str = "2026-07-01T00:00:00+00:00",
    ) -> int:
        with SQLiteConnection(str(self.path)) as connection:
            return int(
                connection.execute(
                    """
                    INSERT INTO jobs(
                        vault_id, vault_file_id, path, action, status,
                        requested_by, requested_at, updated_at
                    )
                    SELECT %s, id, %s, 'upload', 'queued', 1, %s, %s
                    FROM vault_files
                    WHERE vault_id=%s
                      AND id=(
                          SELECT id FROM vault_files
                          WHERE vault_id=%s
                          ORDER BY id
                          LIMIT 1 OFFSET %s
                      )
                    RETURNING id
                    """,
                    (
                        vault_id,
                        path,
                        requested_at,
                        requested_at,
                        vault_id,
                        vault_id,
                        0 if path == "a.txt" else 1,
                    ),
                ).fetchone()["id"]
            )


class FairSchedulerIntegrationTests(_SchedulerDbMixin, unittest.TestCase):
    def test_process_jobs_once_does_not_starve_second_vault(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            # Three older Jobs in vault 10, one newer Job in vault 20.
            connection.execute(
                "INSERT INTO jobs(vault_id, vault_file_id, path, action, status, requested_by, requested_at, updated_at) "
                "SELECT 10, id, 'a.txt', 'upload', 'queued', 1, '2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00' "
                "FROM vault_files WHERE vault_id=10 ORDER BY id LIMIT 1"
            )
            connection.execute(
                "INSERT INTO jobs(vault_id, vault_file_id, path, action, status, requested_by, requested_at, updated_at) "
                "SELECT 10, id, 'b.txt', 'upload', 'queued', 1, '2026-07-01T00:01:00+00:00', '2026-07-01T00:01:00+00:00' "
                "FROM vault_files WHERE vault_id=10 ORDER BY id DESC LIMIT 1"
            )
            connection.execute(
                "INSERT INTO jobs(vault_id, vault_file_id, path, action, status, requested_by, requested_at, updated_at) "
                "SELECT 20, id, 'a.txt', 'upload', 'queued', 1, '2026-07-01T00:02:00+00:00', '2026-07-01T00:02:00+00:00' "
                "FROM vault_files WHERE vault_id=20 ORDER BY id LIMIT 1"
            )

        processed_ids: list[int] = []

        def fake_process(job: dict) -> bool:
            processed_ids.append(int(job["id"]))
            return True

        with (
            patch("app.storage.settings") as mock_settings,
            patch("app.storage.db") as mock_db,
            patch("app.storage.process_job", side_effect=fake_process),
        ):
            mock_settings.operation_concurrency = 2
            mock_db.side_effect = lambda: SQLiteConnection(str(self.path))
            process_jobs_once()

        with SQLiteConnection(str(self.path)) as connection:
            by_vault = {
                row["id"]: row["vault_id"]
                for row in connection.execute("SELECT id, vault_id FROM jobs").fetchall()
            }
        vaults_seen = {by_vault[job_id] for job_id in processed_ids}
        self.assertEqual(len(processed_ids), 2)
        self.assertEqual(vaults_seen, {10, 20})

    def test_cancelled_queued_job_is_not_started(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO jobs(vault_id, vault_file_id, path, action, status, requested_by, requested_at, updated_at) "
                "SELECT 10, id, 'a.txt', 'upload', 'queued', 1, '2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00' "
                "FROM vault_files WHERE vault_id=10 ORDER BY id LIMIT 1"
            )
            job_id = connection.execute("SELECT id FROM jobs").fetchone()["id"]

        cancel_jobs([job_id])
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "UPDATE jobs SET status='cancelled', message=%s, updated_at=%s WHERE id=%s",
                ("cancelled by test", "2026-07-01T00:10:00+00:00", job_id),
            )
        processed_ids: list[int] = []

        def fake_process(job: dict) -> bool:
            processed_ids.append(int(job["id"]))
            return True

        with (
            patch("app.storage.settings") as mock_settings,
            patch("app.storage.db") as mock_db,
            patch("app.storage.process_job", side_effect=fake_process),
        ):
            mock_settings.operation_concurrency = 4
            mock_db.side_effect = lambda: SQLiteConnection(str(self.path))
            process_jobs_once()

        self.assertEqual(processed_ids, [])
        with SQLiteConnection(str(self.path)) as connection:
            status = connection.execute(
                "SELECT status FROM jobs WHERE id=%s", (job_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "cancelled")

    def test_two_sqlite_schedulers_claim_one_job_once(self) -> None:
        job_id = self._insert_upload_job(vault_id=10, path="a.txt")
        start = threading.Barrier(2)
        seen: list[int] = []
        seen_lock = threading.Lock()
        runtime = SimpleNamespace(
            operation_concurrency=1,
            bandwidth_limit_kibps=None,
            restore_poll_interval=0,
        )

        def record(job: dict) -> bool:
            with seen_lock:
                seen.append(int(job["id"]))
            return True

        def run_once(_: int) -> int:
            start.wait(timeout=10)
            return process_jobs_once()

        with (
            patch("app.storage._runtime_settings", return_value=runtime),
            patch(
                "app.storage.db",
                side_effect=lambda: SQLiteConnection(str(self.path)),
            ),
            patch("app.storage.process_job", side_effect=record),
        ):
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(run_once, range(2)))

        self.assertEqual(sum(results), 1)
        self.assertEqual(seen, [job_id])
        with SQLiteConnection(str(self.path)) as connection:
            job = connection.execute(
                "SELECT claim_token, claim_expires_at FROM jobs WHERE id=%s",
                (job_id,),
            ).fetchone()
        self.assertTrue(job["claim_token"])
        self.assertTrue(job["claim_expires_at"])

    def test_expired_queued_lease_is_reclaimed(self) -> None:
        job_id = self._insert_upload_job(vault_id=10, path="a.txt")
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                """
                UPDATE jobs
                SET claim_token='dead-worker',
                    claimed_at='2000-01-01T00:00:00+00:00',
                    claim_expires_at='2000-01-01T00:05:00+00:00'
                WHERE id=%s
                """,
                (job_id,),
            )
        runtime = SimpleNamespace(
            operation_concurrency=1,
            bandwidth_limit_kibps=None,
            restore_poll_interval=0,
        )
        seen: list[int] = []
        with (
            patch("app.storage._runtime_settings", return_value=runtime),
            patch(
                "app.storage.db",
                side_effect=lambda: SQLiteConnection(str(self.path)),
            ),
            patch(
                "app.storage.process_job",
                side_effect=lambda job: seen.append(int(job["id"])) or True,
            ),
        ):
            self.assertEqual(process_jobs_once(), 1)
        self.assertEqual(seen, [job_id])
        with SQLiteConnection(str(self.path)) as connection:
            job = connection.execute(
                "SELECT claim_token FROM jobs WHERE id=%s", (job_id,)
            ).fetchone()
        self.assertNotEqual(job["claim_token"], "dead-worker")

    def test_claimed_job_cancellation_cannot_be_overwritten_by_worker(self) -> None:
        job_id = self._insert_upload_job(vault_id=10, path="a.txt")
        timestamp = storage_module.now_iso()
        with SQLiteConnection(str(self.path)) as connection:
            claimed = ArchiveCatalog(connection).claim_job(
                job_id=job_id,
                claim_token="worker-claim",
                claimed_at=timestamp,
                claim_expires_at=(
                    datetime.now(timezone.utc) + timedelta(days=1)
                ).isoformat(),
                now=timestamp,
                restore_due_before=timestamp,
            )
            self.assertIsNotNone(claimed)
            job = connection.execute(
                """
                SELECT j.*, v.source_root
                FROM jobs j JOIN vaults v ON v.id=j.vault_id
                WHERE j.id=%s
                """,
                (job_id,),
            ).fetchone()

        runtime = SimpleNamespace(restore_poll_interval=0)

        def cancel_then_attempt_complete(active: dict) -> None:
            with SQLiteConnection(str(self.path)) as connection:
                connection.execute(
                    "UPDATE jobs SET status='cancelled' WHERE id=%s", (job_id,)
                )
            self.assertFalse(storage_module.set_job(active["id"], "completed"))

        with (
            patch("app.storage._runtime_settings", return_value=runtime),
            patch(
                "app.storage.db",
                side_effect=lambda: SQLiteConnection(str(self.path)),
            ),
            patch(
                "app.storage.source_layout.vault_local_access",
                return_value=SimpleNamespace(local_operations_allowed=True),
            ),
            patch("app.storage.process_upload", side_effect=cancel_then_attempt_complete),
        ):
            self.assertTrue(process_job(dict(job)))

        with SQLiteConnection(str(self.path)) as connection:
            status = connection.execute(
                "SELECT status FROM jobs WHERE id=%s", (job_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "cancelled")

    def test_heartbeat_lease_loss_fences_the_worker_terminal_write(self) -> None:
        job_id = self._insert_upload_job(vault_id=10, path="a.txt")
        timestamp = storage_module.now_iso()
        with SQLiteConnection(str(self.path)) as connection:
            claimed = ArchiveCatalog(connection).claim_job(
                job_id=job_id,
                claim_token="heartbeat-worker",
                claimed_at=timestamp,
                claim_expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                now=timestamp,
                restore_due_before=timestamp,
            )
            self.assertIsNotNone(claimed)
            job = connection.execute(
                """
                SELECT j.*, v.source_root
                FROM jobs j JOIN vaults v ON v.id=j.vault_id
                WHERE j.id=%s
                """,
                (job_id,),
            ).fetchone()

        runtime = SimpleNamespace(restore_poll_interval=0)

        def wait_for_heartbeat_loss(active: dict) -> None:
            time.sleep(0.25)
            storage_module.ensure_job_active(active["id"], "claim lost")

        with (
            patch("app.storage._runtime_settings", return_value=runtime),
            patch(
                "app.storage.db",
                side_effect=lambda: SQLiteConnection(str(self.path)),
            ),
            patch("app.storage.JOB_CLAIM_LEASE_SECONDS", 0.01),
            patch("app.storage._renew_claim_if_owned", return_value=None),
            patch(
                "app.storage.source_layout.vault_local_access",
                return_value=SimpleNamespace(local_operations_allowed=True),
            ),
            patch("app.storage.process_upload", side_effect=wait_for_heartbeat_loss),
        ):
            self.assertFalse(process_job(dict(job)))

        with SQLiteConnection(str(self.path)) as connection:
            status = connection.execute(
                "SELECT status FROM jobs WHERE id=%s", (job_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "queued")

    def test_queue_depth_counts_unleased_backlog_not_concurrency_batch(self) -> None:
        first = self._insert_upload_job(vault_id=10, path="a.txt")
        self._insert_upload_job(vault_id=10, path="b.txt")
        self._insert_upload_job(vault_id=20, path="a.txt")
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                """
                UPDATE jobs
                SET claim_token='live-worker',
                    claimed_at='2099-01-01T00:00:00+00:00',
                    claim_expires_at='2099-01-01T00:05:00+00:00'
                WHERE id=%s
                """,
                (first,),
            )
            connection.execute(
                """
                INSERT INTO jobs(
                    vault_id, vault_file_id, path, action, status,
                    requested_by, requested_at, updated_at,
                    claim_token, claimed_at, claim_expires_at
                )
                SELECT 20, id, 'b.txt', 'recover', 'restoring',
                       1, '2000-01-01T00:00:00+00:00',
                       '2000-01-01T00:00:00+00:00',
                       'dead-restorer', '2000-01-01T00:00:00+00:00',
                       '2000-01-01T00:05:00+00:00'
                FROM vault_files
                WHERE vault_id=20
                ORDER BY id DESC
                LIMIT 1
                """
            )
        runtime = SimpleNamespace(restore_poll_interval=0)
        with (
            patch("app.storage._runtime_settings", return_value=runtime),
            patch(
                "app.storage.db",
                side_effect=lambda: SQLiteConnection(str(self.path)),
            ),
        ):
            self.assertEqual(claimable_queue_depth(), 3)


class ExpiredLeaseSweepTests(_SchedulerDbMixin, unittest.TestCase):
    def _hold_upload_job(
        self,
        job_id: int,
        *,
        status: str = "uploading",
        token: str = "dead-worker",
        expires_at: str,
        claimed_at: str = "2026-07-01T00:00:00+00:00",
    ) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status=%s,
                    claim_token=%s,
                    claimed_at=%s,
                    claim_expires_at=%s,
                    updated_at=%s
                WHERE id=%s
                """,
                (status, token, claimed_at, expires_at, claimed_at, job_id),
            )

    def _with_worker_db(self):
        runtime = SimpleNamespace(
            operation_concurrency=1,
            bandwidth_limit_kibps=None,
            restore_poll_interval=0,
        )
        return (
            patch("app.storage._runtime_settings", return_value=runtime),
            patch(
                "app.storage.db",
                side_effect=lambda: SQLiteConnection(str(self.path)),
            ),
        )

    def test_lease_valid_at_restart_remains_stuck_until_expiry(self) -> None:
        job_id = self._insert_upload_job(vault_id=10, path="a.txt")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self._hold_upload_job(job_id, expires_at=future)
        seen: list[int] = []
        runtime_patch, db_patch = self._with_worker_db()
        with runtime_patch, db_patch:
            summary = reconcile_interrupted_jobs()
            self.assertEqual(summary, {"completed": 0, "requeued": 0, "failed": 0})
            with patch(
                "app.storage.process_job",
                side_effect=lambda job: seen.append(int(job["id"])),
            ):
                self.assertEqual(process_jobs_once(), 0)
            self.assertEqual(claimable_queue_depth(), 0)
            self.assertEqual(observe_stuck_lease_jobs()[0], 0)
        with SQLiteConnection(str(self.path)) as connection:
            job = connection.execute(
                "SELECT status, claim_token FROM jobs WHERE id=%s",
                (job_id,),
            ).fetchone()
        self.assertEqual(job["status"], "uploading")
        self.assertEqual(job["claim_token"], "dead-worker")
        self.assertEqual(seen, [])

    def test_expired_intermediate_lease_is_swept_without_restart(self) -> None:
        job_id = self._insert_upload_job(vault_id=10, path="a.txt")
        self._hold_upload_job(
            job_id,
            expires_at="2000-01-01T00:05:00+00:00",
        )
        seen: list[int] = []
        runtime_patch, db_patch = self._with_worker_db()
        with runtime_patch, db_patch:
            self.assertEqual(claimable_queue_depth(), 0)
            stuck, age = observe_stuck_lease_jobs()
            self.assertEqual(stuck, 1)
            self.assertGreater(age, 0)
            with patch(
                "app.storage.process_job",
                side_effect=lambda job: seen.append(int(job["id"])),
            ):
                self.assertEqual(process_jobs_once(), 1)
            self.assertEqual(seen, [job_id])
            self.assertEqual(observe_stuck_lease_jobs()[0], 0)
        with SQLiteConnection(str(self.path)) as connection:
            job = connection.execute(
                "SELECT status, claim_token FROM jobs WHERE id=%s",
                (job_id,),
            ).fetchone()
        self.assertEqual(job["status"], "queued")
        self.assertNotEqual(job["claim_token"], "dead-worker")

    def test_live_worker_lease_is_not_recovered_when_sibling_expires(self) -> None:
        live_id = self._insert_upload_job(vault_id=10, path="a.txt")
        dead_id = self._insert_upload_job(vault_id=10, path="b.txt")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self._hold_upload_job(
            live_id,
            token="live-worker",
            expires_at=future,
        )
        self._hold_upload_job(
            dead_id,
            token="dead-worker",
            expires_at="2000-01-01T00:05:00+00:00",
        )
        seen: list[int] = []
        runtime_patch, db_patch = self._with_worker_db()
        with runtime_patch, db_patch:
            with patch(
                "app.storage.process_job",
                side_effect=lambda job: seen.append(int(job["id"])),
            ):
                self.assertEqual(process_jobs_once(), 1)
        self.assertEqual(seen, [dead_id])
        with SQLiteConnection(str(self.path)) as connection:
            rows = connection.execute(
                "SELECT id, status, claim_token FROM jobs WHERE id IN (%s, %s)",
                (live_id, dead_id),
            ).fetchall()
        by_id = {int(row["id"]): row for row in rows}
        self.assertEqual(by_id[live_id]["status"], "uploading")
        self.assertEqual(by_id[live_id]["claim_token"], "live-worker")
        self.assertEqual(by_id[dead_id]["status"], "queued")
        self.assertNotEqual(by_id[dead_id]["claim_token"], "dead-worker")

    def test_stuck_metrics_are_distinct_from_claimable_backlog(self) -> None:
        queued_id = self._insert_upload_job(vault_id=10, path="a.txt")
        stuck_id = self._insert_upload_job(vault_id=20, path="a.txt")
        self._hold_upload_job(
            stuck_id,
            expires_at="2000-01-01T00:05:00+00:00",
        )
        runtime_patch, db_patch = self._with_worker_db()
        with runtime_patch, db_patch:
            self.assertEqual(claimable_queue_depth(), 1)
            stuck, age = observe_stuck_lease_jobs()
            self.assertEqual(stuck, 1)
            self.assertGreater(age, 0)
            metrics_service.reset_for_tests()
            metrics_service.set_gauge("queue_depth", float(claimable_queue_depth()))
            count, oldest = observe_stuck_lease_jobs()
            metrics_service.set_gauge("jobs_stuck", float(count))
            metrics_service.set_gauge("jobs_stuck_oldest_age_seconds", oldest)
            rendered = metrics_service.render_prometheus()
        self.assertIn("queue_depth 1", rendered)
        self.assertIn("jobs_stuck 1", rendered)
        self.assertIn("jobs_stuck_oldest_age_seconds", rendered)
        self.assertNotEqual(queued_id, stuck_id)

    def test_sweep_resumes_after_transient_db_error(self) -> None:
        job_id = self._insert_upload_job(vault_id=10, path="a.txt")
        self._hold_upload_job(
            job_id,
            expires_at="2000-01-01T00:05:00+00:00",
        )
        seen: list[int] = []
        runtime_patch, db_patch = self._with_worker_db()
        with runtime_patch, db_patch:
            with patch(
                "app.storage.reconcile_interrupted_jobs",
                side_effect=RuntimeError("database is locked"),
            ):
                with self.assertRaisesRegex(RuntimeError, "database is locked"):
                    process_jobs_once()
            with SQLiteConnection(str(self.path)) as connection:
                held = connection.execute(
                    "SELECT status, claim_token FROM jobs WHERE id=%s",
                    (job_id,),
                ).fetchone()
            self.assertEqual(held["status"], "uploading")
            self.assertEqual(held["claim_token"], "dead-worker")
            with patch(
                "app.storage.process_job",
                side_effect=lambda job: seen.append(int(job["id"])),
            ):
                self.assertEqual(process_jobs_once(), 1)
        self.assertEqual(seen, [job_id])


if __name__ == "__main__":
    unittest.main()
