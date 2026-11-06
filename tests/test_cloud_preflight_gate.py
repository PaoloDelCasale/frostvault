"""Pure policy/worker tests; persistent/concurrent acceptance runs on CI PostgreSQL."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import psycopg

from app import storage
from app.services import cloud_preflight_gate as gate


class CloudPreflightPolicyTests(unittest.TestCase):
    def test_backoff_is_bounded_even_after_long_iam_propagation(self):
        self.assertEqual([gate.reprobe_seconds(i) for i in range(1, 8)], [5, 10, 20, 40, 60, 60, 60])
        self.assertLessEqual(gate.reprobe_seconds(10**9), 60)

    def test_clock_rollback_and_invalid_restart_timestamp_do_not_hold_the_gate(self):
        now = datetime.now(timezone.utc)
        for due in (None, 'invalid', (now + timedelta(hours=1)).isoformat(), (now - timedelta(seconds=1)).isoformat()):
            self.assertTrue(gate.probe_due({'next_probe_at': due}, now))
        self.assertFalse(gate.probe_due({'next_probe_at': (now + timedelta(seconds=60)).isoformat()}, now))

    def test_aborted_readiness_publication_is_suspension_not_a_transfer_retry(self):
        with patch.object(gate, 'check', side_effect=psycopg.errors.SerializationFailure()) as check:
            with self.assertRaises(gate.CloudPreflightBlocked):
                storage.validate_cloud_vault({'s3_bucket': 'bucket'})
            check.assert_called_once()  # No provider replay due to a DB abort.

    def test_blockage_preserves_job_retry_budget_and_never_retries_mutations(self):
        for action, handler in (("upload", "process_upload"), ("recover", "process_recover"),
                                ("rename", "process_rename"), ("free-space", "process_free_space"),
                                ("storage-class", "process_storage_class")):
            job = {"id": 99001, "action": action, "status": "queued", "retry_count": 7}
            with self.subTest(action=action), patch.object(storage, "_runtime_settings"), patch.object(
                storage, "_load_claimed_job_status", return_value={"status": "queued"}
            ), patch.object(storage, handler, side_effect=gate.CloudPreflightBlocked("IAM")), patch.object(
                storage, "set_job", return_value=True
            ) as update, patch.object(storage, "schedule_upload_retry") as retry:
                self.assertFalse(storage._process_claimed_job(job))
                self.assertEqual(job["retry_count"], 7)
                update.assert_called_once_with(job["id"], "queued", "S3 readiness blocked; Job remains queued")
                retry.assert_not_called()
