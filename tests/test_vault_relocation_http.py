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


class VaultRelocationHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "app.db"
        result = run_alembic(self.db_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash) "
                "VALUES (1, 'owner', 'Owner', 'hash')"
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

    def client_for(self, *, stale: bool = False) -> tuple[TestClient, dict[str, str]]:
        client = TestClient(app, client=("127.0.0.1", 50000))
        with SQLiteConnection(str(self.db_path)) as connection:
            token = create_session(connection, user_id=1, auth_method="oidc")
            if stale:
                connection.execute(
                    "UPDATE sessions SET reauth_at='2000-01-01T00:00:00+00:00' WHERE user_id=1"
                )
        client.cookies.set(self.settings.session_cookie_name, token)
        csrf = client.get("/api/me").json()["csrf_token"]
        return client, {"X-CSRF-Token": csrf}

    def test_relocation_does_not_require_recent_reauthentication(self) -> None:
        # Only the Recovery Kit download and cloud deletion re-authenticate
        # (#412); an unknown Vault reaches the handler and is Not Found.
        client, headers = self.client_for(stale=True)
        response = client.post(
            "/api/admin/vaults/7/relocate",
            json={"volume_alias": "photos", "relative_path": "new", "reason": "move"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_csrf_is_not_weakened(self) -> None:
        client, _ = self.client_for()
        response = client.post(
            "/api/admin/vaults/7/relocate",
            json={"volume_alias": "photos", "relative_path": "new", "reason": "move"},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
