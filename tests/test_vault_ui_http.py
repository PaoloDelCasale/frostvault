from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.database import SQLiteConnection
from app.main import app
from app.security import hash_password
from app.sessions import create_session
from tests.test_database import run_alembic


class VaultRoleUiHttpTests(unittest.TestCase):
    PASSWORD = "correct horse battery staple"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with SQLiteConnection(str(self.database_path)) as connection:
            self.user_ids = {
                username: self._create_user(
                    connection, username, is_admin=username == "global-admin"
                )
                for username in ("owner", "operator", "viewer", "global-admin")
            }
            self.vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                RETURNING id
                """
            ).fetchone()["id"]
            for username, role in (
                ("owner", "owner"),
                ("operator", "operator"),
                ("viewer", "viewer"),
                # A global administrator still uses the member role for the
                # regular archive UI; admin overrides belong to /api/admin.
                ("global-admin", "operator"),
            ):
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (%s, %s, %s)",
                    (self.vault_id, self.user_ids[username], role),
                )

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
            allow_local_delete=True,
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def _create_user(
        self, connection: SQLiteConnection, username: str, *, is_admin: bool = False
    ) -> int:
        return connection.execute(
            """
            INSERT INTO users(username, display_name, password_hash, is_admin)
            VALUES (%s, %s, %s, %s) RETURNING id
            """,
            (username, username.title(), hash_password(self.PASSWORD), is_admin),
        ).fetchone()["id"]

    def _authenticate(self, username: str) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            raw_token = create_session(
                connection, user_id=self.user_ids[username], auth_method="oidc"
            )
        self.client.cookies.set(self.test_settings.session_cookie_name, raw_token)

    def _archive(self, username: str) -> str:
        self._authenticate(username)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200, response.text)
        return response.text

    def test_owner_gets_operation_controls_and_configuration_gated_cleanup(self) -> None:
        page = self._archive("owner")

        self.assertIn('data-role="owner"', page)
        self.assertIn('data-can-operate="true"', page)
        self.assertIn('data-delete-enabled="true"', page)
        self.assertIn('id="scan-button"', page)
        self.assertIn('id="vault-access-link"', page)

        access_page = self.client.get('/vault/access')
        self.assertEqual(access_page.status_code, 200, access_page.text)
        self.assertIn('/static/vault_access.js', access_page.text)
        self.assertIn('id="user-lookup-form"', access_page.text)
        self.assertIn('id="quota-panel"', access_page.text)
        self.assertIn('id="lifecycle-panel"', access_page.text)
        self.assertIn('id="lifecycle-default-form"', access_page.text)
        self.assertIn('id="owner-quota-state"', access_page.text)
        self.assertNotIn('id="quota-form"', access_page.text)

        # Exercise the same server-side rendering gate with the feature disabled.
        with patch("app.main.settings", replace(self.test_settings, allow_local_delete=False)):
            page = self.client.get("/").text
        self.assertIn('data-delete-enabled="false"', page)

    def test_operator_gets_operations_but_not_owner_only_controls(self) -> None:
        page = self._archive("operator")

        self.assertIn('data-role="operator"', page)
        self.assertIn('data-can-operate="true"', page)
        self.assertIn('data-delete-enabled="false"', page)
        self.assertIn('id="scan-button"', page)
        self.assertNotIn('id="vault-access-link"', page)
        self.assertNotIn('quota-panel', page)
        self.assertEqual(self.client.get('/vault/access').status_code, 403)

    def test_viewer_gets_read_only_rendering_without_mutation_controls(self) -> None:
        page = self._archive("viewer")

        self.assertIn('data-role="viewer"', page)
        self.assertIn('data-can-operate="false"', page)
        self.assertIn('data-delete-enabled="false"', page)
        self.assertNotIn('id="scan-button"', page)
        self.assertNotIn('id="vault-access-link"', page)
        self.assertNotIn('quota-panel', page)
        self.assertEqual(self.client.get('/vault/access').status_code, 403)

    def test_global_admin_uses_their_vault_member_role_in_archive_ui(self) -> None:
        page = self._archive("global-admin")

        self.assertIn('data-role="operator"', page)
        self.assertIn('data-can-operate="true"', page)
        self.assertIn('data-delete-enabled="false"', page)
        self.assertIn('href="/admin"', page)
        self.assertIn('id="scan-button"', page)

    def test_admin_governance_ui_keeps_membership_and_transfer_explicit(self) -> None:
        self._authenticate("global-admin")
        page = self.client.get("/admin")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn('id="member-form"', page.text)
        self.assertIn('id="member-role"', page.text)
        self.assertIn('<option value="operator">Operator</option>', page.text)
        self.assertIn('<option value="viewer">Viewer</option>', page.text)
        self.assertNotIn('<option value="owner">Owner</option>', page.text)
        self.assertIn('id="member-reason"', page.text)
        self.assertIn('name="reason"', page.text)
        self.assertIn('id="transfer-owner-form"', page.text)
        self.assertIn('id="transfer-owner-user"', page.text)
        self.assertIn('id="transfer-owner-confirm"', page.text)
        self.assertIn('id="transfer-owner-reason"', page.text)
        self.assertIn('id="quota-form"', page.text)
        self.assertIn('storage_soft_limit_bytes', page.text)
        self.assertIn('restore_30d_hard_limit_bytes', page.text)

        script = self.client.get("/static/admin.js")
        self.assertEqual(script.status_code, 200, script.text)
        self.assertIn("data.error === 'reauth_required'", script.text)
        self.assertIn("return api(url, options, false)", script.text)
        self.assertIn("/transfer-owner", script.text)
        self.assertIn("members.filter(member => member.active && member.role !== 'owner')", script.text)
        self.assertIn("new_owner_user_id", script.text)
        self.assertIn("body:JSON.stringify({user_id:Number(form.get('user_id')), role:form.get('role'), reason})", script.text)
        self.assertIn("/api/admin/vaults/${selectedVaultId}/quotas", script.text)
        self.assertIn("return api(url, options, false)", script.text)
        self.assertIn("Soft ${label} limit cannot exceed the hard limit.", script.text)


if __name__ == "__main__":
    unittest.main()
