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
from tests.spa_fixture import write_spa_dist
from tests.test_database import run_alembic


ADMIN_PASSWORD = "correct-horse-battery"
LOOPBACK = ("127.0.0.1", 40000)
OUTSIDE = ("203.0.113.7", 40000)


class BreakGlassHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('admin', 'Admin', %s, TRUE)
                """,
                (hash_password(ADMIN_PASSWORD),),
            )
            connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('member', 'Member', %s, FALSE)
                """,
                (hash_password(ADMIN_PASSWORD),),
            )
            connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('shell', 'Shell', NULL, TRUE)
                """
            )

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
            oidc_enabled=True,
            oidc_issuer="https://issuer.example",
            oidc_client_id="client",
            oidc_client_secret="top-secret",
            break_glass_allowed_cidrs="",
            frontend_dist_dir=str(write_spa_dist(Path(self._tmp.name))),
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
            "app.oidc.settings",
            "app.invites.settings",
            "app.breakglass.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.cookie_name = self.test_settings.session_cookie_name

    def _client(self, peer: tuple[str, int]) -> TestClient:
        return TestClient(app=main.app, client=peer, follow_redirects=False)

    def _backoff_rows(self) -> int:
        with SQLiteConnection(str(self.database_path)) as connection:
            return connection.execute(
                "SELECT COUNT(*) AS total FROM auth_backoff"
            ).fetchone()["total"]

    # --- network gating ---------------------------------------------------

    def test_break_glass_login_from_loopback_succeeds(self) -> None:
        client = self._client(LOOPBACK)
        response = client.post(
            "/api/login", json={"username": "admin", "password": ADMIN_PASSWORD}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(client.cookies.get(self.cookie_name))

    def test_break_glass_login_from_outside_network_is_forbidden(self) -> None:
        client = self._client(OUTSIDE)
        response = client.post(
            "/api/login", json={"username": "admin", "password": ADMIN_PASSWORD}
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIsNone(client.cookies.get(self.cookie_name))

    def test_login_page_hides_local_form_outside_network(self) -> None:
        # Break-glass availability is enforced on POST /api/login (ADR-0004),
        # not by omitting fields from the SPA shell HTML.
        outside = self._client(OUTSIDE)
        denied = outside.post(
            "/api/login", json={"username": "admin", "password": ADMIN_PASSWORD}
        )
        self.assertEqual(denied.status_code, 403, denied.text)

        loopback = self._client(LOOPBACK)
        page = loopback.get("/login")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn('id="root"', page.text)

    # --- admin-only -------------------------------------------------------

    def test_non_admin_cannot_break_glass_login(self) -> None:
        client = self._client(LOOPBACK)
        response = client.post(
            "/api/login", json={"username": "member", "password": ADMIN_PASSWORD}
        )
        self.assertEqual(response.status_code, 401, response.text)

    def test_null_password_user_cannot_break_glass_login(self) -> None:
        client = self._client(LOOPBACK)
        response = client.post(
            "/api/login", json={"username": "shell", "password": ""}
        )
        self.assertEqual(response.status_code, 401, response.text)

    # --- throttling -------------------------------------------------------

    def test_repeated_failures_trigger_backoff(self) -> None:
        client = self._client(LOOPBACK)
        for _ in range(5):
            failed = client.post(
                "/api/login", json={"username": "admin", "password": "wrong-guess"}
            )
            self.assertEqual(failed.status_code, 401, failed.text)
        blocked = client.post(
            "/api/login", json={"username": "admin", "password": ADMIN_PASSWORD}
        )
        self.assertEqual(blocked.status_code, 429, blocked.text)
        self.assertIn("Retry-After", blocked.headers)

    def test_successful_login_resets_backoff(self) -> None:
        client = self._client(LOOPBACK)
        for _ in range(3):
            client.post(
                "/api/login", json={"username": "admin", "password": "wrong-guess"}
            )
        self.assertGreater(self._backoff_rows(), 0)
        ok = client.post(
            "/api/login", json={"username": "admin", "password": ADMIN_PASSWORD}
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(self._backoff_rows(), 0)

    def test_invite_guessing_is_throttled_by_ip(self) -> None:
        client = self._client(OUTSIDE)
        for _ in range(5):
            miss = client.get("/auth/oidc/login", params={"invite": "no-such-token"})
            self.assertEqual(miss.status_code, 400, miss.text)
        blocked = client.get("/auth/oidc/login", params={"invite": "another-guess"})
        self.assertEqual(blocked.status_code, 429, blocked.text)


if __name__ == "__main__":
    unittest.main()
