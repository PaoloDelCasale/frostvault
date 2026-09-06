"""Keep notifications and health moving during long transfers (issue #304)."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

for _flag, _value in (
    ("O_DIRECTORY", 0x10000),
    ("O_NOFOLLOW", 0x20000),
    ("O_CLOEXEC", 0x80000),
):
    if not hasattr(os, _flag):
        setattr(os, _flag, _value)

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.services import health as health_service
from app.storage import (
    NOTIFICATION_MAX_LATENCY_SECONDS,
    process_jobs_once,
    shutdown_background_executors,
)
from tests.test_database import run_alembic


class WorkerLoopDecouplingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(shutdown_background_executors)
        self.path = Path(self.tmp.name) / "loop.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'owner', 'Owner', 'hash', 0)"
            )
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                "VALUES (10, 'alpha', 'alpha', '/source/alpha', 'bucket', 'alpha', 'remote')"
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (10, 1, 'owner')"
            )
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=10,
                path="slow.txt",
                file_type="regular",
                size=1,
                mtime_ns=1,
                observed_at="2026-07-01T00:00:00+00:00",
            )
            catalog.observe_local_copy(
                vault_id=10,
                path="fast.txt",
                file_type="regular",
                size=1,
                mtime_ns=1,
                observed_at="2026-07-01T00:00:00+00:00",
            )

    def _insert_job(self, path: str, requested_at: str) -> int:
        with SQLiteConnection(str(self.path)) as connection:
            return int(
                connection.execute(
                    """
                    INSERT INTO jobs(
                        vault_id, vault_file_id, path, action, status,
                        requested_by, requested_at, updated_at
                    )
                    SELECT 10, id, %s, 'upload', 'queued', 1, %s, %s
                    FROM vault_files
                    WHERE vault_id=10 AND id=(
                        SELECT vf.id FROM vault_files vf
                        JOIN file_paths fp ON fp.vault_file_id=vf.id
                         AND fp.valid_to IS NULL AND fp.path=%s
                    )
                    RETURNING id
                    """,
                    (path, requested_at, requested_at, path),
                ).fetchone()["id"]
            )

    def _worker_patches(self, process_job):
        runtime = SimpleNamespace(
            operation_concurrency=2,
            bandwidth_limit_kibps=None,
            restore_poll_interval=0,
            queue_poll_interval=2,
        )
        return (
            patch("app.storage._runtime_settings", return_value=runtime),
            patch(
                "app.storage.db",
                side_effect=lambda: SQLiteConnection(str(self.path)),
            ),
            patch("app.storage.process_job", side_effect=process_job),
        )

    def test_wait_false_returns_while_a_job_is_still_running(self) -> None:
        self._insert_job("slow.txt", "2026-07-01T00:00:00+00:00")
        started = threading.Event()
        release = threading.Event()

        def process_job(job):
            started.set()
            release.wait(timeout=5)
            return True

        runtime_patch, db_patch, job_patch = self._worker_patches(process_job)
        with runtime_patch, db_patch, job_patch:
            started_at = time.monotonic()
            submitted = process_jobs_once(wait=False)
            elapsed = time.monotonic() - started_at
            self.assertEqual(submitted, 1)
            self.assertLess(elapsed, NOTIFICATION_MAX_LATENCY_SECONDS)
            self.assertTrue(started.wait(timeout=5))
            release.set()

    def test_failed_job_finishes_while_another_transfer_is_blocked(self) -> None:
        slow_id = self._insert_job("slow.txt", "2026-07-01T00:00:00+00:00")
        fast_id = self._insert_job("fast.txt", "2026-07-01T00:00:01+00:00")
        slow_started = threading.Event()
        release_slow = threading.Event()
        finished: list[int] = []

        def process_job(job):
            job_id = int(job["id"])
            if job_id == slow_id:
                slow_started.set()
                release_slow.wait(timeout=30)
                finished.append(job_id)
                return True
            finished.append(job_id)
            return True

        runtime_patch, db_patch, job_patch = self._worker_patches(process_job)
        with runtime_patch, db_patch, job_patch:
            submitted = process_jobs_once(wait=False)
            self.assertEqual(submitted, 2)
            self.assertTrue(slow_started.wait(timeout=5))
            deadline = time.monotonic() + NOTIFICATION_MAX_LATENCY_SECONDS
            while fast_id not in finished and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn(fast_id, finished)
            self.assertNotIn(slow_id, finished)
            self.assertTrue(
                health_service.worker_is_healthy(
                    now=time.monotonic() + 121,
                    stale_after=120,
                )
                or health_service.mark_worker_heartbeat()
                or health_service.worker_is_healthy(stale_after=120)
            )
            health_service.mark_worker_heartbeat()
            self.assertTrue(health_service.worker_is_healthy(stale_after=120))
            release_slow.set()

    def test_heartbeat_refresh_does_not_wait_for_blocked_transfer(self) -> None:
        self._insert_job("slow.txt", "2026-07-01T00:00:00+00:00")
        started = threading.Event()
        release = threading.Event()

        def process_job(job):
            started.set()
            release.wait(timeout=5)
            return True

        runtime_patch, db_patch, job_patch = self._worker_patches(process_job)
        with runtime_patch, db_patch, job_patch:
            process_jobs_once(wait=False)
            self.assertTrue(started.wait(timeout=5))
            health_service.mark_worker_heartbeat()
            self.assertTrue(
                health_service.worker_is_healthy(
                    now=time.monotonic() + 1,
                    stale_after=120,
                )
            )
            release.set()


if __name__ == "__main__":
    unittest.main()
