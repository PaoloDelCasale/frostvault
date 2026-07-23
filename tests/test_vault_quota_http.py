from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.security import hash_password
from app.services.vault_quotas import QuotaLimits, set_limits
from app.sessions import create_session, csrf_token_for
from tests.test_database import run_alembic


class VaultQuotaHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "app.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            self.admin_id = self._user(connection, "admin", True)
            self.owner_id = self._user(connection, "owner", False)
            self.operator_id = self._user(connection, "operator", False)
            self.viewer_id = self._user(connection, "viewer", False)
            self.vault_id = connection.execute(
                "INSERT INTO vaults(slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                "VALUES ('docs', 'Docs', '/source', 'bucket', 'docs', 'remote') RETURNING id"
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
        test_settings = replace(settings, db_backend="sqlite", sqlite_path=str(self.path), cookie_secure=False)
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
            "INSERT INTO users(username, display_name, password_hash, is_admin) VALUES (%s, %s, %s, %s) RETURNING id",
            (name, name.title(), hash_password("a secure test password"), is_admin),
        ).fetchone()["id"]

    def _authenticate(self, user_id: int) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            token = create_session(connection, user_id=user_id, auth_method="oidc")
            csrf = csrf_token_for(connection, token)
        self.client.cookies.set(settings.session_cookie_name, token)
        self.client.cookies.set(settings.csrf_cookie_name, csrf)

    def _headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.client.cookies.get(settings.csrf_cookie_name) or ""}

    def test_quota_api_reports_an_evaluated_allow_with_no_decisions(self) -> None:
        self._authenticate(self.owner_id)
        response = self.client.get("/api/vault/quotas")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["evaluation"], {
            "state": "evaluated",
            "allowed": True,
            "decisions": [],
        })

    def test_quota_api_reports_authoritative_current_warning_reason(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            set_limits(connection, self.vault_id, QuotaLimits(concurrency_soft_limit=0, concurrency_hard_limit=2))
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=self.vault_id, path="pending.txt", file_type="regular",
                size=1, mtime_ns=1, observed_at="2026-01-01T00:00:00+00:00",
            )
            catalog.queue_jobs(
                vault_id=self.vault_id, path="pending.txt", action="upload",
                requested_by=self.owner_id, requested_at="2026-01-01T00:00:00+00:00",
                group_id="pending", is_directory=False,
            )

        self._authenticate(self.owner_id)
        response = self.client.get("/api/vault/quotas")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["evaluation"], {
            "state": "evaluated",
            "allowed": True,
            "decisions": [{
                "code": "quota.concurrency.soft_exceeded",
                "severity": "warning",
                "projected": 1,
                "limit": 0,
            }],
        })

    def test_owner_read_is_separate_from_admin_and_denies_other_roles(self) -> None:
        self._authenticate(self.owner_id)
        owner = self.client.get("/api/vault/quotas")
        self.assertEqual(owner.status_code, 200, owner.text)
        self.assertEqual(owner.json()["limits"]["storage_hard_limit_bytes"], None)

        self._authenticate(self.operator_id)
        self.assertEqual(self.client.get("/api/vault/quotas").status_code, 403)
        self._authenticate(self.viewer_id)
        self.assertEqual(self.client.get("/api/vault/quotas").status_code, 403)

        self._authenticate(self.admin_id)
        admin_read = self.client.get(f"/api/admin/vaults/{self.vault_id}/quotas")
        self.assertEqual(admin_read.status_code, 200, admin_read.text)

    def test_admin_update_requires_reauth_reason_and_notifies_owner(self) -> None:
        self._authenticate(self.admin_id)
        missing_reason = self.client.put(
            f"/api/admin/vaults/{self.vault_id}/quotas",
            json={"concurrency_hard_limit": 1}, headers=self._headers(),
        )
        self.assertEqual(missing_reason.status_code, 422)

        with self.assertLogs("app.audit", level="WARNING") as logs:
            response = self.client.put(
                f"/api/admin/vaults/{self.vault_id}/quotas",
                json={"concurrency_hard_limit": 0, "reason": "protect service capacity"},
                headers=self._headers(),
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["limits"]["concurrency_hard_limit"], 0)
        self.assertIn('"event": "vault_quotas_changed"', logs.records[-1].getMessage())
        self.assertIn(str(self.owner_id), logs.records[-1].getMessage())

        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO vault_files(id, vault_id, status, created_at) VALUES ('file-1', %s, 'active', '2026-01-01')",
                (self.vault_id,),
            )
            connection.execute(
                "INSERT INTO file_paths(vault_file_id, vault_id, path, valid_from) VALUES ('file-1', %s, 'upload.txt', '2026-01-01')",
                (self.vault_id,),
            )
            connection.execute(
                "INSERT INTO local_copies(vault_file_id, presence, file_type, size, mtime_ns, observed_at) VALUES ('file-1', 'present', 'regular', 1, 1, '2026-01-01')"
            )
        self._authenticate(self.operator_id)
        blocked = self.client.post("/api/upload", json={"path": "upload.txt"}, headers=self._headers())
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(blocked.json()["error"], "quota_blocked")
        self.assertEqual(blocked.json()["quota"]["decisions"][0]["code"], "quota.concurrency.hard_exceeded")

    def test_invalid_quota_order_is_rejected(self) -> None:
        self._authenticate(self.admin_id)
        response = self.client.put(
            f"/api/admin/vaults/{self.vault_id}/quotas",
            json={"storage_soft_limit_bytes": 5, "storage_hard_limit_bytes": 4, "reason": "invalid test"},
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
