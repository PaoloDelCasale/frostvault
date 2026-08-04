"""HTTP coverage for recovery version listing, estimates, and approval (issue #4)."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.catalog import ArchiveCatalog
from app.config import settings
from app.database import SQLiteConnection
from app.security import hash_password
from app.sessions import create_session, csrf_token_for
from app.storage import process_jobs_once
from tests.test_database import run_alembic


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class RecoveryHttpTests(unittest.TestCase):
    PASSWORD = "correct-horse-battery"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        self.source = Path(self._tmp.name) / "source"
        self.source.mkdir()
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with SQLiteConnection(str(self.database_path)) as connection:
            self.owner_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('owner', 'Owner', %s, FALSE) RETURNING id
                """,
                (hash_password(self.PASSWORD),),
            ).fetchone()["id"]
            self.operator_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('operator', 'Operator', %s, FALSE) RETURNING id
                """,
                (hash_password(self.PASSWORD),),
            ).fetchone()["id"]
            self.vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('docs', 'Docs', %s, 'bucket', 'docs', 'remote')
                RETURNING id
                """,
                (str(self.source),),
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (%s, %s, 'owner')",
                (self.vault_id, self.owner_id),
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (%s, %s, 'operator')",
                (self.vault_id, self.operator_id),
            )
            catalog = ArchiveCatalog(connection)
            payload = b"http-recover"
            catalog.observe_local_copy(
                vault_id=self.vault_id,
                path="docs/note.txt",
                file_type="regular",
                size=len(payload),
                mtime_ns=1,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            file_row = catalog.get_file_by_path(self.vault_id, "docs/note.txt")
            catalog.mark_local_copy_missing(
                file_row["id"], observed_at="2026-07-21T11:00:00+00:00"
            )
            self.version_id = catalog.record_archive_version(
                vault_id=self.vault_id,
                path="docs/note.txt",
                object_key="docs/docs/note.txt",
                provider_version_id="v1",
                size=len(payload),
                storage_class="GLACIER",
                etag="etag",
                uploaded_at="2026-07-21T10:00:00+00:00",
                observed_at="2026-07-21T10:00:00+00:00",
                scan_id="s1",
            )
            catalog.mark_version_verified(
                self.version_id,
                plaintext_sha256=_sha(payload),
                verified_at="2026-07-21T10:01:00+00:00",
            )

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
            restore_high_impact_gib=100,
            restore_high_impact_eur=10.0,
            restore_approval_hold_seconds=3600,
            restore_tier="Bulk",
            restore_days=3,
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
            "app.storage.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(
            app=main.app, client=("127.0.0.1", 50000), follow_redirects=False
        )

    def _authenticate(self, user_id: int) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            raw_token = create_session(connection, user_id=user_id, auth_method="oidc")
            csrf_token = csrf_token_for(connection, raw_token)
        self.client.cookies.set(self.test_settings.session_cookie_name, raw_token)
        self.client.cookies.set("frostvault_csrf", csrf_token)

    def _csrf(self) -> dict:
        return {"X-CSRF-Token": self.client.cookies.get("frostvault_csrf") or ""}

    def test_versions_and_estimate_expose_irreversible_restore_boundary(self) -> None:
        self._authenticate(self.owner_id)
        versions = self.client.get(
            "/api/files/versions",
            params={"path": "docs/note.txt"},
        )
        self.assertEqual(versions.status_code, 200, versions.text)
        body = versions.json()
        self.assertEqual(body["recoverable_count"], 1)
        self.assertEqual(body["default_archive_version_id"], self.version_id)

        estimate = self.client.post(
            "/api/recover/estimate",
            headers=self._csrf(),
            json={
                "path": "docs/note.txt",
                "archive_version_id": self.version_id,
                "restore_tier": "Bulk",
                "restore_days": 3,
            },
        )
        self.assertEqual(estimate.status_code, 200, estimate.text)
        payload = estimate.json()
        self.assertTrue(payload["requires_restore"])
        self.assertTrue(payload["restore_object_irreversible"])
        self.assertEqual(payload["estimate"]["tier"], "Bulk")
        self.assertIsNone(payload["estimate"]["price_book_id"])
        self.assertEqual(payload["estimate"]["price_book_name"], "builtin-defaults")

    def test_operator_cannot_approve_high_impact_restore(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE archive_versions SET size=%s WHERE id=%s",
                (100 * 1024**3, self.version_id),
            )
        self._authenticate(self.operator_id)
        queued = self.client.post(
            "/api/recover",
            headers=self._csrf(),
            json={"path": "docs/note.txt", "archive_version_id": self.version_id},
        )
        self.assertEqual(queued.status_code, 202, queued.text)
        group_id = queued.json()["group_id"]
        with (
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.s3_client"),
        ):
            process_jobs_once()
        denied = self.client.post(
            "/api/recover/approve",
            headers=self._csrf(),
            json={"group_id": group_id},
        )
        self.assertEqual(denied.status_code, 403, denied.text)


if __name__ == "__main__":
    unittest.main()
