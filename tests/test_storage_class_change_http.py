"""HTTP seams for manual storage-class change Jobs (issue #110).

Seams under test:
1. Single-file class-change request targets the intended Vault File /
   Archive Version and enqueues one Job with the chosen class.
3. Vault class-change scope is owner-only; operator receives 403.
7. Objects already at the target class are refused at single-file scope.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.catalog import ArchiveCatalog
from app.config import settings
from app.database import SQLiteConnection
from app.security import hash_password
from app.sessions import create_session, csrf_token_for
from app import storage as storage_module
from tests.test_database import run_alembic


class StorageClassChangeHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        storage_module.cancelled_jobs.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(storage_module.cancelled_jobs.clear)
        self.path = Path(self.tmp.name) / "app.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            self.admin_id = self._user(connection, "admin", True)
            self.owner_id = self._user(connection, "owner", False)
            self.operator_id = self._user(connection, "operator", False)
            self.viewer_id = self._user(connection, "viewer", False)
            self.vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('docs', 'Docs Archive', '/source', 'bucket', 'docs', 'remote')
                RETURNING id
                """
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (%s, %s, 'owner')",
                (self.vault_id, self.owner_id),
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (%s, %s, 'operator')",
                (self.vault_id, self.operator_id),
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (%s, %s, 'viewer')",
                (self.vault_id, self.viewer_id),
            )
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=self.vault_id,
                path="report.txt",
                file_type="regular",
                size=12,
                mtime_ns=1,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            self.version_id = catalog.record_archive_version(
                vault_id=self.vault_id,
                path="report.txt",
                object_key="docs/report.txt",
                provider_version_id="s3-v1",
                size=12,
                storage_class="STANDARD",
                etag="etag",
                uploaded_at="2026-07-21T10:00:00+00:00",
                observed_at="2026-07-21T10:00:00+00:00",
                scan_id="scan",
                origin="upload",
            )
            catalog.mark_version_verified(
                self.version_id,
                plaintext_sha256="a" * 64,
                verified_at="2026-07-21T10:01:00+00:00",
            )
        test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.path),
            cookie_secure=False,
        )
        self.patchers = [
            patch("app.main.settings", test_settings),
            patch("app.database.settings", test_settings),
            patch("app.sessions.settings", test_settings),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app, client=("127.0.0.1", 50000))

    @staticmethod
    def _user(connection, name: str, is_admin: bool) -> int:
        return connection.execute(
            """
            INSERT INTO users(username, display_name, password_hash, is_admin)
            VALUES (%s, %s, %s, %s) RETURNING id
            """,
            (name, name.title(), hash_password("a secure test password"), is_admin),
        ).fetchone()["id"]

    def _authenticate(self, user_id: int, *, reauth: bool = True) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            token = create_session(connection, user_id=user_id, auth_method="oidc")
            csrf = csrf_token_for(connection, token)
            if reauth:
                connection.execute(
                    "UPDATE sessions SET reauth_at=%s WHERE user_id=%s",
                    (datetime.now(timezone.utc).isoformat(), user_id),
                )
        self.client.cookies.set(settings.session_cookie_name, token)
        self.client.cookies.set(settings.csrf_cookie_name, csrf)

    def _headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.client.cookies.get(settings.csrf_cookie_name) or ""}

    def _select_vault(self) -> None:
        response = self.client.post(
            "/api/vaults/select",
            json={"vault_id": self.vault_id},
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_operator_enqueues_single_file_storage_class_job(self) -> None:
        self._authenticate(self.operator_id)
        self._select_vault()
        response = self.client.post(
            "/api/storage-class",
            json={
                "path": "report.txt",
                "is_directory": False,
                "target_storage_class": "DEEP_ARCHIVE",
                "archive_version_id": self.version_id,
            },
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(payload["item_count"], 1)
        self.assertEqual(len(payload["job_ids"]), 1)
        self.assertEqual(payload["target_storage_class"], "DEEP_ARCHIVE")
        self.assertEqual(payload["archive_version_id"], self.version_id)
        with SQLiteConnection(str(self.path)) as connection:
            job = connection.execute(
                "SELECT action, path, archive_version_id, target_storage_class, status FROM jobs WHERE id=%s",
                (payload["job_ids"][0],),
            ).fetchone()
        self.assertEqual(job["action"], "storage-class")
        self.assertEqual(job["path"], "report.txt")
        self.assertEqual(job["archive_version_id"], self.version_id)
        self.assertEqual(job["target_storage_class"], "DEEP_ARCHIVE")
        self.assertEqual(job["status"], "queued")

    def test_storage_class_options_include_rates_and_retrieval_traits(self) -> None:
        self._authenticate(self.operator_id)
        self._select_vault()
        response = self.client.get("/api/storage-classes", headers=self._headers())
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        items = {item["id"]: item for item in payload["items"]}
        self.assertIn("STANDARD", items)
        self.assertIn("DEEP_ARCHIVE", items)
        standard = items["STANDARD"]
        deep = items["DEEP_ARCHIVE"]
        self.assertEqual(standard["currency"], "EUR")
        self.assertEqual(standard["storage_rate_eur_per_gib_month"], 0.023)
        self.assertEqual(standard["retrieval"], "instant")
        self.assertEqual(standard["min_duration_days"], 0)
        self.assertFalse(standard["requires_restore"])
        self.assertEqual(deep["storage_rate_eur_per_gib_month"], 0.00099)
        self.assertEqual(deep["retrieval"], "restore")
        self.assertEqual(deep["min_duration_days"], 180)
        self.assertTrue(deep["requires_restore"])
        self.assertEqual(deep["restore_hours_bulk"], 48.0)
        self.assertEqual(deep["restore_rate_eur_per_gib_bulk"], 0.0025)
        self.assertIn("disclaimer", payload["assumptions"])

    def test_deep_archive_warm_enqueue_attaches_restore_estimate(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                """
                UPDATE archive_versions
                SET storage_class='DEEP_ARCHIVE', restore_state='not_requested', size=%s
                WHERE id=%s
                """,
                (1024**3, self.version_id),  # 1 GiB
            )
        self._authenticate(self.operator_id)
        self._select_vault()
        response = self.client.post(
            "/api/storage-class",
            json={
                "path": "report.txt",
                "is_directory": False,
                "target_storage_class": "STANDARD",
                "archive_version_id": self.version_id,
            },
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(payload["target_storage_class"], "STANDARD")
        self.assertTrue(payload["requires_restore"])
        self.assertEqual(payload["restore_tier"], "Bulk")
        self.assertEqual(payload["estimated_hours"], 48.0)
        self.assertAlmostEqual(payload["estimated_cost_eur"], 0.0025, places=6)
        with SQLiteConnection(str(self.path)) as connection:
            job = connection.execute(
                """
                SELECT restore_tier, restore_days, estimated_cost_eur, estimated_hours,
                       target_storage_class
                FROM jobs WHERE id=%s
                """,
                (payload["job_ids"][0],),
            ).fetchone()
        self.assertEqual(job["target_storage_class"], "STANDARD")
        self.assertEqual(job["restore_tier"], "Bulk")
        self.assertAlmostEqual(float(job["estimated_cost_eur"]), 0.0025, places=6)
        self.assertEqual(float(job["estimated_hours"]), 48.0)

    def test_single_file_noop_target_class_is_rejected(self) -> None:
        self._authenticate(self.operator_id)
        self._select_vault()
        response = self.client.post(
            "/api/storage-class",
            json={
                "path": "report.txt",
                "is_directory": False,
                "target_storage_class": "STANDARD",
            },
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_vault_scope_requires_owner_operator_forbidden(self) -> None:
        self._authenticate(self.operator_id)
        self._select_vault()
        response = self.client.post(
            "/api/storage-class",
            json={
                "path": "",
                "whole_vault": True,
                "target_storage_class": "GLACIER",
            },
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 403, response.text)


if __name__ == "__main__":
    unittest.main()
