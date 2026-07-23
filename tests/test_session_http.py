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
from tests.test_database import run_alembic


class SessionHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        self.username = "alice"
        self.password = "correct horse battery"
        with SQLiteConnection(str(database_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES (%s, 'Alice', %s, TRUE)
                """,
                (self.username, hash_password(self.password)),
            )

        self.settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(database_path),
            cookie_secure=False,
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
        ):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(app, client=("127.0.0.1", 50000))
        self.cookie_name = self.settings.session_cookie_name

    def _login(self) -> "TestClient":
        response = self.client.post(
            "/api/login",
            json={"username": self.username, "password": self.password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_login_sets_a_hardened_session_cookie(self) -> None:
        response = self._login()

        set_cookie = response.headers["set-cookie"]
        lowered = set_cookie.lower()
        self.assertIn(f"{self.cookie_name}=", set_cookie)
        self.assertIn("httponly", lowered)
        self.assertIn("samesite=lax", lowered)
        self.assertIn("path=/", lowered)
        self.assertNotIn("secure", lowered)

    def test_me_is_reachable_with_the_session_cookie(self) -> None:
        self._login()

        response = self.client.get("/api/me")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["username"], self.username)

    def test_me_requires_a_session_cookie(self) -> None:
        response = self.client.get("/api/me")

        self.assertEqual(response.status_code, 401)

    def test_logout_revokes_the_session_server_side(self) -> None:
        self._login()
        token = self.client.cookies.get(self.cookie_name)
        self.assertTrue(token)

        logout = self.client.post(
            "/api/logout",
            headers={"X-CSRF-Token": self.client.cookies.get("frostvault_csrf") or ""},
        )
        self.assertEqual(logout.status_code, 200)

        replayed = self.client.get(
            "/api/me", headers={"Cookie": f"{self.cookie_name}={token}"}
        )
        self.assertEqual(replayed.status_code, 401)


if __name__ == "__main__":
    unittest.main()
