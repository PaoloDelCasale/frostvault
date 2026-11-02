"""HTTP authorization boundaries for audit event visibility (issue #16).

Seams under test (single owner, #412):
- ``GET /api/audit-events`` (events of the selected Vault)
- ``GET /api/admin/audit-events`` (every event)
"""
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
from app.services import audit_events
from app.sessions import create_session, csrf_token_for
from tests.test_database import run_alembic


class AuditVisibilityHttpTests(unittest.TestCase):
    PASSWORD = "correct-horse-battery"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with SQLiteConnection(str(self.database_path)) as connection:
            self.owner_id = self._create_user(connection, "owner")
            self.vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                RETURNING id
                """
            ).fetchone()["id"]
            audit_events.record_audit_event(
                connection,
                event="lifecycle_policy_changed",
                actor_user_id=self.owner_id,
                vault_id=self.vault_id,
                outcome="success",
                visibility="vault",
            )
            audit_events.record_audit_event(
                connection,
                event="vault_root_relocated",
                actor_user_id=self.owner_id,
                vault_id=self.vault_id,
                outcome="success",
                visibility="owner",
                source_root="/sources/private-path",
            )
            audit_events.record_audit_event(
                connection,
                event="break_glass_failed",
                outcome="failure",
                visibility="admin",
                ip="203.0.113.10",
            )

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(
            app=main.app, client=("127.0.0.1", 50000), follow_redirects=False
        )

    def _create_user(self, connection, username: str) -> int:
        return connection.execute(
            """
            INSERT INTO users(username, display_name, password_hash)
            VALUES (%s, %s, %s) RETURNING id
            """,
            (username, username.title(), hash_password(self.PASSWORD)),
        ).fetchone()["id"]

    def _authenticate(self, user_id: int) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            raw_token = create_session(connection, user_id=user_id, auth_method="oidc")
            csrf_token = csrf_token_for(connection, raw_token)
        self.client.cookies.set(self.test_settings.session_cookie_name, raw_token)
        self.client.cookies.set("frostvault_csrf", csrf_token)

    def test_owner_sees_vault_and_owner_events_of_the_selected_vault(self) -> None:
        self._authenticate(self.owner_id)
        response = self.client.get("/api/audit-events")
        self.assertEqual(response.status_code, 200, response.text)
        names = {item["event"] for item in response.json()["events"]}
        self.assertIn("lifecycle_policy_changed", names)
        self.assertIn("vault_root_relocated", names)
        self.assertNotIn("break_glass_failed", names)

    def test_owner_lists_every_audit_event(self) -> None:
        self._authenticate(self.owner_id)
        response = self.client.get("/api/admin/audit-events")
        self.assertEqual(response.status_code, 200, response.text)
        names = {item["event"] for item in response.json()["events"]}
        self.assertIn("lifecycle_policy_changed", names)
        self.assertIn("vault_root_relocated", names)
        self.assertIn("break_glass_failed", names)

    def test_audit_events_require_authentication(self) -> None:
        for path in ("/api/audit-events", "/api/admin/audit-events"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

if __name__ == "__main__":
    unittest.main()
