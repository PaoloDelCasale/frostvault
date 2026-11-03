"""One installation-wide upload cap; recovery is never throttled (#420)."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.storage import upload_s3_file, s3_download_transfer_config, _ThrottledByteProgress


class SDKBandwidthTests(unittest.TestCase):
    def test_limit_is_converted_to_bytes_for_managed_upload_only(self):
        for limit, expected in ((None, None), (0, None), (512, 512 * 1024)):
            with self.subTest(limit=limit), patch("app.storage._runtime_settings", return_value=SimpleNamespace(bandwidth_limit_kibps=limit, job_progress_min_interval_ms=500)), patch("app.storage.s3_client"), patch("app.storage.set_job_progress"), patch("app.storage.upload_file", return_value={"VersionId": "v"}) as upload:
                self.assertEqual(upload_s3_file({"id": 1, "s3_bucket": "bucket"}, source=Path("file"), key="vaults/id/file"), {"VersionId": "v"})
                self.assertEqual(upload.call_args.kwargs["max_bandwidth"], expected)
        self.assertIsNone(s3_download_transfer_config().max_bandwidth)

    def test_progress_is_throttled_and_retry_rewinds_are_not_double_counted(self):
        job = {"id": 1, "claim_token": "claim"}
        with patch("app.storage.set_job_progress") as report, patch("app.storage.time.monotonic", return_value=100):
            progress = _ThrottledByteProgress(job)
            progress.add(10)
            for _ in range(100):
                progress.add(1)
            progress.add(-20)
            self.assertEqual(report.call_count, 1)
            progress.flush()
        report.assert_called_with(1, 90, claim_token="claim")
