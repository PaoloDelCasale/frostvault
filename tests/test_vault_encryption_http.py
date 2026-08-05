"""HTTP coverage for crypt vault creation, custody, and recovery export."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import settings
from app.database import SQLiteConnection
from app.main import app
from app.services import source_layout
from app.sessions import create_session
from tests.spa_fixture import write_spa_dist
from tests.test_database import run_alembic


class VaultEncryptionHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        self.sources_root = Path(self._tmp.name) / "sources"
        self.sources_root.mkdir()
        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        self.managed_root = source_layout.ensure_managed_directory()
        self.rclone_config = Path(self._tmp.name) / "rclone.conf"
        self.rclone_config.write_text(
            "[base]\ntype = local\nnounc = true\n", encoding="utf-8"
        )
        self.master_key = Fernet.generate_key().decode("ascii")

        with SQLiteConnection(str(self.database_path)) as connection:
            self.owner_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('alice', 'Alice', '', FALSE) RETURNING id
                """
            ).fetchone()["id"]
            self.admin_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('admin', 'Admin', '', TRUE) RETURNING id
                """
            ).fetchone()["id"]
            self.operator_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('oper', 'Oper', '', FALSE) RETURNING id
                """
            ).fetchone()["id"]

        self.settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
            vault_s3_bucket="test-bucket",
            vault_rclone_remote="plain-remote",
            vault_rclone_base_remote="base",
            archive_master_key=self.master_key,
            rclone_config=str(self.rclone_config),
            reauth_window_seconds=600,
            frontend_dist_dir=str(write_spa_dist(Path(self._tmp.name))),
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
            "app.services.vaults.settings",
            "app.services.rclone_runtime.settings",
            "app.services.vault_recovery.settings",
            "app.services.vault_crypto.settings",
        ):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def _authenticate(self, user_id: int, *, reauth: bool = False) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            raw_token = create_session(
                connection, user_id=user_id, auth_method="oidc"
            )
            if reauth:
                connection.execute(
                    "UPDATE sessions SET reauth_at=%s WHERE user_id=%s",
                    (datetime.now(timezone.utc).isoformat(), user_id),
                )
        self.client.cookies.set(self.settings.session_cookie_name, raw_token)

    def _csrf(self) -> str:
        return self.client.get("/api/me").json()["csrf_token"]

    def _select(self, vault_id: int) -> None:
        response = self.client.post(
            "/api/vaults/select",
            json={"vault_id": vault_id},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _create_crypt(self) -> dict:
        self._authenticate(self.owner_id)
        response = self.client.post(
            "/api/vaults",
            json={"name": "Secret", "slug": "secret", "encryption_mode": "crypt"},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_admin_vault_responses_use_only_the_public_projection(self) -> None:
        """Issue #189: key-set assertions never render encrypted values."""
        expected_keys = {
            "id",
            "uuid",
            "slug",
            "name",
            "source_root",
            "s3_bucket",
            "s3_prefix",
            "rclone_remote",
            "enabled",
            "encryption_mode",
            "decommission_state",
            "decommissioned_at",
            "root_released_at",
            "member_count",
        }
        self._authenticate(self.admin_id, reauth=True)
        created_response = self.client.post(
            "/api/admin/vaults",
            json={
                "name": "Admin Crypt",
                "slug": "admin-crypt",
                "owner_user_id": self.owner_id,
                "encryption_mode": "crypt",
                "reason": "provision encrypted archive",
            },
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(created_response.status_code, 201)
        created = created_response.json()
        self.assertEqual(set(created), expected_keys)
        self.assertEqual(created["member_count"], 1)

        # An unreviewed future persistence-only column must not cross the
        # administrative HTTP boundary either.
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "ALTER TABLE vaults ADD COLUMN future_secret_ciphertext TEXT"
            )
            connection.execute(
                "UPDATE vaults SET future_secret_ciphertext=%s WHERE id=%s",
                ("opaque", created["id"]),
            )

        listed_response = self.client.get("/api/admin/vaults")
        self.assertEqual(listed_response.status_code, 200)
        listed = listed_response.json()["items"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(set(listed[0]), expected_keys)

    def test_create_page_serves_spa_and_api_accepts_encryption_mode(self) -> None:
        self._authenticate(self.owner_id)
        page = self.client.get("/vaults/new")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="root"', page.text)

        crypt = self.client.post(
            "/api/vaults",
            json={"name": "Secret", "slug": "secret", "encryption_mode": "crypt"},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(crypt.status_code, 201, crypt.text)
        self.assertEqual(crypt.json()["encryption_mode"], "crypt")

        plain = self.client.post(
            "/api/vaults",
            json={"name": "Plain", "slug": "plain", "encryption_mode": "plain"},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(plain.status_code, 201, plain.text)
        self.assertEqual(plain.json()["encryption_mode"], "plain")

    def test_create_crypt_vault_returns_one_time_recovery_material(self) -> None:
        body = self._create_crypt()
        self.assertEqual(body["encryption_mode"], "crypt")
        self.assertIn("recovery_export", body)
        self.assertIn("filename_encryption = standard", body["recovery_export"])
        self.assertFalse(body["recovery_custody_confirmed"])

        with SQLiteConnection(str(self.database_path)) as connection:
            row = connection.execute(
                "SELECT crypt_password_ciphertext FROM vaults WHERE id=%s",
                (body["id"],),
            ).fetchone()
        self.assertNotIn(row["crypt_password_ciphertext"], body["recovery_export"])

    def test_upload_api_blocks_crypt_vault_before_custody_confirmation(self) -> None:
        body = self._create_crypt()
        self._select(body["id"])
        source = self.managed_root / body["uuid"]
        (source / "note.txt").write_text("hi", encoding="utf-8")
        # Catalog the local file through observe via scan tree would be heavy;
        # insert through the catalog API used by the app.
        from app.catalog import ArchiveCatalog

        with SQLiteConnection(str(self.database_path)) as connection:
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=body["id"],
                path="note.txt",
                file_type="regular",
                size=2,
                mtime_ns=1,
                observed_at="2026-01-01T00:00:00+00:00",
            )

        response = self.client.post(
            "/api/upload",
            json={"path": "note.txt", "is_directory": False},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"], "recovery_custody_required")

    def test_owner_can_confirm_custody_and_then_upload(self) -> None:
        body = self._create_crypt()
        self._select(body["id"])
        confirm = self.client.post(
            "/api/vault/recovery/confirm",
            json={"acknowledged": True},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(confirm.status_code, 200, confirm.text)
        self.assertTrue(confirm.json()["recovery_custody_confirmed"])

        from app.catalog import ArchiveCatalog

        with SQLiteConnection(str(self.database_path)) as connection:
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=body["id"],
                path="note.txt",
                file_type="regular",
                size=2,
                mtime_ns=1,
                observed_at="2026-01-01T00:00:00+00:00",
            )
        response = self.client.post(
            "/api/upload",
            json={"path": "note.txt", "is_directory": False},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(response.status_code, 202, response.text)

    def test_reexport_requires_reauth_reason_and_is_audited(self) -> None:
        body = self._create_crypt()
        self._select(body["id"])
        self.client.post(
            "/api/vault/recovery/confirm",
            json={"acknowledged": True},
            headers={"X-CSRF-Token": self._csrf()},
        )
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE sessions SET reauth_at=%s WHERE user_id=%s",
                ("2000-01-01T00:00:00+00:00", self.owner_id),
            )

        denied = self.client.post(
            "/api/vault/recovery/export",
            json={"reason": "lost my copy"},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"], "reauth_required")

        self._authenticate(self.owner_id, reauth=True)
        self._select(body["id"])
        with self.assertLogs("app.audit", level="WARNING") as captured:
            exported = self.client.post(
                "/api/vault/recovery/export",
                json={"reason": "lost my copy"},
                headers={"X-CSRF-Token": self._csrf()},
            )
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertIn("filename_encryption = standard", exported.json()["recovery_export"])
        joined = "\n".join(captured.output)
        self.assertIn("vault_recovery_exported", joined)
        self.assertIn("lost my copy", joined)

    def test_admin_reexport_notifies_owner(self) -> None:
        body = self._create_crypt()
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE vaults SET recovery_custody_confirmed_at=%s WHERE id=%s",
                (datetime.now(timezone.utc).isoformat(), body["id"]),
            )

        self._authenticate(self.admin_id, reauth=True)
        with self.assertLogs("app.audit", level="WARNING") as captured:
            response = self.client.post(
                f"/api/admin/vaults/{body['id']}/recovery/export",
                json={"reason": "support restore drill"},
                headers={"X-CSRF-Token": self._csrf()},
            )
        self.assertEqual(response.status_code, 200, response.text)
        joined = "\n".join(captured.output)
        self.assertIn("vault_recovery_exported", joined)
        self.assertIn(f'"notify_user_id": {self.owner_id}', joined)
        self.assertIn("support restore drill", joined)

    def test_operator_cannot_export_recovery_secret(self) -> None:
        body = self._create_crypt()
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (%s, %s, 'operator')",
                (body["id"], self.operator_id),
            )
            connection.execute(
                "UPDATE vaults SET recovery_custody_confirmed_at=%s WHERE id=%s",
                (datetime.now(timezone.utc).isoformat(), body["id"]),
            )
        self._authenticate(self.operator_id, reauth=True)
        self._select(body["id"])
        response = self.client.post(
            "/api/vault/recovery/export",
            json={"reason": "curious"},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(response.status_code, 403, response.text)


if __name__ == "__main__":
    unittest.main()
