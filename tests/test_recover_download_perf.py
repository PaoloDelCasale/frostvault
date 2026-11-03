"""Recovery download throughput knobs (parallel S3).

Seams under test:
- ``current_key_download_perf_args`` / ``s3_download_transfer_config`` settings.
- Plain ``download_exact_version_plaintext`` uses ``download_file`` with a pinned
  VersionId and TransferConfig concurrency.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.storage import (
    download_exact_version_plaintext,
    s3_download_transfer_config,
)


class RecoverDownloadPerfTests(unittest.TestCase):

    def test_s3_transfer_config_uses_settings_and_is_never_throttled(self) -> None:
        settings = SimpleNamespace(
            s3_download_max_concurrency=6,
            s3_download_multipart_threshold_mib=16,
            s3_download_multipart_chunksize_mib=8,
        )
        with patch("app.storage.settings", settings):
            config = s3_download_transfer_config()
        self.assertEqual(config.max_concurrency, 6)
        self.assertEqual(config.multipart_threshold, 16 * 1024 * 1024)
        # Only uploads are throttled (#413); recoveries run at full speed.
        self.assertIsNone(config.max_bandwidth)

    def test_plain_download_uses_transfer_manager_with_version_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory) / "out.tmp"
            payload = b"plain-recover-bytes"
            client = Mock()

            def download_file(
                Bucket,
                Key,
                Filename,
                ExtraArgs=None,
                Callback=None,
                Config=None,
            ):
                self.assertEqual(Bucket, "bucket")
                self.assertEqual(Key, "docs/file.txt")
                self.assertEqual(ExtraArgs, {"VersionId": "v-1"})
                self.assertIsNotNone(Config)
                self.assertEqual(Config.max_concurrency, 10)
                Path(Filename).write_bytes(payload)
                if Callback is not None:
                    Callback(len(payload))

            client.download_file = Mock(side_effect=download_file)
            job = {
                "id": 42,
                "path": "file.txt",
                "s3_bucket": "bucket",
                "s3_prefix": "docs",

            }
            settings = SimpleNamespace(
                s3_download_max_concurrency=10,
                s3_download_multipart_threshold_mib=8,
                s3_download_multipart_chunksize_mib=8,
                job_progress_min_interval_ms=50,
            )
            with (
                patch("app.storage.settings", settings),
                patch("app.storage.s3_client", return_value=client),
                patch("app.storage.set_job_progress"),
            ):
                download_exact_version_plaintext(
                    job,
                    object_key="docs/file.txt",
                    provider_version_id="v-1",
                    temporary=temporary,
                )
            client.download_file.assert_called_once()
            self.assertEqual(temporary.read_bytes(), payload)

if __name__ == "__main__":
    unittest.main()
