"""GET /api/me vault capability seams (issue #59).

Exposes the archive capability flags that today live only as Jinja
``data-*`` attributes on ``index.html``, so the SPA can read them without
calling the expensive ``/api/stats`` endpoint.
"""

from __future__ import annotations

import re
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


class MeCapabilitiesHttpTests(unittest.TestCase):
    PASSWORD = "correct horse battery staple"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with SQLiteConnection(str(self.database_path)) as connection:
            self.user_ids = {
                username: self._create_user(connection, username)
                for username in ("owner", "operator", "viewer", "orphan")
            }
            self.vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('docs', 'Docs Archive', '/source', 'bucket', 'docs', 'remote')
                RETURNING id
                """
            ).fetchone()["id"]
            for username, role in (
                ("owner", "owner"),
                ("operator", "operator"),
                ("viewer", "viewer"),
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

    def _create_user(self, connection: SQLiteConnection, username: str) -> int:
        return connection.execute(
            """
            INSERT INTO users(username, display_name, password_hash, is_admin)
            VALUES (%s, %s, %s, FALSE) RETURNING id
            """,
            (username, username.title(), hash_password(self.PASSWORD)),
        ).fetchone()["id"]

    def _authenticate(self, username: str) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            raw_token = create_session(
                connection, user_id=self.user_ids[username], auth_method="oidc"
            )
        self.client.cookies.set(self.test_settings.session_cookie_name, raw_token)

    def _me(self, username: str) -> dict:
        self._authenticate(username)
        response = self.client.get("/api/me")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_owner_me_reports_vault_owner_capabilities(self) -> None:
        """Seam 1: owner → is_vault_owner true, can_operate true."""
        payload = self._me("owner")

        vault = payload["vault"]
        self.assertEqual(vault["id"], self.vault_id)
        self.assertEqual(vault["slug"], "docs")
        self.assertEqual(vault["name"], "Docs Archive")
        self.assertEqual(vault["role"], "owner")
        self.assertTrue(vault["is_vault_owner"])
        self.assertTrue(vault["can_operate"])

    def test_operator_me_can_operate_but_is_not_vault_owner(self) -> None:
        """Seam 2: operator → can_operate true, is_vault_owner false."""
        payload = self._me("operator")

        vault = payload["vault"]
        self.assertEqual(vault["role"], "operator")
        self.assertTrue(vault["can_operate"])
        self.assertFalse(vault["is_vault_owner"])

    def test_viewer_me_cannot_operate_and_is_not_vault_owner(self) -> None:
        """Seam 3: viewer → can_operate false, is_vault_owner false."""
        payload = self._me("viewer")

        vault = payload["vault"]
        self.assertEqual(vault["role"], "viewer")
        self.assertFalse(vault["can_operate"])
        self.assertFalse(vault["is_vault_owner"])

    def test_delete_enabled_false_for_owner_when_local_delete_disabled(self) -> None:
        """Seam 4: delete_enabled false when allow_local_delete is off, even for owner."""
        with patch(
            "app.main.settings",
            replace(self.test_settings, allow_local_delete=False),
        ):
            payload = self._me("owner")

        self.assertFalse(payload["vault"]["delete_enabled"])

    def test_cloud_deletion_enabled_requires_vault_flag_and_owner_role(self) -> None:
        """Seam 5: cloud_deletion_enabled reflects vault setting AND owner role."""
        # Default vault flag is off → owner still sees false.
        owner_off = self._me("owner")
        self.assertFalse(owner_off["vault"]["cloud_deletion_enabled"])

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE vaults SET cloud_deletion_enabled=TRUE WHERE id=%s",
                (self.vault_id,),
            )

        owner_on = self._me("owner")
        self.assertTrue(owner_on["vault"]["cloud_deletion_enabled"])

        # Non-owners never get the capability even when the vault flag is on.
        operator = self._me("operator")
        self.assertFalse(operator["vault"]["cloud_deletion_enabled"])
        viewer = self._me("viewer")
        self.assertFalse(viewer["vault"]["cloud_deletion_enabled"])
