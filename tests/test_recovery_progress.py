"""#646: a Recovery in progress is visible on the file, in the list and in the panel."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from app.services.file_listing import FileListing
from app.services.job_admission import queue_jobs
from app.services.job_queries import list_jobs
from app.services.recovery_progress import project_recovery
from app.storage import process_jobs_once
from tests.fixtures import FrostVaultTestCase, override_settings
from tests.test_recovery_verification import _prepare_cloud_only_version
from app.services.job_queue import JobQueue

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


class RecoveryInProgressCatalogTests(FrostVaultTestCase):
    """A DEEP_ARCHIVE recovery waiting for AWS (the scenario of #646)."""

    owner = None

    def setUp(self):
        super().setUp()
        self.enterContext(patch("app.services.job_queue.cancelled_jobs", set()))
        _source, _ = _prepare_cloud_only_version(
            self.database, self.tmp_path, relative_path="cold/c0.bin", payload=b"x",
            storage_class="DEEP_ARCHIVE", object_key="docs/cold/c0.bin",
        )
        runtime = dict(restore_tier="Bulk", restore_days=3)
        with override_settings(**runtime):
            queue_jobs("cold/c0.bin", "recover", VAULT, False, restore_tier="Standard", restore_days=1)
        client = Mock()
        client.head_object = Mock(return_value={"ContentLength": 1, "StorageClass": "DEEP_ARCHIVE",
                                                "VersionId": "s3-version-1"})
        worker = dict(operation_concurrency=1, restore_poll_interval=900, restore_days=3,
                                 restore_tier="Bulk", restore_high_impact_gib=100, restore_high_impact_eur=10.0,
                                 restore_approval_hold_seconds=3600)
        with (override_settings(**worker), patch("app.storage.validate_cloud_vault"),
              patch("app.storage.s3_client", return_value=client)):
            process_jobs_once()
        client.restore_object.assert_called_once()
        client.get_object.assert_not_called()

    def _item(self):
        with self.database.connect() as connection:
            return FileListing(connection).get_file_item(VAULT, "cold/c0.bin")

    def _requested_at(self):
        with self.database.connect() as connection:
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
        with self.database.connect() as connection:
            page = FileListing(connection).list_files_page(VAULT, directory="cold")
        [item] = [row for row in page["items"] if row["type"] == "file"]
        self.assertEqual(item["recovery"]["state"], "waiting_for_cloud")
        self.assertFalse(item["recover_eligible"])

    def test_job_group_reports_request_instant_and_expected_readiness(self):
        with self.database.connect() as connection:
            [group] = list_jobs(connection, vault_id=VAULT, locale="it", actions=["recover"], active_only=True)["groups"]
        self.assertEqual(group["status"], "restoring")
        self.assertEqual(group["restore_tier"], "Standard")
        self.assertEqual(group["requested_at"], self._requested_at())
        self.assertEqual(_instant(group["ready_by"]), self._requested_at() + timedelta(hours=12))

    def test_stopped_wait_keeps_the_cloud_restore_and_offers_resuming_it(self):
        with self.database.connect() as connection:
            connection.execute("UPDATE jobs SET status='cancelled' WHERE action='recover'")
        item = self._item()
        self.assertTrue(item["recover_eligible"])
        self.assertEqual((item["recovery"]["state"], item["recovery"]["active"], item["recovery"]["tier"]),
                         ("waiting_for_cloud", False, "Standard"))

    def _process(self, restore_header=None):
        client = Mock()
        head = {"ContentLength": 1, "StorageClass": "DEEP_ARCHIVE", "VersionId": "s3-version-1"}
        if restore_header:
            head["Restore"] = restore_header
        client.head_object = Mock(return_value=head)
        worker = dict(operation_concurrency=1, restore_poll_interval=900, restore_days=3,
                                 restore_tier="Bulk", restore_high_impact_gib=100, restore_high_impact_eur=10.0,
                                 restore_approval_hold_seconds=3600)
        with (override_settings(**worker), patch("app.storage.validate_cloud_vault"),
              patch("app.storage.s3_client", return_value=client)):
            process_jobs_once()
        return client

    def _stop_waiting(self):
        with self.database.connect() as connection:
            ids = [row["id"] for row in connection.execute(
                "SELECT id FROM jobs WHERE action='recover' AND status NOT IN ('completed','failed','cancelled')")]
            JobQueue(connection).cancel_open_jobs(
                vault_id=VAULT, job_ids=ids, message="Recovery stopped",
                message_key="job.recovery_stopped", now=datetime.now(timezone.utc).isoformat())

    def _resume(self):
        runtime = dict(restore_tier="Bulk", restore_days=3)
        with override_settings(**runtime):
            queue_jobs("cold/c0.bin", "recover", VAULT, False, restore_tier="Bulk", restore_days=3)
        client = self._process('ongoing-request="true"')
        client.restore_object.assert_not_called()

    def test_resumed_recovery_shows_the_first_cloud_request(self):
        """#677: a new «Recupera» after «Smetti di attendere» keeps the AWS request time."""
        with self.database.connect() as connection:
            connection.execute("UPDATE jobs SET requested_at=requested_at - interval '5 minutes' WHERE action='recover'")
        first = self._requested_at()
        self._stop_waiting()
        with self.database.connect() as connection:
            [row] = connection.execute("SELECT message_key FROM jobs WHERE action='recover'").fetchall()
        self.assertEqual(row["message_key"], "job.recovery_stopped_waiting")
        self._resume()
        recovery = self._item()["recovery"]
        self.assertEqual((recovery["state"], recovery["active"], recovery["tier"], recovery["days"]),
                         ("waiting_for_cloud", True, "Standard", 1))
        self.assertEqual(_instant(recovery["requested_at"]), first)
        self.assertEqual(_instant(recovery["ready_by"]), first + timedelta(hours=12))
        with self.database.connect() as connection:
            [group] = list_jobs(connection, vault_id=VAULT, locale="it", actions=["recover"], active_only=True)["groups"]
        self.assertEqual(group["requested_at"], first)
        # A second stop and resume still leads back to the first request.
        self._stop_waiting()
        self._resume()
        self.assertEqual(_instant(self._item()["recovery"]["requested_at"]), first)

    def test_recovery_stopped_before_aws_resumes_nothing(self):
        runtime = dict(restore_tier="Bulk", restore_days=3)
        with self.database.connect() as connection:
            connection.execute("UPDATE jobs SET status='queued' WHERE action='recover'")
        self._stop_waiting()
        with self.database.connect() as connection:
            [row] = connection.execute("SELECT message_key FROM jobs WHERE action='recover'").fetchall()
        self.assertEqual(row["message_key"], "job.recovery_stopped")
        with override_settings(**runtime):
            queue_jobs("cold/c0.bin", "recover", VAULT, False, restore_tier="Bulk", restore_days=3)
        with self.database.connect() as connection:
            latest = connection.execute(
                "SELECT requested_at FROM jobs WHERE action='recover' ORDER BY id DESC LIMIT 1").fetchone()["requested_at"]
        recovery = self._item()["recovery"]
        self.assertEqual(_instant(recovery["requested_at"]), latest)
        self.assertEqual(recovery["tier"], "Bulk")

    def test_restored_copy_is_available_until_its_expiry(self):
        expiry = datetime.now(timezone.utc) + timedelta(days=1)
        with self.database.connect() as connection:
            connection.execute("UPDATE jobs SET status='cancelled' WHERE action='recover'")
            connection.execute("UPDATE archive_versions SET restore_state='available', restore_expiry=%s", (expiry,))
        item = self._item()
        self.assertTrue(item["recover_eligible"])
        self.assertEqual(item["recovery"]["state"], "ready")
        self.assertEqual(_instant(item["recovery"]["expires_at"]), expiry)


if __name__ == "__main__":
    unittest.main()
