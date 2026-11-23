from __future__ import annotations

import unittest

from tests.fixtures import FrostVaultTestCase, bump_session_version


class SessionHttpTests(FrostVaultTestCase):
    password = "correct horse battery"
    owner = {"display_name": "Alice", "password": password}

    def setUp(self) -> None:
        super().setUp()
        self.client = self.http_client()
        self.cookie_name = self.settings.session_cookie_name

    def test_login_sets_a_hardened_session_cookie(self) -> None:
        response = self.sign_in(password=self.password, username="anything").response

        set_cookie = response.headers["set-cookie"]
        lowered = set_cookie.lower()
        self.assertIn(f"{self.cookie_name}=", set_cookie)
        self.assertIn("httponly", lowered)
        self.assertIn("samesite=lax", lowered)
        self.assertIn("path=/", lowered)
        self.assertNotIn("secure", lowered)

    def test_me_is_reachable_with_the_session_cookie(self) -> None:
        self.sign_in(password=self.password, username="anything")

        response = self.client.get("/api/me")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["id"], 1)
        self.assertEqual(body["display_name"], "Alice")
        self.assertEqual(body["auth_method"], "local")
        self.assertNotIn("username", body)

    def test_me_requires_a_session_cookie(self) -> None:
        response = self.client.get("/api/me")

        self.assertEqual(response.status_code, 401)

    def test_logout_revokes_the_session_server_side(self) -> None:
        self.sign_in(password=self.password, username="anything")
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

    def test_bumped_owner_session_version_revokes_every_session(self) -> None:
        self.sign_in(password=self.password, username="anything")
        token = self.client.cookies.get(self.cookie_name)
        with self.database.connect() as connection:
            bump_session_version(connection)

        replayed = self.client.get(
            "/api/me", headers={"Cookie": f"{self.cookie_name}={token}"}
        )
        self.assertEqual(replayed.status_code, 401)


if __name__ == "__main__":
    unittest.main()
