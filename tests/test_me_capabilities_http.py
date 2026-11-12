"""GET /api/me vault capability seams (issue #59).

Exposes the archive capability flags that the SPA reads from /api/me
(delete_enabled, cloud_deletion_enabled) so the client can gate actions
without calling the expensive ``/api/stats`` endpoint. The single owner
(#412) has no Vault role: every Vault is theirs.
"""

from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings

from app.main import app
from app.security import hash_password
from app.sessions import create_session
from tests.spa_fixture import write_spa_dist
from tests.test_database import run_alembic


class MeCapabilitiesHttpTests(unittest.TestCase):
    PASSWORD = "correct horse battery staple"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.dist_dir = write_spa_dist(Path(self._tmp.name))

        with PostgresConnection(str(self.database_path)) as connection:
            self.owner_id = connection.execute(
                """
                INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', %s) RETURNING id
                """,
                (hash_password(self.PASSWORD),),
            ).fetchone()["id"]

        self.test_settings = replace(
            settings,
            database_dsn=database_dsn(str(self.database_path)),
            cookie_secure=False,
            bootstrap_admin_username="owner",
            frontend_dist_dir=str(self.dist_dir),
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

    def _create_vault(self) -> int:
        with PostgresConnection(str(self.database_path)) as connection:
            return connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix) VALUES ('docs', 'Docs Archive', '/source', 'bucket', 'docs')
                RETURNING id
                """
            ).fetchone()["id"]

    def _me(self) -> dict:
        with PostgresConnection(str(self.database_path)) as connection:
            raw_token = create_session(
                connection, user_id=self.owner_id, auth_method="oidc"
            )
        self.client.cookies.set(self.test_settings.session_cookie_name, raw_token)
        response = self.client.get("/api/me")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_owner_me_reports_the_selected_vault(self) -> None:
        """Seam 1: the owner sees its Vault without any role field."""
        vault_id = self._create_vault()
        vault = self._me()["vault"]

        self.assertEqual(vault["id"], vault_id)
        self.assertEqual(vault["slug"], "docs")
        self.assertEqual(vault["name"], "Docs Archive")
        self.assertTrue(vault["delete_enabled"])
        for removed in ("role", "can_operate", "is_vault_owner"):
            self.assertNotIn(removed, vault)

    def test_delete_enabled_false_when_source_is_unavailable(self) -> None:
        """Manual release is available with automation off, never on an absent source."""
        from types import SimpleNamespace
        with patch(
            "app.main.vault_local_access",
            return_value=SimpleNamespace(local_operations_allowed=False),
        ):
            self._create_vault()
            payload = self._me()

        self.assertFalse(payload["vault"]["delete_enabled"])

    def test_cloud_deletion_has_no_enablement_setting(self) -> None:
        self._create_vault()
        self.assertTrue(self._me()["vault"]["cloud_deletion_enabled"])

    def test_owner_without_vault_gets_null_vault_block(self) -> None:
        """Seam 6: before the first Vault exists → a null vault block."""
        payload = self._me()

        self.assertIsNone(payload["vault"])
        self.assertEqual(payload["username"], "owner")

    def test_me_reports_the_owner_identity(self) -> None:
        """Seam 7: identity and session fields, without multi-user flags."""
        self._create_vault()
        payload = self._me()

        self.assertEqual(payload["id"], self.owner_id)
        self.assertEqual(payload["username"], "owner")
        self.assertEqual(payload["display_name"], "Owner")
        self.assertNotIn("is_admin", payload)
        self.assertNotIn("active", payload)
        self.assertEqual(payload["session_version"], 1)
        self.assertIsInstance(payload["csrf_token"], str)
        self.assertTrue(payload["csrf_token"])
        self.assertEqual(payload["auth_method"], "oidc")
        self.assertEqual(payload["locale"], "en")
        self.assertEqual(payload["locales"], ["en", "it"])

if __name__ == "__main__":
    unittest.main()
