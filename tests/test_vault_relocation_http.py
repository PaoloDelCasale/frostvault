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
from app.sessions import create_session
from tests.spa_fixture import write_spa_dist
from tests.test_database import run_alembic


class VaultRelocationAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "app.db"
        result = run_alembic(self.db_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'member', 'Member', 'hash', 0), (2, 'admin', 'Admin', 'hash', 1)"
            )
        configured = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.db_path),
            cookie_secure=False,
            frontend_dist_dir=str(write_spa_dist(Path(self.tmp.name))),
        )
        for target in ("app.main.settings", "app.database.settings", "app.sessions.settings"):
            patcher = patch(target, configured)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.settings = configured

    def client_for(self, user_id: int, *, stale: bool = False) -> tuple[TestClient, dict[str, str]]:
        client = TestClient(app, client=("127.0.0.1", 50000))
        with SQLiteConnection(str(self.db_path)) as connection:
            token = create_session(connection, user_id=user_id, auth_method="oidc")
            if stale:
                connection.execute(
                    "UPDATE sessions SET reauth_at='2000-01-01T00:00:00+00:00' WHERE user_id=%s",
                    (user_id,),
                )
        client.cookies.set(self.settings.session_cookie_name, token)
        csrf = client.get("/api/me").json()["csrf_token"]
        return client, {"X-CSRF-Token": csrf}

    def test_owner_operator_or_viewer_cannot_invoke_global_admin_route(self) -> None:
        client, headers = self.client_for(1)
        response = client.post(
            "/api/admin/vaults/7/relocate",
            json={"volume_alias": "photos", "relative_path": "new", "reason": "move"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 403)

    def test_global_admin_requires_recent_reauthentication(self) -> None:
        client, headers = self.client_for(2, stale=True)
        response = client.post(
            "/api/admin/vaults/7/relocate",
            json={"volume_alias": "photos", "relative_path": "new", "reason": "move"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], {"error": "reauth_required"})

    def test_csrf_is_not_weakened(self) -> None:
        client, _ = self.client_for(2)
        response = client.post(
            "/api/admin/vaults/7/relocate",
            json={"volume_alias": "photos", "relative_path": "new", "reason": "move"},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
