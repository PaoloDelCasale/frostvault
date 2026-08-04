"""HTTP seams for operation policies and cost price books (issue #12)."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.database import SQLiteConnection
from app.security import hash_password
from app.sessions import create_session, csrf_token_for
from tests.test_database import run_alembic


class OperationPolicyHttpTests(unittest.TestCase):
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
            "INSERT INTO users(username, display_name, password_hash, is_admin) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
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

    def test_owner_reads_default_manual_policy(self) -> None:
        self._authenticate(self.owner_id)
        response = self.client.get("/api/vault/operation-policy")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["auto_upload"])
        self.assertEqual(body["stability_seconds"], 300)
        self.assertEqual(body["include_globs"], [])
        self.assertEqual(body["exclude_globs"], [])

    def test_operator_cannot_change_operation_policy(self) -> None:
        self._authenticate(self.operator_id)
        response = self.client.put(
            "/api/vault/operation-policy",
            headers=self._headers(),
            json={
                "auto_upload": True,
                "stability_seconds": 300,
                "include_globs": [],
                "exclude_globs": [],
                "bandwidth_limit_kibps": None,
                "operating_windows": [],
            },
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_owner_can_update_policy_and_preview_globs(self) -> None:
        self._authenticate(self.owner_id)
        update = self.client.put(
            "/api/vault/operation-policy",
            headers=self._headers(),
            json={
                "auto_upload": True,
                "auto_local_cleanup": True,
                "local_retention_days": 30,
                "stability_seconds": 600,
                "include_globs": ["**/*.txt"],
                "exclude_globs": ["tmp/**"],
                "bandwidth_limit_kibps": 256,
                "operating_windows": [
                    {"weekday": 0, "start": "09:00", "end": "17:00"}
                ],
            },
        )
        self.assertEqual(update.status_code, 200, update.text)
        self.assertTrue(update.json()["auto_upload"])
        self.assertTrue(update.json()["auto_local_cleanup"])
        self.assertEqual(update.json()["local_retention_days"], 30)
        preview = self.client.post(
            "/api/vault/operation-policy/preview-globs",
            headers=self._headers(),
            json={
                "paths": ["a.txt", "tmp/x.txt", "photo.jpg"],
                "include_globs": ["**/*.txt"],
                "exclude_globs": ["tmp/**"],
            },
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["included"], ["a.txt"])
        self.assertEqual(preview.json()["excluded"], ["tmp/x.txt", "photo.jpg"])

    def test_non_admin_cannot_request_storage_cost_estimates(self) -> None:
        self._authenticate(self.owner_id)
        response = self.client.post(
            "/api/admin/cost-estimates/storage",
            headers=self._headers(),
            json={"size_bytes": 1024, "storage_class": "STANDARD"},
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_admin_can_manage_price_books(self) -> None:
        self._authenticate(self.admin_id)
        created = self.client.post(
            "/api/admin/cost-price-books",
            headers=self._headers(),
            json={
                "name": "eu-2026-07",
                "currency": "EUR",
                "effective_at": "2026-07-01T00:00:00+00:00",
                "assumptions": {"disclaimer": "Internal estimate."},
                "storage_rates": {"STANDARD": 0.02},
                "restore_rates": {"GLACIER": {"Bulk": 0.0025}},
                "reason": "Publish July pricing",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        book_id = created.json()["id"]
        activated = self.client.post(
            f"/api/admin/cost-price-books/{book_id}/activate",
            headers=self._headers(),
            json={"reason": "Activate July pricing"},
        )
        self.assertEqual(activated.status_code, 200, activated.text)
        active = self.client.get("/api/admin/cost-price-books/active")
        self.assertEqual(active.status_code, 200, active.text)
        self.assertEqual(active.json()["id"], book_id)
        estimate = self.client.post(
            "/api/admin/cost-estimates/storage",
            headers=self._headers(),
            json={"size_bytes": 1073741824, "storage_class": "STANDARD"},
        )
        self.assertEqual(estimate.status_code, 200, estimate.text)
        body = estimate.json()
        self.assertEqual(body["estimated_cost_eur"], 0.02)
        self.assertEqual(body["price_book_id"], book_id)
        self.assertEqual(body["price_book_name"], "eu-2026-07")
        self.assertEqual(body["pricing_effective_at"], "2026-07-01T00:00:00+00:00")
        self.assertIn("assumptions", body)


if __name__ == "__main__":
    unittest.main()
