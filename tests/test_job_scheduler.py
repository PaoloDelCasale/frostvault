"""Fair Job scheduling and cancellation (issue #12).

Seams under test:
- ``app.services.job_scheduler.select_fair_jobs`` — deterministic fair
  interleave across Vaults so one Vault cannot starve others.
- ``app.services.job_scheduler.job_is_within_operating_window`` — defer Jobs
  outside configured windows without cancelling them.
- Cancellation remains observable through Job status via the existing
  ``cancel_jobs`` seam (covered in an integration slice below).
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.services.job_scheduler import (
    job_is_within_operating_window,
    select_fair_jobs,
)
from app.storage import cancel_jobs, process_jobs_once
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


class FairSchedulerIntegrationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
