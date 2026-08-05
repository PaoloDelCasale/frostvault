"""HTTP security seams for Local Reauthentication backoff (issue #191)."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import backoff, main
from app.backoff import reauth_account_key, reauth_ip_key
from app.config import settings
from app.database import SQLiteConnection
from app.security import hash_password
from app.sessions import create_session, csrf_token_for
from tests.test_database import run_alembic


class ReauthBackoffHttpTests(unittest.TestCase):
    PASSWORD = "correct-horse-battery"
    WRONG_PASSWORD = "not-the-local-password"
    CLIENT_IP = "127.0.0.1"
    STALE_REAUTH_AT = "2000-01-01T00:00:00+00:00"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with SQLiteConnection(str(self.database_path)) as connection:
            self.user_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('alice', 'Alice', %s, TRUE)
                RETURNING id
                """,
                (hash_password(self.PASSWORD),),
            ).fetchone()["id"]

        self.settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
            break_glass_allowed_cidrs="",
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
            "app.breakglass.settings",
        ):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        with SQLiteConnection(str(self.database_path)) as connection:
            self.raw_token = create_session(
                connection,
                user_id=self.user_id,
                auth_method="local",
                ip=self.CLIENT_IP,
            )
            self.csrf_token = csrf_token_for(connection, self.raw_token)
            session = connection.execute(
                "SELECT id FROM sessions WHERE user_id=%s", (self.user_id,)
            ).fetchone()
            self.session_id = session["id"]
            connection.execute(
                "UPDATE sessions SET reauth_at=%s WHERE id=%s",
                (self.STALE_REAUTH_AT, self.session_id),
            )

        self.ip_counter_key = reauth_ip_key(self.CLIENT_IP)
        self.account_counter_key = reauth_account_key(self.user_id)
        self.client = TestClient(main.app, client=(self.CLIENT_IP, 50000))
        self.client.cookies.set(self.settings.session_cookie_name, self.raw_token)
        self.client.cookies.set(self.settings.csrf_cookie_name, self.csrf_token)

    def _reauth(self, password: str):
        return self.client.post(
            "/api/reauth",
            json={"password": password},
            headers={"X-CSRF-Token": self.csrf_token},
        )

    def _session_reauth_at(self) -> str | None:
        with SQLiteConnection(str(self.database_path)) as connection:
            return connection.execute(
                "SELECT reauth_at FROM sessions WHERE id=%s", (self.session_id,)
            ).fetchone()["reauth_at"]

    def _insert_counter(
        self,
        connection: SQLiteConnection,
        *,
        scope: str,
        key: str,
        failure_count: int,
        updated_at: str,
        next_allowed_at: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO auth_backoff(
                scope, key, failure_count, next_allowed_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (scope, key, failure_count, next_allowed_at, updated_at),
        )

    def test_failures_commit_to_reauth_namespaces_and_threshold_throttles(self) -> None:
        """Counter writes survive 401/429 responses and never step up a session."""
        for _ in range(backoff.THRESHOLD - 1):
            failed = self._reauth(self.WRONG_PASSWORD)
            self.assertEqual(failed.status_code, 401)

        threshold = self._reauth(self.WRONG_PASSWORD)
        self.assertEqual(threshold.status_code, 429)
        self.assertGreater(int(threshold.headers["Retry-After"]), 0)

        # A correct password cannot bypass the already persisted threshold.
        blocked = self._reauth(self.PASSWORD)
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Retry-After", blocked.headers)
        self.assertEqual(self._session_reauth_at(), self.STALE_REAUTH_AT)

        with SQLiteConnection(str(self.database_path)) as connection:
            counters = connection.execute(
                """
                SELECT scope, key, failure_count
                FROM auth_backoff
                WHERE (scope=%s AND key=%s) OR (scope=%s AND key=%s)
                ORDER BY scope
                """,
                (
                    "account",
                    self.account_counter_key,
                    "ip",
                    self.ip_counter_key,
                ),
            ).fetchall()
            events = connection.execute(
                """
                SELECT event, actor_user_id, detail_json
                FROM audit_events
                WHERE actor_user_id=%s
                  AND event IN ('reauth_failed', 'auth_backoff_blocked')
                ORDER BY id
                """,
                (self.user_id,),
            ).fetchall()

        self.assertEqual(
            {(row["scope"], row["key"]) for row in counters},
            {
                ("account", self.account_counter_key),
                ("ip", self.ip_counter_key),
            },
        )
        self.assertEqual({row["failure_count"] for row in counters}, {backoff.THRESHOLD})
        self.assertIn("reauth_failed", {row["event"] for row in events})
        self.assertIn("auth_backoff_blocked", {row["event"] for row in events})
        for event in events:
            detail = json.loads(event["detail_json"])
            self.assertEqual(event["actor_user_id"], self.user_id)
            self.assertEqual(detail.get("flow"), "reauth")
            self.assertNotIn("password", detail)
            self.assertNotIn("secret", detail)

    def test_success_clears_only_reauth_counters(self) -> None:
        """A Local Reauthentication must not reset Local Sign-in counters."""
        now = datetime.now(timezone.utc).isoformat()
        with SQLiteConnection(str(self.database_path)) as connection:
            # Existing Local Sign-in counters deliberately use the unprefixed
            # keys and must remain untouched by the reauth success below.
            self._insert_counter(
                connection,
                scope="ip",
                key=self.CLIENT_IP,
                failure_count=2,
                updated_at=now,
            )
            self._insert_counter(
                connection,
                scope="account",
                key="alice",
                failure_count=2,
                updated_at=now,
            )
            self._insert_counter(
                connection,
                scope="ip",
                key=self.ip_counter_key,
                failure_count=2,
                updated_at=now,
            )
            self._insert_counter(
                connection,
                scope="account",
                key=self.account_counter_key,
                failure_count=2,
                updated_at=now,
            )

        response = self._reauth(self.PASSWORD)
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(self._session_reauth_at(), self.STALE_REAUTH_AT)

        with SQLiteConnection(str(self.database_path)) as connection:
            retained = connection.execute(
                """
                SELECT scope, key FROM auth_backoff
                WHERE (scope=%s AND key=%s) OR (scope=%s AND key=%s)
                """,
                ("ip", self.CLIENT_IP, "account", "alice"),
            ).fetchall()
            reauth_rows = connection.execute(
                """
                SELECT scope, key FROM auth_backoff
                WHERE (scope=%s AND key=%s) OR (scope=%s AND key=%s)
                """,
                (
                    "ip",
                    self.ip_counter_key,
                    "account",
                    self.account_counter_key,
                ),
            ).fetchall()
            success = connection.execute(
                """
                SELECT actor_user_id, detail_json
                FROM audit_events
                WHERE event='reauth_succeeded'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()

        self.assertEqual(
            {(row["scope"], row["key"]) for row in retained},
            {("ip", self.CLIENT_IP), ("account", "alice")},
        )
        self.assertEqual(reauth_rows, [])
        self.assertEqual(success["actor_user_id"], self.user_id)
        success_detail = json.loads(success["detail_json"])
        self.assertEqual(success_detail.get("flow"), "reauth")
        self.assertNotIn("password", success_detail)
        self.assertNotIn("secret", success_detail)

    def test_retry_after_uses_the_longest_active_reauth_dimension(self) -> None:
        """A shorter IP delay must not understate a longer account delay."""
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        with SQLiteConnection(str(self.database_path)) as connection:
            self._insert_counter(
                connection,
                scope="ip",
                key=self.ip_counter_key,
                failure_count=backoff.THRESHOLD,
                updated_at=now.isoformat(),
                next_allowed_at=(now + timedelta(seconds=30)).isoformat(),
            )
            self._insert_counter(
                connection,
                scope="account",
                key=self.account_counter_key,
                failure_count=backoff.THRESHOLD + 1,
                updated_at=now.isoformat(),
                next_allowed_at=(now + timedelta(seconds=90)).isoformat(),
            )

        with patch("app.backoff._now", return_value=now):
            response = self._reauth(self.PASSWORD)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "90")
        self.assertEqual(self._session_reauth_at(), self.STALE_REAUTH_AT)


if __name__ == "__main__":
    unittest.main()
