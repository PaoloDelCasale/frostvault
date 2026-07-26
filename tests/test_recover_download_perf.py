"""Recovery download throughput knobs (parallel S3 / rclone multi-thread).

Seams under test:
- ``rclone_download_perf_args`` / ``s3_download_transfer_config`` settings.
- Plain ``download_exact_version_plaintext`` uses ``download_file`` with a pinned
  VersionId and TransferConfig concurrency.
- Crypt recover path forwards multi-thread flags into ``run_rclone``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.storage import (
    download_exact_version_plaintext,
    job_bandwidth_bytes_per_sec,
    rclone_download_perf_args,
    s3_download_transfer_config,
)


class RecoverDownloadPerfTests(unittest.TestCase):
    def test_rclone_download_perf_args_use_settings(self) -> None:
        settings = SimpleNamespace(
            rclone_multi_thread_streams=12,
            rclone_multi_thread_cutoff_mib=32,
        )
        with patch("app.storage.settings", settings):
            args = rclone_download_perf_args()
        self.assertEqual(
            args,
            ["--multi-thread-streams=12", "--multi-thread-cutoff=32M"],
        )

    def test_s3_transfer_config_honours_job_bwlimit(self) -> None:
        settings = SimpleNamespace(
            s3_download_max_concurrency=6,
            s3_download_multipart_threshold_mib=16,
            s3_download_multipart_chunksize_mib=8,
        )
        with patch("app.storage.settings", settings):
            config = s3_download_transfer_config({"bwlimit": "256k"})
        self.assertEqual(config.max_concurrency, 6)
        self.assertEqual(config.multipart_threshold, 16 * 1024 * 1024)
        self.assertEqual(config.max_bandwidth, 256 * 1024)
        self.assertEqual(job_bandwidth_bytes_per_sec({"bwlimit": "256k"}), 256 * 1024)
        self.assertIsNone(job_bandwidth_bytes_per_sec({}))

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
                "rclone_remote": "plain-remote",
                "encryption_mode": "plain",
                "bwlimit": None,
            }
            settings = SimpleNamespace(
                s3_download_max_concurrency=10,
                s3_download_multipart_threshold_mib=8,
                s3_download_multipart_chunksize_mib=8,
                job_progress_min_interval_ms=50,
            )
            with (
                patch("app.storage.settings", settings),
                patch("app.storage.rclone_remote_is_crypt", return_value=False),
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

    def test_crypt_download_passes_multi_thread_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory) / "out.tmp"
            captured: list[tuple] = []

            def fake_rclone(*args, **kwargs) -> None:
                captured.append((args, kwargs))
                Path(args[2]).write_bytes(b"crypt-bytes")

            job = {
                "id": 43,
                "path": "file.txt",
                "s3_bucket": "bucket",
                "s3_prefix": "docs",
                "rclone_remote": "crypt-remote",
                "encryption_mode": None,
                "bwlimit": "128k",
            }
            settings = SimpleNamespace(
                rclone_multi_thread_streams=8,
                rclone_multi_thread_cutoff_mib=64,
                job_progress_min_interval_ms=50,
            )
            with (
                patch("app.storage.settings", settings),
                patch("app.storage.rclone_remote_is_crypt", return_value=True),
                patch("app.storage.run_rclone", side_effect=fake_rclone),
            ):
                download_exact_version_plaintext(
                    job,
                    object_key="docs/file.txt.bin",
                    provider_version_id="crypt-v",
                    temporary=temporary,
                )
            self.assertEqual(len(captured), 1)
            args, kwargs = captured[0]
            self.assertEqual(args[0], "copyto")
            self.assertIn("--s3-version-id=crypt-v", args)
            self.assertIn("--multi-thread-streams=8", args)
            self.assertIn("--multi-thread-cutoff=64M", args)
            self.assertEqual(kwargs.get("bwlimit"), "128k")


if __name__ == "__main__":
    unittest.main()
