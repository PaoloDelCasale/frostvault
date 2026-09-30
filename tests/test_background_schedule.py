"""Background maintenance schedule marks (B3).

Seam: ``storage.persisted_maintenance_ages`` and
``storage.initial_maintenance_marks``.  ``background_loop`` compares its last-run
marks with ``loop.time()``, a monotonic clock whose zero is host boot on Linux.
Starting every mark at ``0.0`` tied the first scan, audit, and backup to host
uptime instead of to when the task really last ran.
"""

from __future__ import annotations

import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import storage
from app.database import SQLiteConnection
from tests.test_database import run_alembic


class InitialMarksTests(unittest.TestCase):
    def test_never_run_tasks_are_due_immediately_whatever_the_clock_says(self) -> None:
        marks = storage.initial_maintenance_marks(120.0, {})
        for name in storage.MAINTENANCE_TASKS:
            self.assertTrue(math.isinf(marks[name]))
            # A host booted two minutes ago must still run its weekly audit.
            self.assertGreaterEqual(120.0 - marks[name], 7 * 24 * 60 * 60)

    def test_recent_runs_are_placed_relative_to_the_loop_clock(self) -> None:
        marks = storage.initial_maintenance_marks(
            5_000.0, {"scan": None, "audit": 3_600.0, "backup": 0.0}
        )
        self.assertEqual(marks["audit"], 1_400.0)
        self.assertEqual(marks["backup"], 5_000.0)
        self.assertTrue(math.isinf(marks["scan"]))
        self.assertTrue(math.isinf(marks["backup_verify"]))


class PersistedAgesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "schedule.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

    def _ages(self) -> dict[str, float | None]:
        with patch(
            "app.storage.db",
            side_effect=lambda: SQLiteConnection(str(self.path)),
        ):
            return storage.persisted_maintenance_ages(now=self.now)

    def test_fresh_database_reports_every_task_as_never_run(self) -> None:
        self.assertEqual(
            self._ages(),
            {"scan": None, "audit": None, "backup": None, "backup_verify": None},
        )

    def test_ages_come_from_durable_records_and_the_scan_is_always_due(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            for vault_id in (1, 2):
                connection.execute(
                    "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                    "VALUES (%s, %s, %s, '/source', 'bucket', 'p', 'remote')",
                    (vault_id, f"v{vault_id}", f"V{vault_id}"),
                )
            connection.execute(
                """
                INSERT INTO vault_component_health(
                    vault_id, component, last_success_at, healthy, updated_at
                ) VALUES (1, 'audit', %s, TRUE, %s), (2, 'audit', %s, TRUE, %s)
                """,
                (
                    (self.now - timedelta(days=1)).isoformat(),
                    (self.now - timedelta(days=1)).isoformat(),
                    (self.now - timedelta(days=3)).isoformat(),
                    (self.now - timedelta(days=3)).isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO metadata_backup_runs(
                    created_at, finished_at, reason, backend, status, verified_at
                ) VALUES
                    (%s, %s, 'pre_upgrade', 'sqlite', 'succeeded', NULL),
                    (%s, %s, 'scheduled', 'sqlite', 'verified', %s)
                """,
                (
                    (self.now - timedelta(hours=1)).isoformat(),
                    (self.now - timedelta(hours=1)).isoformat(),
                    (self.now - timedelta(hours=20)).isoformat(),
                    (self.now - timedelta(hours=20)).isoformat(),
                    (self.now - timedelta(hours=6)).isoformat(),
                ),
            )

        ages = self._ages()
        self.assertIsNone(ages["scan"])
        # The Vault audited longest ago drives the next audit pass.
        self.assertEqual(ages["audit"], 3 * 24 * 60 * 60)
        # A pre-upgrade backup does not count as the scheduled one.
        self.assertEqual(ages["backup"], 20 * 60 * 60)
        self.assertEqual(ages["backup_verify"], 6 * 60 * 60)

    def test_one_vault_without_an_audit_record_makes_the_audit_due(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            for vault_id in (1, 2):
                connection.execute(
                    "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                    "VALUES (%s, %s, %s, '/source', 'bucket', 'p', 'remote')",
                    (vault_id, f"v{vault_id}", f"V{vault_id}"),
                )
            connection.execute(
                """
                INSERT INTO vault_component_health(
                    vault_id, component, last_success_at, healthy, updated_at
                ) VALUES (1, 'audit', %s, TRUE, %s)
                """,
                (self.now.isoformat(), self.now.isoformat()),
            )
        self.assertIsNone(self._ages()["audit"])


if __name__ == "__main__":
    unittest.main()
