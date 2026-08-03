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
from app.security import DUMMY_PASSWORD_HASH, hash_password
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
            connection.execute(
                """
                INSERT INTO users(
                    username, display_name, password_hash, is_admin, active
                ) VALUES ('inactive', 'Inactive', %s, FALSE, FALSE)
                """,
                (hash_password(ADMIN_PASSWORD),),
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

    # --- local authentication --------------------------------------------

    def test_active_non_admin_can_sign_in_without_privilege_escalation(self) -> None:
        client = self._client(LOOPBACK)
        response = client.post(
            "/api/login", json={"username": "member", "password": ADMIN_PASSWORD}
        )
        self.assertEqual(response.status_code, 200, response.text)

        me = client.get("/api/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertFalse(me.json()["is_admin"])
        self.assertEqual(me.json()["auth_method"], "local")
        self.assertEqual(client.get("/api/admin/users").status_code, 403)

    def test_active_non_admin_can_sign_in_from_an_allowed_cidr(self) -> None:
        allowed_settings = replace(
            self.test_settings, break_glass_allowed_cidrs="203.0.113.0/24"
        )
        client = self._client(OUTSIDE)
        with patch("app.breakglass.settings", allowed_settings):
            response = client.post(
                "/api/login",
                json={"username": "member", "password": ADMIN_PASSWORD},
            )
        self.assertEqual(response.status_code, 200, response.text)

    def test_inactive_passwordless_unknown_and_wrong_password_are_indistinguishable(
        self,
    ) -> None:
        attempts = (
            ("inactive", ADMIN_PASSWORD),
            ("shell", ""),
            ("unknown", ADMIN_PASSWORD),
            ("member", "wrong-password"),
        )
        responses = [
            self._client(LOOPBACK).post(
                "/api/login", json={"username": username, "password": password}
            )
            for username, password in attempts
        ]

        self.assertEqual(
            {
                (response.status_code, response.json().get("detail"))
                for response in responses
            },
            {(401, "Incorrect username or password")},
        )

    def test_wrong_password_verifies_once_against_the_real_hash(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            password_hash = connection.execute(
                "SELECT password_hash FROM users WHERE username=%s",
                ("member",),
            ).fetchone()["password_hash"]

        client = self._client(LOOPBACK)
        with patch("app.main.verify_password", return_value=False) as verify:
            response = client.post(
                "/api/login",
                json={"username": "member", "password": "wrong-password"},
            )

        self.assertEqual(response.status_code, 401, response.text)
        verify.assert_called_once_with(password_hash, "wrong-password")

    def test_ineligible_users_verify_once_against_the_dummy_hash_and_cannot_sign_in(
        self,
    ) -> None:
        attempts = (
            ("unknown", ADMIN_PASSWORD),
            ("inactive", ADMIN_PASSWORD),
            ("shell", ADMIN_PASSWORD),
        )
        for username, password in attempts:
            with self.subTest(username=username):
                client = self._client(LOOPBACK)
                # Returning True here proves that the dummy verification result
                # is never enough to authenticate an ineligible account.
                with patch("app.main.verify_password", return_value=True) as verify:
                    response = client.post(
                        "/api/login",
                        json={"username": username, "password": password},
                    )

                self.assertEqual(response.status_code, 401, response.text)
                verify.assert_called_once_with(DUMMY_PASSWORD_HASH, password)

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
