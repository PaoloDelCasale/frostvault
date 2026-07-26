"""Push subscription HTTP seams (issue #72, seams 4 and 7)."""

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
from app.sessions import create_session, csrf_token_for
from tests.test_database import run_alembic


class PushSubscriptionHttpTests(unittest.TestCase):
    PASSWORD = "correct horse battery staple"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        self.dist_dir = Path(self._tmp.name) / "dist"
        self.dist_dir.mkdir()
        (self.dist_dir / "index.html").write_text(
            "<!doctype html><html><body>spa</body></html>\n", encoding="utf-8"
        )

        with SQLiteConnection(str(self.database_path)) as connection:
            self.user_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('owner', 'Owner', %s, FALSE) RETURNING id
                """,
                (hash_password(self.PASSWORD),),
            ).fetchone()["id"]
            self.vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('docs', 'Docs', '/src', 'bucket', 'docs', 'remote')
                RETURNING id
                """
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) "
                "VALUES (%s, %s, 'owner')",
                (self.vault_id, self.user_id),
            )

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            frontend_dist_dir=str(self.dist_dir),
            cookie_secure=False,
            allowed_hosts="",
            trusted_proxies="",
            oidc_enabled=False,
            vapid_public_key="BP-test-public-key-not-a-placeholder",
            vapid_private_key="test-private-key-not-a-placeholder",
            vapid_subject="mailto:ops@example.com",
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
            "app.config.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(app, client=("127.0.0.1", 50000))
        with SQLiteConnection(str(self.database_path)) as connection:
            token = create_session(
                connection, user_id=self.user_id, auth_method="local"
            )
            csrf = csrf_token_for(connection, token)
            self.session_id = connection.execute(
                "SELECT id FROM sessions WHERE user_id=%s ORDER BY created_at DESC LIMIT 1",
                (self.user_id,),
            ).fetchone()["id"]
        self.client.cookies.set(settings.session_cookie_name, token)
        self.client.cookies.set(settings.csrf_cookie_name, csrf or "")
        self.csrf = csrf or ""

    def test_post_push_subscription_persists_for_current_user_and_device(self) -> None:
        response = self.client.post(
            "/api/push/subscriptions",
            headers={"X-CSRF-Token": self.csrf},
            json={
                "endpoint": "https://push.example/device-1",
                "keys": {"p256dh": "p256dh-key", "auth": "auth-key"},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["endpoint"], "https://push.example/device-1")
        self.assertEqual(body["user_id"], self.user_id)

        with SQLiteConnection(str(self.database_path)) as connection:
            row = connection.execute(
                "SELECT user_id, session_id, endpoint, p256dh, auth "
                "FROM push_subscriptions WHERE endpoint=%s",
                ("https://push.example/device-1",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], self.user_id)
        self.assertEqual(row["session_id"], self.session_id)
        self.assertEqual(row["p256dh"], "p256dh-key")
        self.assertEqual(row["auth"], "auth-key")

    def test_push_config_reports_unconfigured_without_errors(self) -> None:
        unconfigured = replace(
            self.test_settings,
            vapid_public_key="",
            vapid_private_key="",
        )
        with patch("app.main.settings", unconfigured), patch(
            "app.config.settings", unconfigured
        ):
            response = self.client.get("/api/push/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"configured": False, "vapid_public_key": None},
        )


if __name__ == "__main__":
    unittest.main()
