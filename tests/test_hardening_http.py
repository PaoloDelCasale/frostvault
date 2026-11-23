from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.routes.sign_in import _safe_return_to
from app.sessions import is_reauth_recent
from tests.fixtures import FrostVaultTestCase, seed_vault


class HardeningHttpTestCase(FrostVaultTestCase):
    """Shared harness: a migrated PostgreSQL database with the single owner."""

    password = "correct horse battery"
    owner = {"display_name": "Alice", "password": password}

    def setUp(self) -> None:
        super().setUp()
        self.client = self.http_client()
        self.cookie_name = self.settings.session_cookie_name

    def _csrf(self) -> str:
        return self.client.get("/api/me").json()["csrf_token"]


class HostHardeningTests(HardeningHttpTestCase):
    settings_overrides = {"allowed_hosts": "archive.example"}

    def test_request_with_a_disallowed_host_is_rejected(self) -> None:
        response = self.client.get("/health", headers={"Host": "evil.example"})
        self.assertEqual(response.status_code, 400, response.text)

    def test_request_with_an_allowed_host_passes(self) -> None:
        response = self.client.get("/health", headers={"Host": "archive.example"})
        self.assertEqual(response.status_code, 200, response.text)


class CsrfExposureTests(HardeningHttpTestCase):
    def test_me_exposes_a_csrf_token_and_readable_cookie(self) -> None:
        self.sign_in(password=self.password)

        response = self.client.get("/api/me")

        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()["csrf_token"]
        self.assertTrue(token)
        set_cookie = response.headers["set-cookie"].lower()
        self.assertIn(f"{self.settings.csrf_cookie_name}=".lower(), set_cookie)
        self.assertNotIn("httponly", set_cookie)
        self.assertEqual(self.client.cookies.get(self.settings.csrf_cookie_name), token)


class CsrfEnforcementTests(HardeningHttpTestCase):
    settings_overrides = {"allowed_hosts": "testserver"}

    def test_mutation_without_a_csrf_header_is_rejected(self) -> None:
        self.sign_in(password=self.password)
        response = self.client.post("/api/logout")
        self.assertEqual(response.status_code, 403, response.text)

    def test_mutation_with_a_wrong_csrf_header_is_rejected(self) -> None:
        self.sign_in(password=self.password)
        response = self.client.post(
            "/api/logout", headers={"X-CSRF-Token": "not-the-token"}
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_mutation_with_the_session_csrf_token_passes(self) -> None:
        self.sign_in(password=self.password)
        token = self._csrf()
        response = self.client.post(
            "/api/logout", headers={"X-CSRF-Token": token}
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_cross_origin_mutation_is_rejected_even_with_a_token(self) -> None:
        self.sign_in(password=self.password)
        token = self._csrf()
        response = self.client.post(
            "/api/logout",
            headers={"X-CSRF-Token": token, "Origin": "http://evil.example"},
        )
        self.assertEqual(response.status_code, 403, response.text)


class OpenRedirectGuardTests(unittest.TestCase):
    def test_local_path_is_preserved(self) -> None:
        self.assertEqual(_safe_return_to("/files?dir=x"), "/files?dir=x")

    def test_missing_value_falls_back_to_root(self) -> None:
        self.assertEqual(_safe_return_to(None), "/")

    def test_protocol_relative_url_is_rejected(self) -> None:
        self.assertEqual(_safe_return_to("//evil.example"), "/")

    def test_absolute_url_is_rejected(self) -> None:
        self.assertEqual(_safe_return_to("http://evil.example"), "/")

    def test_backslash_trick_is_rejected(self) -> None:
        self.assertEqual(_safe_return_to("/\\evil.example"), "/")

    def test_non_path_value_is_rejected(self) -> None:
        self.assertEqual(_safe_return_to("evil.example"), "/")


class ReauthTests(HardeningHttpTestCase):
    def _expire_reauth(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE sessions SET reauth_at=%s",
                (datetime(2000, 1, 1, tzinfo=timezone.utc),),
            )

    def _delete_from_cloud(self, csrf: str):
        """«Elimina dal cloud» is one of the two re-authenticated actions."""
        with self.database.connect() as connection:
            if not connection.execute("SELECT id FROM vaults").fetchone():
                seed_vault(connection)
        return self.client.post(
            "/api/cloud-purge",
            headers={"X-CSRF-Token": csrf},
            json={"path": "report.txt", "confirmation": "Docs", "reason": "test", "generated_phrase": "test-phrase"},
        )

    def _assert_not_reauth_required(self, response) -> None:
        self.assertNotEqual(response.json().get("code"), "reauth_required")

    def test_fresh_login_counts_as_recent_reauth(self) -> None:
        self.sign_in(password=self.password)
        csrf = self._csrf()
        self._assert_not_reauth_required(self._delete_from_cloud(csrf))

    def test_stale_reauth_blocks_a_sensitive_action(self) -> None:
        self.sign_in(password=self.password)
        csrf = self._csrf()
        self._expire_reauth()

        response = self._delete_from_cloud(csrf)

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["code"], "reauth_required")

    def test_break_glass_password_reauth_restores_access(self) -> None:
        self.sign_in(password=self.password)
        csrf = self._csrf()
        self._expire_reauth()
        self.assertEqual(self._delete_from_cloud(csrf).status_code, 403)

        reauth = self.client.post(
            "/api/reauth",
            headers={"X-CSRF-Token": csrf},
            json={"password": self.password},
        )
        self.assertEqual(reauth.status_code, 200, reauth.text)

        self._assert_not_reauth_required(self._delete_from_cloud(csrf))

    def test_break_glass_reauth_with_a_wrong_password_is_rejected(self) -> None:
        self.sign_in(password=self.password)
        csrf = self._csrf()

        response = self.client.post(
            "/api/reauth",
            headers={"X-CSRF-Token": csrf},
            json={"password": "definitely wrong"},
        )

        self.assertEqual(response.status_code, 401, response.text)


class ReauthWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_reauth_within_window_is_recent(self) -> None:
        recent = self.now - timedelta(seconds=300)
        self.assertTrue(is_reauth_recent(recent, now=self.now, window_seconds=600))

    def test_reauth_older_than_window_is_not_recent(self) -> None:
        old = self.now - timedelta(seconds=1200)
        self.assertFalse(is_reauth_recent(old, now=self.now, window_seconds=600))

    def test_missing_reauth_is_not_recent(self) -> None:
        self.assertFalse(is_reauth_recent(None, now=self.now, window_seconds=600))


if __name__ == "__main__":
    unittest.main()
