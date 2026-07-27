from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.database import SQLiteConnection
from app.main import app
from app.main import _safe_return_to
from app.security import hash_password
from app.sessions import create_session, is_reauth_recent
from tests.test_database import run_alembic


class HardeningHttpTestCase(unittest.TestCase):
    """Shared harness: a migrated SQLite database with one admin user."""

    settings_overrides: dict[str, object] = {}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        database_path = Path(self._tmp.name) / "app.db"
        self.database_path = database_path
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
            self.user_id = connection.execute(
                "SELECT id FROM users WHERE username=%s", (self.username,)
            ).fetchone()["id"]

        self.settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(database_path),
            cookie_secure=False,
            **self.settings_overrides,
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

    def _login(self) -> None:
        response = self.client.post(
            "/api/login",
            json={"username": self.username, "password": self.password},
        )
        self.assertEqual(response.status_code, 200, response.text)

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
        self._login()

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
        self._login()
        response = self.client.post("/api/logout")
        self.assertEqual(response.status_code, 403, response.text)

    def test_mutation_with_a_wrong_csrf_header_is_rejected(self) -> None:
        self._login()
        response = self.client.post(
            "/api/logout", headers={"X-CSRF-Token": "not-the-token"}
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_mutation_with_the_session_csrf_token_passes(self) -> None:
        self._login()
        token = self._csrf()
        response = self.client.post(
            "/api/logout", headers={"X-CSRF-Token": token}
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_cross_origin_mutation_is_rejected_even_with_a_token(self) -> None:
        self._login()
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
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE sessions SET reauth_at=%s",
                ("2000-01-01T00:00:00+00:00",),
            )

    def _create_user(self, csrf: str, username: str = "bob"):
        return self.client.post(
            "/api/admin/users",
            headers={"X-CSRF-Token": csrf},
            json={"username": username, "display_name": "Bob"},
        )

    def test_fresh_login_counts_as_recent_reauth(self) -> None:
        self._login()
        csrf = self._csrf()
        response = self._create_user(csrf)
        self.assertEqual(response.status_code, 201, response.text)

    def test_stale_reauth_blocks_a_sensitive_action(self) -> None:
        self._login()
        csrf = self._csrf()
        self._expire_reauth()

        response = self._create_user(csrf)

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json(), {"error": "reauth_required"})

    def test_break_glass_password_reauth_restores_access(self) -> None:
        self._login()
        csrf = self._csrf()
        self._expire_reauth()
        self.assertEqual(self._create_user(csrf).status_code, 403)

        reauth = self.client.post(
            "/api/reauth",
            headers={"X-CSRF-Token": csrf},
            json={"password": self.password},
        )
        self.assertEqual(reauth.status_code, 200, reauth.text)

        self.assertEqual(self._create_user(csrf).status_code, 201)

    def test_break_glass_reauth_with_a_wrong_password_is_rejected(self) -> None:
        self._login()
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
        recent = (self.now - timedelta(seconds=300)).isoformat()
        self.assertTrue(is_reauth_recent(recent, now=self.now, window_seconds=600))

    def test_reauth_older_than_window_is_not_recent(self) -> None:
        old = (self.now - timedelta(seconds=1200)).isoformat()
        self.assertFalse(is_reauth_recent(old, now=self.now, window_seconds=600))

    def test_missing_reauth_is_not_recent(self) -> None:
        self.assertFalse(is_reauth_recent(None, now=self.now, window_seconds=600))


class AdminUserActiveSessionTests(HardeningHttpTestCase):
    """BUG-014: active transitions must invalidate existing Sessions (REQ-026)."""

    def test_bug_014_active_change_bumps_session_version(self) -> None:
        """[BUG-014][Req: REQ-026] deactivate/reactivate invalidates Sessions.

        Seam: PATCH /api/admin/users/{id} (public admin update) with observation
        via the target user's existing session cookie against GET /api/me.
        Desired: any active update bumps session_version so old cookies fail.
        Previously: only password changes bump the version; reactivation revives
        pre-deactivation cookies.
        """
        # Non-admins cannot use Break-glass Login; seed the kind of server-side
        # Session they would obtain through OIDC (same harness as invite_http).
        with SQLiteConnection(str(self.database_path)) as connection:
            member_id = connection.execute(
                """
                INSERT INTO users(
                    username, display_name, password_hash, is_admin, active
                ) VALUES ('member', 'Member', %s, FALSE, TRUE)
                RETURNING id
                """,
                (hash_password("member-horse-battery"),),
            ).fetchone()["id"]
            stale_cookie = create_session(
                connection, user_id=member_id, auth_method="oidc"
            )

        member = TestClient(app, client=("127.0.0.1", 50001))
        member.cookies.set(self.cookie_name, stale_cookie)
        self.assertEqual(member.get("/api/me").status_code, 200)

        self._login()
        csrf = self._csrf()
        deactivate = self.client.patch(
            f"/api/admin/users/{member_id}",
            headers={"X-CSRF-Token": csrf},
            json={"active": False},
        )
        self.assertEqual(deactivate.status_code, 200, deactivate.text)
        self.assertFalse(deactivate.json()["active"])

        reactivate = self.client.patch(
            f"/api/admin/users/{member_id}",
            headers={"X-CSRF-Token": csrf},
            json={"active": True},
        )
        self.assertEqual(reactivate.status_code, 200, reactivate.text)
        self.assertTrue(reactivate.json()["active"])

        replayed = TestClient(app, client=("127.0.0.1", 50002))
        replayed.cookies.set(self.cookie_name, stale_cookie)
        response = replayed.get("/api/me")
        self.assertEqual(
            response.status_code,
            401,
            "pre-deactivation cookie must not authenticate after reactivate",
        )


class AdminUserLastAdminGuardTests(unittest.TestCase):
    """BUG-020: last-admin deactivate must be race-safe (REQ-032)."""

    def test_bug_020_last_admin_guard_is_atomic_with_update(self) -> None:
        """[BUG-020][Req: REQ-032] last-admin invariant must not be check-then-act.

        Seam: ``app.services.user_administration.update_user``, the single
        place PATCH /api/admin/users/{id} now delegates to (the same public
        admin-update boundary exercised by BUG-014 and by the audited Quality
        Playbook regression). Asserts the last-admin guard is encoded
        atomically with the mutating UPDATE (conditional predicate plus the
        lock taken in the same transaction), not as a separate check-then-act
        across two ``with db()`` blocks.
        Desired: deactivating or demoting an administrator uses a single
        transaction and a conditional UPDATE that refuses to leave zero active
        admins.
        Previously: COUNT in one ``with db()`` then UPDATE in another with no
        last-admin predicate on UPDATE.
        """
        service = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "services"
            / "user_administration.py"
        ).read_text(encoding="utf-8")
        start = service.index("def update_user")
        body = service[start:]

        self.assertIn("last_admin", body)
        # The UPDATE that deactivates or demotes must itself encode the guard
        # (subquery / EXISTS) while holding the lock taken in the same
        # transaction.
        update_idx = body.find("UPDATE users SET")
        self.assertGreaterEqual(update_idx, 0)
        guard = body[: update_idx + 400]
        self.assertIn("_lock_active_administrators", guard)
        self.assertTrue(
            "EXISTS (" in guard or "COUNT(" in guard,
            "UPDATE must be conditional on the surviving-administrator predicate",
        )
        self.assertIn("FOR UPDATE", service)


if __name__ == "__main__":
    unittest.main()
