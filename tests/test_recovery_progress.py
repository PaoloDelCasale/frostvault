"""#646: a Recovery in progress is visible on the file, in the list and in the panel."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import config
from app.catalog import ArchiveCatalog
from app.main import queue_jobs
from app.services.job_queries import list_jobs
from app.services.recovery_progress import project_recovery
from app.storage import process_jobs_once
from tests.postgres import PostgresConnection, database_dsn
from tests.test_recovery_verification import _prepare_cloud_only_version

VAULT = 2


def _instant(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


class ProjectRecoveryTests(unittest.TestCase):
    NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)

    def test_no_job_and_no_restore_has_no_recovery(self):
        self.assertIsNone(project_recovery({}, local_exists=False, now=self.NOW))

    def test_restoring_job_waits_for_cloud_until_tier_hours(self):
        recovery = project_recovery({
            "recovery_job_status": "restoring", "recovery_tier": "Standard", "recovery_days": 1,
            "recovery_requested_at": datetime(2026, 11, 14, 9, 57, tzinfo=timezone.utc),
            "storage_class": "DEEP_ARCHIVE", "restore_state": "restoring",
        }, local_exists=False, now=self.NOW)
        self.assertEqual(recovery["state"], "waiting_for_cloud")
        self.assertTrue(recovery["active"])
        self.assertEqual(recovery["tier"], "Standard")
        self.assertEqual(_instant(recovery["ready_by"]), datetime(2026, 11, 14, 21, 57, tzinfo=timezone.utc))
        self.assertIsNone(recovery["expires_at"])

    def test_downloading_and_approval_states(self):
        self.assertEqual(project_recovery({"recovery_job_status": "verifying"}, local_exists=False)["state"], "downloading")
        self.assertEqual(project_recovery({"recovery_job_status": "pending_approval"}, local_exists=False)["state"], "awaiting_approval")
        self.assertEqual(project_recovery({"recovery_job_status": "queued"}, local_exists=False)["state"], "queued")

    def test_readable_copy_is_ready_until_expiry_and_expired_copy_is_ignored(self):
        future = self.NOW + timedelta(days=1)
        ready = project_recovery({"restore_state": "available", "restore_expiry": future}, local_exists=False, now=self.NOW)
        self.assertEqual((ready["state"], ready["active"]), ("ready", False))
        self.assertEqual(_instant(ready["expires_at"]), future)
        self.assertIsNone(project_recovery({"restore_state": "available", "restore_expiry": self.NOW - timedelta(seconds=1)},
                                           local_exists=False, now=self.NOW))

    def test_finished_recovery_of_a_present_file_is_not_shown(self):
        self.assertIsNone(project_recovery({"recovery_job_status": "completed", "restore_state": "restoring"},
                                           local_exists=True, now=self.NOW))


class RecoveryInProgressCatalogTests(unittest.TestCase):
    """A DEEP_ARCHIVE recovery waiting for AWS (the scenario of #646)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.enterContext(patch("app.storage.cancelled_jobs", set()))
        _source, self.database_path, _ = _prepare_cloud_only_version(
            Path(self._tmp.name), relative_path="cold/c0.bin", payload=b"x",
            storage_class="DEEP_ARCHIVE", object_key="docs/cold/c0.bin",
        )
        database = SimpleNamespace(database_dsn=database_dsn(str(self.database_path)))
        self.enterContext(patch("app.database.settings", database))
        runtime = replace(config.settings, restore_tier="Bulk", restore_days=3)
        with patch("app.main.settings", runtime):
            queue_jobs("cold/c0.bin", "recover", VAULT, 1, False, restore_tier="Standard", restore_days=1)
        client = Mock()
        client.head_object = Mock(return_value={"ContentLength": 1, "StorageClass": "DEEP_ARCHIVE",
                                                "VersionId": "s3-version-1"})
        worker = SimpleNamespace(operation_concurrency=1, restore_poll_interval=900, restore_days=3,
                                 restore_tier="Bulk", restore_high_impact_gib=100, restore_high_impact_eur=10.0,
                                 restore_approval_hold_seconds=3600)
        with (patch("app.storage.settings", worker), patch("app.storage.validate_cloud_vault"),
              patch("app.storage.s3_client", return_value=client)):
            process_jobs_once()
        client.restore_object.assert_called_once()
        client.get_object.assert_not_called()

    def _item(self):
        with PostgresConnection(str(self.database_path)) as connection:
            return ArchiveCatalog(connection).get_file_item(VAULT, "cold/c0.bin")

    def _requested_at(self):
        with PostgresConnection(str(self.database_path)) as connection:
            return connection.execute("SELECT requested_at FROM jobs WHERE action='recover'").fetchone()["requested_at"]

    def test_file_detail_shows_the_recovery_and_offers_no_new_one(self):
        item = self._item()
        self.assertEqual(item["state"], "cloud_only")
        self.assertFalse(item["recover_eligible"])
        recovery = item["recovery"]
        self.assertEqual((recovery["state"], recovery["active"], recovery["tier"], recovery["days"]),
                         ("waiting_for_cloud", True, "Standard", 1))
        requested = self._requested_at()
        self.assertEqual(_instant(recovery["requested_at"]), requested)
        self.assertEqual(_instant(recovery["ready_by"]), requested + timedelta(hours=12))

    def test_list_page_carries_the_recovery(self):
        with PostgresConnection(str(self.database_path)) as connection:
            page = ArchiveCatalog(connection).list_files_page(VAULT, directory="cold")
        [item] = [row for row in page["items"] if row["type"] == "file"]
        self.assertEqual(item["recovery"]["state"], "waiting_for_cloud")
        self.assertFalse(item["recover_eligible"])

    def test_job_group_reports_request_instant_and_expected_readiness(self):
        with PostgresConnection(str(self.database_path)) as connection:
            [group] = list_jobs(connection, vault_id=VAULT, locale="it", actions=["recover"], active_only=True)["groups"]
        self.assertEqual(group["status"], "restoring")
        self.assertEqual(group["restore_tier"], "Standard")
        self.assertEqual(group["requested_at"], self._requested_at())
        self.assertEqual(_instant(group["ready_by"]), self._requested_at() + timedelta(hours=12))

    def test_stopped_wait_keeps_the_cloud_restore_and_offers_resuming_it(self):
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute("UPDATE jobs SET status='cancelled' WHERE action='recover'")
        item = self._item()
        self.assertTrue(item["recover_eligible"])
        self.assertEqual((item["recovery"]["state"], item["recovery"]["active"], item["recovery"]["tier"]),
                         ("waiting_for_cloud", False, "Standard"))

    def test_restored_copy_is_available_until_its_expiry(self):
        expiry = datetime.now(timezone.utc) + timedelta(days=1)
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute("UPDATE jobs SET status='cancelled' WHERE action='recover'")
            connection.execute("UPDATE archive_versions SET restore_state='available', restore_expiry=%s", (expiry,))
        item = self._item()
        self.assertTrue(item["recover_eligible"])
        self.assertEqual(item["recovery"]["state"], "ready")
        self.assertEqual(_instant(item["recovery"]["expires_at"]), expiry)


if __name__ == "__main__":
    unittest.main()
