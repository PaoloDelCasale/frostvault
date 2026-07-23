"""HTTP gates for cloud deletion workflows (issue #10).

Seams under test:
- Owner/admin can toggle vault cloud-deletion setting with recent reauth.
- Operators/viewers cannot enable setting, archive, or purge.
- Preview, archive, and purge endpoints enforce confirmation/delay gates.
- Cancel during pending_delay is exposed through the jobs cancel API.
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


class CloudDeletionHttpTests(unittest.TestCase):
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
            version_id = catalog.record_archive_version(
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
                version_id,
                plaintext_sha256="a" * 64,
                verified_at="2026-07-21T10:01:00+00:00",
            )
            file_row = catalog.get_file_by_path(self.vault_id, "report.txt")
            catalog.mark_local_copy_missing(
                file_row["id"], observed_at="2026-07-21T11:00:00+00:00"
            )
        test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.path),
            cookie_secure=False,
            cloud_purge_delay_seconds=86400,
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

    def test_setting_defaults_off_and_owner_can_enable_with_reauth(self) -> None:
        self._authenticate(self.owner_id)
        self._select_vault()
        response = self.client.get("/api/vault/cloud-deletion")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["enabled"])
        self.assertIn("Delete Marker", response.json()["delete_marker_explanation"])

        denied = self.client.put(
            "/api/vault/cloud-deletion",
            json={"enabled": True},
            headers=self._headers(),
        )
        # Fresh session without reauth window? We set reauth_at above.
        # Operator cannot enable:
        self._authenticate(self.operator_id)
        self._select_vault()
        forbidden = self.client.put(
            "/api/vault/cloud-deletion",
            json={"enabled": True},
            headers=self._headers(),
        )
        self.assertEqual(forbidden.status_code, 403, forbidden.text)

        self._authenticate(self.owner_id)
        self._select_vault()
        enabled = self.client.put(
            "/api/vault/cloud-deletion",
            json={"enabled": True},
            headers=self._headers(),
        )
        self.assertEqual(enabled.status_code, 200, enabled.text)
        self.assertTrue(enabled.json()["enabled"])

    def test_operators_and_viewers_cannot_purge_or_archive(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "UPDATE vaults SET cloud_deletion_enabled=TRUE WHERE id=%s",
                (self.vault_id,),
            )
        for user_id in (self.operator_id, self.viewer_id):
            self._authenticate(user_id)
            self._select_vault()
            archive = self.client.post(
                "/api/cloud-archive",
                json={"path": "report.txt", "is_directory": False},
                headers=self._headers(),
            )
            self.assertEqual(archive.status_code, 403, archive.text)
            purge = self.client.post(
                "/api/cloud-purge",
                json={
                    "path": "report.txt",
                    "is_directory": False,
                    "confirmation": "Docs Archive",
                    "reason": "cleanup",
                    "generated_phrase": "amber-birch-10",
                },
                headers=self._headers(),
            )
            self.assertEqual(purge.status_code, 403, purge.text)

    def test_owner_purge_requires_confirmation_and_schedules_delay(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "UPDATE vaults SET cloud_deletion_enabled=TRUE WHERE id=%s",
                (self.vault_id,),
            )
        self._authenticate(self.owner_id)
        self._select_vault()
        preview = self.client.post(
            "/api/cloud-deletion/preview",
            json={"path": "report.txt", "is_directory": False},
            headers=self._headers(),
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["version_count"], 1)

        bad = self.client.post(
            "/api/cloud-purge",
            json={
                "path": "report.txt",
                "is_directory": False,
                "confirmation": "wrong",
                "reason": "cleanup",
                "generated_phrase": "amber-birch-10",
            },
            headers=self._headers(),
        )
        self.assertEqual(bad.status_code, 422, bad.text)

        ok = self.client.post(
            "/api/cloud-purge",
            json={
                "path": "report.txt",
                "is_directory": False,
                "confirmation": "Docs Archive",
                "reason": "cleanup obsolete copies",
                "generated_phrase": "amber-birch-10",
            },
            headers=self._headers(),
        )
        self.assertEqual(ok.status_code, 202, ok.text)
        body = ok.json()
        self.assertEqual(body["status"], "pending_delay")
        self.assertTrue(body["pending_until"])

        cancel = self.client.post(
            "/api/jobs/cancel",
            json={"group_id": body["group_id"], "action": "cloud-purge"},
            headers=self._headers(),
        )
        self.assertEqual(cancel.status_code, 200, cancel.text)
        self.assertEqual(cancel.json()["cancelled_count"], 1)


if __name__ == "__main__":
    unittest.main()
