from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.config import settings

from app.security import DUMMY_PASSWORD_HASH, hash_password
from tests.spa_fixture import write_spa_dist
from tests.proxy_stack import uvicorn_proxy_app
from tests.test_database import run_alembic


ADMIN_PASSWORD = "correct-horse-battery"
LOOPBACK = ("127.0.0.1", 40000)
OUTSIDE = ("203.0.113.7", 40000)
LAN = ("192.168.1.20", 40000)


class BreakGlassHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        # FrostVault has a single User: the owner (#412).
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash)
                VALUES ('admin', 'Admin', %s)
                """,
                (hash_password(ADMIN_PASSWORD),),
            )

        self.test_settings = replace(
            settings,
            database_dsn=database_dsn(str(self.database_path)),
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
            "app.breakglass.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.cookie_name = self.test_settings.session_cookie_name

    def _client(self, peer: tuple[str, int]) -> TestClient:
        return TestClient(app=main.app, client=peer, follow_redirects=False)

    def _backoff_rows(self) -> int:
        with PostgresConnection(str(self.database_path)) as connection:
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

    def test_owner_signs_in_with_the_emergency_password(self) -> None:
        client = self._client(LOOPBACK)
        response = client.post(
            "/api/login", json={"username": "admin", "password": ADMIN_PASSWORD}
        )
        self.assertEqual(response.status_code, 200, response.text)

        me = client.get("/api/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["username"], "admin")
        self.assertEqual(me.json()["auth_method"], "local")
        self.assertNotIn("is_admin", me.json())

    def test_emergency_password_is_accepted_from_a_configured_lan_cidr(self) -> None:
        allowed_settings = replace(
            self.test_settings, break_glass_allowed_cidrs="192.168.1.0/24"
        )
        client = self._client(LAN)
        with patch("app.breakglass.settings", allowed_settings):
            response = client.post(
                "/api/login",
                json={"username": "admin", "password": ADMIN_PASSWORD},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(client.cookies.get(self.cookie_name))

    def test_emergency_password_is_refused_outside_the_configured_lan(self) -> None:
        allowed_settings = replace(
            self.test_settings, break_glass_allowed_cidrs="192.168.1.0/24"
        )
        for peer in (OUTSIDE, ("192.168.2.20", 40000)):
            with self.subTest(peer=peer[0]):
                client = self._client(peer)
                with patch("app.breakglass.settings", allowed_settings):
                    response = client.post(
                        "/api/login",
                        json={"username": "admin", "password": ADMIN_PASSWORD},
                    )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertIsNone(client.cookies.get(self.cookie_name))
        with PostgresConnection(str(self.database_path)) as connection:
            denied = connection.execute(
                "SELECT COUNT(*) AS total FROM audit_events "
                "WHERE event='break_glass_denied'"
            ).fetchone()["total"]
        self.assertEqual(denied, 2)

    def test_untrusted_forwarded_for_cannot_claim_a_lan_address(self) -> None:
        client = self._client(OUTSIDE)
        response = client.post(
            "/api/login",
            headers={"X-Forwarded-For": "127.0.0.1"},
            json={"username": "admin", "password": ADMIN_PASSWORD},
        )
        self.assertEqual(response.status_code, 403, response.text)

    def _proxy_client(self, peer: tuple[str, int]) -> TestClient:
        client = TestClient(
            uvicorn_proxy_app(main.app, "172.30.0.2/32"),
            client=peer,
            base_url="https://frostvault.example.com",
            follow_redirects=False,
        )
        self.addCleanup(client.close)
        return client

    def _proxy_settings(self):
        return replace(
            self.test_settings,
            cookie_secure=True,
            allowed_hosts="frostvault.example.com",
            trusted_proxies="172.30.0.2/32",
            break_glass_allowed_cidrs="192.168.1.0/24",
        )

    def test_uvicorn_trusted_proxy_admits_real_lan_client_and_records_ip(self) -> None:
        proxy_settings = self._proxy_settings()
        client = self._proxy_client(("172.30.0.2", 40000))
        with patch.object(main, "settings", proxy_settings), patch(
            "app.breakglass.settings", proxy_settings
        ):
            response = client.post(
                "/api/login",
                headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": LAN[0]},
                json={"username": "admin", "password": ADMIN_PASSWORD},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(client.get("/api/me").json()["username"], "admin")
        self.assertIn("Secure", response.headers["set-cookie"])
        with PostgresConnection(str(self.database_path)) as connection:
            session = connection.execute("SELECT ip FROM sessions").fetchone()
        self.assertEqual(session["ip"], LAN[0])

    def test_uvicorn_stack_denies_spoofed_password_networks_and_audits_real_ip(self) -> None:
        proxy_settings = self._proxy_settings()
        attempts = (
            (OUTSIDE, "127.0.0.1"),
            (OUTSIDE, LAN[0]),
            (("172.30.0.3", 40000), "127.0.0.1"),
            (("172.30.0.2", 40000), f"127.0.0.1, {OUTSIDE[0]}"),
            (("172.30.0.2", 40000), f"{LAN[0]}, {OUTSIDE[0]}"),
        )
        with patch.object(main, "settings", proxy_settings), patch(
            "app.breakglass.settings", proxy_settings
        ):
            for peer, claimed in attempts:
                with self.subTest(peer=peer, claimed=claimed):
                    client = self._proxy_client(peer)
                    response = client.post(
                        "/api/login",
                        headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": claimed},
                        json={"username": "admin", "password": ADMIN_PASSWORD},
                    )
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertIsNone(client.cookies.get(self.cookie_name))
        with PostgresConnection(str(self.database_path)) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) AS total FROM sessions"
            ).fetchone()["total"], 0)
            events = connection.execute(
                "SELECT detail_json::jsonb->>'ip' AS ip FROM audit_events "
                "WHERE event='break_glass_denied'"
            ).fetchall()
        self.assertCountEqual([row["ip"] for row in events],
                              [OUTSIDE[0], OUTSIDE[0], "172.30.0.3", OUTSIDE[0], OUTSIDE[0]])
        self.assertEqual(self._backoff_rows(), 0)

    def test_uvicorn_stack_cannot_spoof_network_for_password_reauthentication(self) -> None:
        proxy_settings = self._proxy_settings()
        allowed = self._proxy_client(("172.30.0.2", 40000))
        with patch.object(main, "settings", proxy_settings), patch(
            "app.breakglass.settings", proxy_settings
        ):
            login = allowed.post(
                "/api/login",
                headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": LAN[0]},
                json={"username": "admin", "password": ADMIN_PASSWORD},
            )
            self.assertEqual(login.status_code, 200, login.text)
            for peer, claimed in (
                (OUTSIDE, "127.0.0.1"),
                (OUTSIDE, LAN[0]),
                (("172.30.0.2", 40000), f"127.0.0.1, {OUTSIDE[0]}"),
            ):
                with self.subTest(peer=peer, claimed=claimed):
                    outside = self._proxy_client(peer)
                    outside.cookies.update(allowed.cookies)
                    response = outside.post(
                        "/api/reauth",
                        headers={
                            "X-Forwarded-Proto": "https",
                            "X-Forwarded-For": claimed,
                            "X-CSRF-Token": allowed.cookies.get(proxy_settings.csrf_cookie_name),
                        },
                        json={"password": ADMIN_PASSWORD},
                    )
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(response.json()["detail"],
                                     "Password reauthentication is not allowed here")

    def test_unknown_username_and_wrong_password_are_indistinguishable(
        self,
    ) -> None:
        attempts = (
            ("unknown", ADMIN_PASSWORD),
            ("admin", "wrong-password"),
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
        with PostgresConnection(str(self.database_path)) as connection:
            password_hash = connection.execute(
                "SELECT password_hash FROM users WHERE username=%s",
                ("admin",),
            ).fetchone()["password_hash"]

        client = self._client(LOOPBACK)
        with patch("app.main.verify_password", return_value=False) as verify:
            response = client.post(
                "/api/login",
                json={"username": "admin", "password": "wrong-password"},
            )

        self.assertEqual(response.status_code, 401, response.text)
        verify.assert_called_once_with(password_hash, "wrong-password")

    def test_ineligible_logins_verify_once_against_the_dummy_hash_and_fail(
        self,
    ) -> None:
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE users SET password_hash=NULL WHERE username='admin'"
            )
        for username in ("unknown", "admin"):
            with self.subTest(username=username):
                client = self._client(LOOPBACK)
                # Returning True here proves that the dummy verification result
                # is never enough to authenticate an ineligible account.
                with patch("app.main.verify_password", return_value=True) as verify:
                    response = client.post(
                        "/api/login",
                        json={"username": username, "password": ADMIN_PASSWORD},
                    )

                self.assertEqual(response.status_code, 401, response.text)
                verify.assert_called_once_with(DUMMY_PASSWORD_HASH, ADMIN_PASSWORD)

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


if __name__ == "__main__":
    unittest.main()
