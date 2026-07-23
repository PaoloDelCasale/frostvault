"""Helpers for S3 integrity CI must not persist secrets in clear text.

Seam: tests.s3_integration_support.write_s3_rclone_config — rclone config
files use env_auth and never embed access/secret keys.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.s3_integration_support import write_s3_rclone_config


class RcloneConfigSecretStorageTests(unittest.TestCase):
    def test_minio_config_does_not_store_secret_in_clear_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rclone.conf"
            secret = "super-secret-integration-key"
            write_s3_rclone_config(
                path,
                endpoint="http://127.0.0.1:9000",
                access_key="minioadmin",
                secret_key=secret,
                bucket="ci",
                prefix="plain",
            )
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(secret, text)
            self.assertNotIn("secret_access_key", text)
            self.assertNotIn("access_key_id", text)
            self.assertIn("env_auth = true", text)
            self.assertIn("endpoint = http://127.0.0.1:9000", text)


if __name__ == "__main__":
    unittest.main()
