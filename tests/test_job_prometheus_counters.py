"""Committed Job outcomes increment Prometheus counters (issue #305)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

for _flag, _value in (
    ("O_DIRECTORY", 0x10000),
    ("O_NOFOLLOW", 0x20000),
    ("O_CLOEXEC", 0x80000),
):
    if not hasattr(os, _flag):
        setattr(os, _flag, _value)

from app.services.local_copies import LocalCopies

from app.services import metrics as metrics_service
from app.storage import (
    observe_failed_job_backlog,
    reconcile_interrupted_jobs,
    schedule_upload_retry,
    set_job,
)
from tests.fixtures import FrostVaultTestCase, seed_vault

DEAD_WORKER = "00000000-0000-4000-8000-00000000dead"


class JobPrometheusCounterTests(FrostVaultTestCase):
    owner = None

    def setUp(self) -> None:
        super().setUp()
        metrics_service.reset_for_tests()
        with self.database.connect() as connection:
            seed_vault(connection, id=2, source_root="/source")
            local_copies = LocalCopies(connection)
            self.file_id = local_copies.observe_local_copy(
                vault_id=2,
                path="a.txt",
                file_type="regular",
                size=1,
                mtime_ns=1,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            self.job_id = self._insert_job(connection, "upload")
        self._db = patch(
            "app.storage.db",
            side_effect=lambda: self.database.connect(),
        )
        self._db.start()
        self.addCleanup(self._db.stop)

    def _insert_job(self, connection, action: str, status: str = "uploading") -> int:
        return int(
            connection.execute(
                """
                INSERT INTO jobs(vault_id, file_id, action, status, requested_at, updated_at)
                VALUES (2, %s, %s, %s, '2026-07-21T10:00:00+00:00',
                        '2026-07-21T10:00:00+00:00')
                RETURNING id
                """,
                (self.file_id, action, status),
            ).fetchone()["id"]
        )

    def _dead_lease(self, connection, job_id: int) -> None:
        connection.execute(
            """
            INSERT INTO job_leases(job_id, token, claimed_at, expires_at)
            VALUES (%s, %s, '2000-01-01T00:00:00+00:00', '2000-01-01T00:05:00+00:00')
            """,
            (job_id, DEAD_WORKER),
        )

    def test_failed_and_retried_jobs_increment_prometheus_counters(self) -> None:
        self.assertTrue(schedule_upload_retry(self.job_id, retry_count=1))
        self.assertTrue(set_job(self.job_id, "failed", "AccessDenied"))
        text = metrics_service.render_prometheus()
        self.assertIn("# TYPE jobs_retries_total counter", text)
        self.assertIn('jobs_retries_total{action="upload"} 1', text)
        self.assertIn("# TYPE jobs_failed_total counter", text)
        self.assertIn('jobs_failed_total{action="upload"} 1', text)
        self.assertNotIn("jobs_completed_total", text)
        self.assertNotIn("path=", text)
        with patch("app.services.upload_failure_problems.CLOSED_UPLOAD_FAILURE_SQL", "FALSE"):
            count, age = observe_failed_job_backlog()
        self.assertEqual(count, 1)
        self.assertGreaterEqual(age, 0.0)

    def test_completed_job_increments_once(self) -> None:
        self.assertTrue(set_job(self.job_id, "completed", "ok"))
        # A terminal Job is final (#589): a repeated outcome writes nothing.
        self.assertFalse(set_job(self.job_id, "completed", "ok again"))
        text = metrics_service.render_prometheus()
        self.assertIn('jobs_completed_total{action="upload"} 1', text)
        self.assertNotIn("jobs_failed_total", text)

    def test_lost_claim_does_not_increment(self) -> None:
        with patch("app.services.job_queue.claim_is_lost", return_value=True):
            self.assertFalse(set_job(self.job_id, "failed", "stale"))
            self.assertFalse(schedule_upload_retry(self.job_id, retry_count=1))
        text = metrics_service.render_prometheus()
        self.assertNotIn("jobs_failed_total", text)
        self.assertNotIn("jobs_retries_total", text)

    def test_cas_miss_does_not_increment(self) -> None:
        with patch("app.services.job_queue.claim_token_for", return_value=DEAD_WORKER):
            self.assertFalse(set_job(self.job_id, "failed", "stale"))
        text = metrics_service.render_prometheus()
        self.assertNotIn("jobs_failed_total", text)

    def test_reconcile_failed_job_increments_after_commit(self) -> None:
        with self.database.connect() as connection:
            self._dead_lease(connection, self.job_id)
        with patch(
            "app.storage.source_layout.vault_local_access",
            return_value=type(
                "Access",
                (),
                {"local_operations_allowed": True},
            )(),
        ):
            summary = reconcile_interrupted_jobs()
        self.assertEqual(summary["requeued"], 1)
        text = metrics_service.render_prometheus()
        self.assertNotIn("jobs_failed_total", text)
        self.assertNotIn("jobs_completed_total", text)

    def test_reconcile_failure_increments_failed_counter(self) -> None:
        with self.database.connect() as connection:
            # A Job never changes action: end the upload, then interrupt a recovery.
            connection.execute("UPDATE jobs SET status='cancelled' WHERE id=%s", (self.job_id,))
            self.job_id = self._insert_job(connection, "recover", "downloading")
            self._dead_lease(connection, self.job_id)
        with patch(
            "app.storage.source_layout.vault_local_access",
            return_value=type(
                "Access",
                (),
                {"local_operations_allowed": True},
            )(),
        ):
            summary = reconcile_interrupted_jobs()
        self.assertEqual(summary["failed"], 1)
        text = metrics_service.render_prometheus()
        self.assertIn('jobs_failed_total{action="recover"} 1', text)
        with self.database.connect() as connection:
            status = connection.execute(
                "SELECT status FROM jobs WHERE id=%s", (self.job_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "failed")


if __name__ == "__main__":
    unittest.main()
