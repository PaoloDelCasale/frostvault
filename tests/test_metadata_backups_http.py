"""Admin HTTP controls for encrypted metadata backups (issue #15).

Seams under test:
- ``GET /api/admin/metadata-backups`` — status + recent runs
- ``POST /api/admin/metadata-backups/run`` — manual backup
- ``GET /api/admin/metadata-backups/download/{run_id}`` — download artifact
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.database import SQLiteConnection
from app.security import hash_password
from app.services import metadata_backups
from app.sessions import create_session, csrf_token_for
from tests.test_database import run_alembic
from tests.test_metadata_backups import RecordingObjectStore


class MetadataBackupHttpTests(unittest.TestCase):
    PASSWORD = "correct-horse-battery"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.database_path = self.root / "app.db"
        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir()
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        self.master_key = Fernet.generate_key().decode("ascii")
        with SQLiteConnection(str(self.database_path)) as connection:
            self.admin_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('admin', 'Admin', %s, TRUE) RETURNING id
                """,
                (hash_password(self.PASSWORD),),
            ).fetchone()["id"]
            self.member_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('member', 'Member', %s, FALSE) RETURNING id
                """,
                (hash_password(self.PASSWORD),),
            ).fetchone()["id"]

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
            archive_master_key=self.master_key,
            metadata_backup_dir=str(self.backup_dir),
            metadata_backup_retention=5,
            metadata_backup_interval_seconds=3600,
            metadata_backup_s3_prefix="system/backups/",
            vault_s3_bucket="archive-bucket",
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
            "app.services.metadata_backups.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.store = RecordingObjectStore()
        store_patcher = patch(
            "app.services.metadata_backups.default_object_store",
            return_value=self.store,
        )
        store_patcher.start()
        self.addCleanup(store_patcher.stop)

        self.client = TestClient(
            app=main.app, client=("127.0.0.1", 50000), follow_redirects=False
        )

    def _authenticate(self, user_id: int, *, reauth: bool = True) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            raw_token = create_session(connection, user_id=user_id, auth_method="oidc")
            if reauth:
                connection.execute(
                    "UPDATE sessions SET reauth_at=%s WHERE user_id=%s",
                    ("2099-01-01T00:00:00+00:00", user_id),
                )
            csrf_token = csrf_token_for(connection, raw_token)
        self.client.cookies.set(self.test_settings.session_cookie_name, raw_token)
        self.client.cookies.set("frostvault_csrf", csrf_token)
        self.client.headers["X-CSRF-Token"] = csrf_token

    def test_member_cannot_list_or_run_backups(self) -> None:
        self._authenticate(self.member_id)
        listed = self.client.get("/api/admin/metadata-backups")
        self.assertEqual(listed.status_code, 403, listed.text)
        ran = self.client.post(
            "/api/admin/metadata-backups/run",
            json={"reason": "operator requested backup"},
        )
        self.assertEqual(ran.status_code, 403, ran.text)

    def test_admin_can_run_list_and_download_backup(self) -> None:
        self._authenticate(self.admin_id)
        ran = self.client.post(
            "/api/admin/metadata-backups/run",
            json={"reason": "operator requested backup"},
        )
        self.assertEqual(ran.status_code, 200, ran.text)
        body = ran.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["reason"], "manual")
        self.assertTrue(any(key.startswith("system/backups/") for key in self.store.objects))

        listed = self.client.get("/api/admin/metadata-backups")
        self.assertEqual(listed.status_code, 200, listed.text)
        payload = listed.json()
        self.assertEqual(payload["status"]["last_status"], "succeeded")
        self.assertEqual(len(payload["runs"]), 1)
        run_id = payload["runs"][0]["id"]

        download = self.client.get(f"/api/admin/metadata-backups/download/{run_id}")
        self.assertEqual(download.status_code, 200, download.text)
        self.assertEqual(
            download.headers.get("content-type"),
            "application/octet-stream",
        )
        self.assertEqual(
            hashlib_sha256(download.content),
            payload["runs"][0]["digest_sha256"],
        )


def hashlib_sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


if __name__ == "__main__":
    unittest.main()
