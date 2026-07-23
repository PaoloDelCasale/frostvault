"""Restore cost/time estimates and high-impact approval (issue #4).

Seams under test:
- Pure estimator `estimate_restore` (size, storage class, tier → cost/time).
- High-impact gate observed through Job status via ArchiveCatalog / worker:
  above threshold jobs stay `pending_approval` and never call AWS RestoreObject
  until primary-owner approval with recent reauthentication.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.main import queue_jobs
from app.services.restore_estimates import (
    estimate_restore,
    is_high_impact_restore,
)
from app.storage import process_jobs_once
from tests.test_database import run_alembic
from tests.test_recovery_verification import (
    _FakeBody,
    _prepare_cloud_only_version,
)


class RestoreEstimateTests(unittest.TestCase):
    def test_bulk_glacier_estimate_uses_configured_pricing(self) -> None:
        estimate = estimate_restore(
            size_bytes=10 * 1024**3,
            storage_class="GLACIER",
            tier="Bulk",
            days=3,
            pricing={
                "GLACIER": {"Expedited": 0.03, "Standard": 0.01, "Bulk": 0.0025},
                "DEEP_ARCHIVE": {"Standard": 0.02, "Bulk": 0.0025},
            },
            hours={
                "GLACIER": {"Expedited": 5 / 60, "Standard": 5, "Bulk": 12},
                "DEEP_ARCHIVE": {"Standard": 12, "Bulk": 48},
            },
        )
        self.assertEqual(estimate.tier, "Bulk")
        self.assertEqual(estimate.days, 3)
        self.assertAlmostEqual(estimate.estimated_cost_eur, 0.025, places=6)
        self.assertEqual(estimate.estimated_hours, 12)
        self.assertTrue(estimate.restore_object_irreversible)

    def test_high_impact_triggered_by_size_or_cost(self) -> None:
        self.assertTrue(
            is_high_impact_restore(
                size_bytes=100 * 1024**3,
                estimated_cost_eur=1.0,
                size_threshold_gib=100,
                cost_threshold_eur=10.0,
            )
        )
        self.assertTrue(
            is_high_impact_restore(
                size_bytes=1024,
                estimated_cost_eur=10.0,
                size_threshold_gib=100,
                cost_threshold_eur=10.0,
            )
        )
        self.assertFalse(
            is_high_impact_restore(
                size_bytes=50 * 1024**3,
                estimated_cost_eur=5.0,
                size_threshold_gib=100,
                cost_threshold_eur=10.0,
            )
        )


class HighImpactApprovalTests(unittest.TestCase):
    def test_high_impact_glacier_restore_waits_for_owner_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # 100 GiB → high impact by size even with cheap Bulk pricing
            size = 100 * 1024**3
            payload = b"x"  # content unused until download
            source, database_path, version_id = _prepare_cloud_only_version(
                root,
                relative_path="huge.bin",
                payload=payload,
                storage_class="GLACIER",
                object_key="docs/huge.bin",
            )
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    "UPDATE archive_versions SET size=%s WHERE id=%s",
                    (size, version_id),
                )

            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=900,
                restore_days=3,
                restore_tier="Bulk",
                restore_high_impact_gib=100,
                restore_high_impact_eur=10.0,
                restore_approval_hold_seconds=3600,
            )
            client = Mock()
            client.head_object = Mock(
                return_value={
                    "ContentLength": size,
                    "StorageClass": "GLACIER",
                    "VersionId": "s3-version-1",
                }
            )
            client.restore_object = Mock()
            client.get_object = Mock()
            with patch("app.database.settings", database_settings):
                queue_jobs("huge.bin", "recover", 2, 1)
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.storage.validate_cloud_vault"),
                    patch("app.storage.s3_client", return_value=client),
                ):
                    process_jobs_once()

            client.restore_object.assert_not_called()
            client.get_object.assert_not_called()
            client.head_object.assert_not_called()
            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT status, message, pending_until FROM jobs WHERE path=%s",
                    ("huge.bin",),
                ).fetchone()
            self.assertEqual(job["status"], "pending_approval")
            self.assertIsNotNone(job["pending_until"])
            self.assertFalse((source / "huge.bin").exists())
