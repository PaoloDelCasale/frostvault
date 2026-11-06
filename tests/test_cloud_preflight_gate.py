"""Pure policy/worker tests; persistent/concurrent acceptance runs on CI PostgreSQL."""
import unittest
from unittest.mock import patch

from app import storage
from app.services import cloud_preflight_gate as gate


class CloudPreflightPolicyTests(unittest.TestCase):
    def test_backoff_is_bounded_even_after_long_iam_propagation(self):
        self.assertEqual([gate.reprobe_seconds(i) for i in range(1, 8)], [5, 10, 20, 40, 60, 60, 60])
        self.assertLessEqual(gate.reprobe_seconds(10**9), 60)

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
