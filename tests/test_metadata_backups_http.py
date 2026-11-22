"""Admin HTTP controls for encrypted metadata backups (issue #15).

Seams under test:
- ``GET /api/general/metadata-backups`` — status + recent runs
- ``POST /api/general/metadata-backups/run`` — manual backup
- ``GET /api/general/metadata-backups/download/{run_id}`` — download artifact
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from tests.fixtures import FrostVaultTestCase, override_settings
from tests.test_metadata_backups import RecordingObjectStore


class MetadataBackupHttpTests(FrostVaultTestCase):
    PASSWORD = "correct-horse-battery"
    owner = {"display_name": "Admin", "password": PASSWORD}

    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp_path
        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir()
        self.master_key = Fernet.generate_key().decode("ascii")
        self.enterContext(override_settings(
            archive_master_key=self.master_key,
            metadata_backup_dir=str(self.backup_dir),
            metadata_backup_retention=5,
            metadata_backup_interval_seconds=3600,
            metadata_backup_s3_prefix="system/backups/",
        ))

        self.store = RecordingObjectStore()
        self.enterContext(patch(
            "app.services.metadata_backups.default_object_store",
            return_value=self.store,
        ))

        self.client = self.http_client(follow_redirects=False)

    def test_backups_require_authentication(self) -> None:
        listed = self.client.get("/api/general/metadata-backups")
        self.assertEqual(listed.status_code, 401, listed.text)

    def test_owner_can_run_list_and_download_backup(self) -> None:
        self.sign_in(reauthenticated_until="2099-01-01T00:00:00+00:00", send_csrf_header=True)
        ran = self.client.post(
            "/api/general/metadata-backups/run",
            json={"reason": "operator requested backup"},
        )
        self.assertEqual(ran.status_code, 200, ran.text)
        body = ran.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["reason"], "manual")
        self.assertTrue(any(key.startswith("system/backups/") for key in self.store.objects))

        listed = self.client.get("/api/general/metadata-backups")
        self.assertEqual(listed.status_code, 200, listed.text)
        payload = listed.json()
        self.assertEqual(payload["status"]["last_status"], "succeeded")
        self.assertEqual(len(payload["runs"]), 1)
        run_id = payload["runs"][0]["id"]

        download = self.client.get(f"/api/general/metadata-backups/download/{run_id}")
        self.assertEqual(download.status_code, 200, download.text)
        self.assertEqual(
            download.headers.get("content-type"),
            "application/octet-stream",
        )
        self.assertEqual(
            hashlib_sha256(download.content),
            payload["runs"][0]["digest_sha256"],
        )
        with self.database.connect() as connection:
            actions = connection.execute("SELECT event, detail::text AS detail FROM audit_events ORDER BY id").fetchall()
        self.assertEqual([row["event"] for row in actions], ["metadata_backup_created", "metadata_backup_downloaded"])
        self.assertIn("operator requested backup", actions[0]["detail"])


def hashlib_sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


if __name__ == "__main__":
    unittest.main()
