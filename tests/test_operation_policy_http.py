"""HTTP seams for operation policies and cost estimates (#12, #413)."""

from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.config import settings

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
        with PostgresConnection(str(self.path)) as connection:
            self.owner_id = self._user(connection, "owner")
            self.vault_id = connection.execute(
                "INSERT INTO vaults(slug, name, source_root, s3_bucket, s3_prefix) VALUES ('docs', 'Docs', '/source', 'bucket', 'docs') RETURNING id"
            ).fetchone()["id"]
        test_settings = replace(
            settings,
            database_dsn=database_dsn(str(self.path)),
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
    def _user(connection, name: str) -> int:
        return connection.execute(
            "INSERT INTO users(username, display_name, password_hash) "
            "VALUES (%s, %s, %s) RETURNING id",
            (name, name.title(), hash_password("a secure test password")),
        ).fetchone()["id"]

    def _authenticate(self, user_id: int) -> None:
        with PostgresConnection(str(self.path)) as connection:
            token = create_session(connection, user_id=user_id, auth_method="oidc")
            csrf = csrf_token_for(connection, token)
        self.client.cookies.set(settings.session_cookie_name, token)
        self.client.cookies.set(settings.csrf_cookie_name, csrf)

    def _headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.client.cookies.get(settings.csrf_cookie_name) or ""}

    def test_owner_reads_default_automatic_policy(self) -> None:
        self._authenticate(self.owner_id)
        response = self.client.get("/api/vault/operation-policy")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["auto_upload"])
        self.assertEqual(body["stability_seconds"], 300)
        self.assertEqual(body["include_globs"], [])
        self.assertEqual(body["exclude_globs"], [])

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
            },
        )
        self.assertEqual(update.status_code, 200, update.text)
        self.assertNotIn("bandwidth_limit_kibps", update.json())
        self.assertNotIn("operating_windows", update.json())
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

    def test_owner_can_request_storage_cost_estimates(self) -> None:
        self._authenticate(self.owner_id)
        response = self.client.post(
            "/api/admin/cost-estimates/storage",
            headers=self._headers(),
            json={"size_bytes": 1024, "storage_class": "STANDARD"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["size_bytes"], 1024)

    def test_policy_rejects_removed_bandwidth_and_window_fields(self) -> None:
        self._authenticate(self.owner_id)
        for field, value in (
            ("bandwidth_limit_kibps", 256),
            ("operating_windows", [{"weekday": 0, "start": "09:00", "end": "17:00"}]),
        ):
            with self.subTest(field=field):
                response = self.client.put(
                    "/api/vault/operation-policy",
                    headers=self._headers(),
                    json={"auto_upload": True, field: value},
                )
                self.assertEqual(response.status_code, 422, response.text)

    def _archive_version(self, connection, path: str, size: int | None) -> None:
        file_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO vault_files(id, vault_id, status, created_at) "
            "VALUES (%s, %s, 'active', '2026-07-01T00:00:00+00:00')",
            (file_id, self.vault_id),
        )
        connection.execute(
            """
            INSERT INTO archive_versions(
                id, vault_file_id, vault_id, version_number, object_key,
                provider_version_id, size, storage_class, etag, discovered_at,
                origin, integrity, availability
            ) VALUES (
                %s, %s, %s, 1, %s, %s, %s, 'STANDARD', 'etag',
                '2026-07-01T00:00:00+00:00', 'upload', 'verified', 'available'
            )
            """,
            (str(uuid.uuid4()), file_id, self.vault_id, f"docs/{path}", path, size),
        )

    def test_vault_estimate_uses_bundled_list_prices_and_cloud_size(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            self._archive_version(connection, "a.bin", 1024**3)
            self._archive_version(connection, "b.bin", 1024**3)
        self._authenticate(self.owner_id)
        regional = replace(main.settings, aws_region="eu-south-1")
        with patch("app.main.settings", regional):
            estimate = self.client.post(
                "/api/admin/cost-estimates/storage",
                headers=self._headers(),
                json={"vault_id": self.vault_id, "storage_class": "STANDARD"},
            )
        self.assertEqual(estimate.status_code, 200, estimate.text)
        body = estimate.json()
        self.assertEqual(body["size_bytes"], 2 * 1024**3)
        self.assertEqual(body["region"], "eu-south-1")
        self.assertEqual(body["estimated_cost_eur"], 0.048)
        self.assertNotIn("price_book_id", body)
        self.assertIn("disclaimer", body["assumptions"])

    def test_vault_estimate_refuses_unknown_sizes_and_missing_vaults(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            self._archive_version(connection, "unknown.bin", None)
        self._authenticate(self.owner_id)
        unknown = self.client.post(
            "/api/admin/cost-estimates/storage",
            headers=self._headers(),
            json={"vault_id": self.vault_id, "storage_class": "STANDARD"},
        )
        self.assertEqual(unknown.status_code, 409, unknown.text)
        missing = self.client.post(
            "/api/admin/cost-estimates/storage",
            headers=self._headers(),
            json={"vault_id": 999, "storage_class": "STANDARD"},
        )
        self.assertEqual(missing.status_code, 404, missing.text)
        ambiguous = self.client.post(
            "/api/admin/cost-estimates/storage",
            headers=self._headers(),
            json={"vault_id": self.vault_id, "size_bytes": 1, "storage_class": "STANDARD"},
        )
        self.assertEqual(ambiguous.status_code, 422, ambiguous.text)

    def test_quota_and_price_book_endpoints_are_gone(self) -> None:
        self._authenticate(self.owner_id)
        removed = (
            ("GET", "/api/vault/quotas"),
            ("GET", f"/api/admin/vaults/{self.vault_id}/quotas"),
            ("PUT", f"/api/admin/vaults/{self.vault_id}/quotas"),
            ("GET", "/api/admin/cost-price-books"),
            ("POST", "/api/admin/cost-price-books"),
            ("GET", "/api/admin/cost-price-books/active"),
            ("POST", "/api/admin/cost-price-books/1/activate"),
        )
        for method, path in removed:
            with self.subTest(method=method, path=path):
                response = self.client.request(
                    method, path, headers=self._headers(), json={}
                )
                self.assertEqual(response.status_code, 404, response.text)
        paths = main.app.openapi()["paths"]
        self.assertFalse(
            [path for path in paths if "quota" in path or "price-book" in path]
        )


if __name__ == "__main__":
    unittest.main()
